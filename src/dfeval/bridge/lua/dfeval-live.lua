-- Transparent real-game bridge. Trusted host: dfeval-live start <32 hex session>.
-- The evaluated agent cannot supply code, command names, paths, or job templates.
-- Native field/API references: DFHack 53.16-r1 docs/dev/Lua API.txt;
-- scripts/modtools/set-need.lua, scripts/autofish.lua, scripts/full-heal.lua;
-- lua/dfhack/workshops.lua, scripts/lever.lua, vanilla reaction_other.txt.
local json = require('json')
local json_types = require('json.internal')
local NULL = '\0'
local YEAR_TICKS = 403200 -- 12 months * 28 days * 1200 simulation ticks
local BREW_REACTION = 'BREW_DRINK_FROM_PLANT'
local MAX_BREW_EVENTS = 4096
local MAX_PRODUCT_OUTPUTS = 256
local MAX_TRACKED_BREW_JOBS = 100000
local function array() return json_types:newArray{} end
local function object() return json_types:newObject{} end

local function read_field(errors, path, fn)
    local ok, value = pcall(fn)
    if not ok or value == nil then
        table.insert(errors, {path=path, message=ok and 'Field unavailable' or tostring(value)})
        return NULL
    end
    return value
end

local function absolute_tick()
    return df.global.cur_year * YEAR_TICKS + df.global.cur_year_tick
end

-- Trusted-host pacing only. DFHack 53.16-r1.1 setfps.lua changes these two
-- fields; there is no setFPS Lua API in this release. Never write gfps, native
-- action timers, or calendar multipliers. Source: scripts commit 7549711a.
local function native_fps()
    local enabler = df.global.enabler
    local fps, gfps = enabler.fps, enabler.gfps
    if type(fps) ~= 'number' or fps ~= fps or fps <= 0 or fps == math.huge
        or type(gfps) ~= 'number' or gfps ~= gfps or gfps <= 0 or gfps == math.huge then
        error('Native simulation/graphics FPS caps are unavailable or invalid')
    end
    return fps, gfps
end

local function capture_speed(state)
    state.speed = {override_active=false}
    local ok, original, gfps = pcall(native_fps)
    if ok then
        state.speed.original, state.speed.original_gfps = original, gfps
    else
        state.speed.capture_error = tostring(original)
    end
end

local function write_native_fps(fps)
    local _, gfps = native_fps()
    df.global.enabler.fps = fps
    df.global.enabler.fps_per_gfps = fps / gfps
    local effective, after_gfps = native_fps()
    local ratio = df.global.enabler.fps_per_gfps
    if effective ~= fps or after_gfps ~= gfps or type(ratio) ~= 'number' or ratio ~= ratio
        or math.abs(ratio - fps/gfps) > 0.00001 * math.max(1, fps/gfps) then
        error('Native FPS cap or pacing ratio did not match requested readback')
    end
    return effective
end

local function restore_speed(state)
    local speed = state.speed
    if not speed or not speed.original then
        return false, 'Original native simulation FPS cap is unknown'
    end
    local ok, err = pcall(function()
        if speed.override_active then write_native_fps(speed.original) end
        local effective = native_fps()
        if effective ~= speed.original then error('Native simulation cap does not match session original') end
    end)
    if not ok then
        speed.restore_error = tostring(err)
        return false, speed.restore_error
    end
    speed.override_active, speed.restore_error = false, nil
    return true
end

local function speed_status(state, errors)
    local speed = state and state.speed or {}
    local out = {original=speed.original or NULL, requested=speed.requested or NULL,
        effective=NULL, graphics_cap=NULL, original_graphics_cap=speed.original_gfps or NULL,
        override_active=speed.override_active == true, restore_error=speed.restore_error or NULL,
        capture_error=speed.capture_error or NULL, restore_reason=speed.restore_reason or NULL,
        max_requested=10000}
    local ok, effective, gfps = pcall(native_fps)
    if ok then out.effective, out.graphics_cap = effective, gfps
    else table.insert(errors, {path='simulation_fps', message=tostring(effective)}) end
    return out
end

local function set_speed(state, fps, req)
    if not state.speed or not state.speed.original then
        error('Cannot change simulation FPS without a known session original cap')
    end
    if df.global.pause_state ~= true then error('Pause the fortress before changing simulation FPS') end
    state.speed.requested = fps
    state.speed.override_active = true -- retain recovery ownership before either write
    state.speed.request_seq = req.seq
    state.speed.world_data = df.global.world.world_data
    state.speed.restore_reason = nil
    local ok, err = pcall(write_native_fps, fps)
    if not ok then
        local restored, why = restore_speed(state)
        error(tostring(err) .. (restored and '; original cap restored' or '; restore failed: ' .. tostring(why)))
    end
end

-- Evidence hooks observe native production without modifying call_native,
-- reagents, products, labor or inventories. Pinned DFHack b638b59 eventful.cpp
-- 328-359 calls onReactionComplete only after native produce grows out_items.
local function reset_brewing(state)
    state.brew_epoch = (state.brew_epoch or 0) + 1
    state.brew_events, state.brew_pending, state.brew_jobs = array(), {}, {}
    state.brew_event_count, state.brew_dropped, state.brew_job_count = 0, 0, 0
    state.brew_error_count, state.brew_last_error = 0, NULL
end

local function product_tracking_error(state, err)
    state.brew_error_count = state.brew_error_count + 1
    state.brew_last_error = tostring(err):sub(1, 1000)
end

local function install_product_tracking(state)
    state.brew_available = false
    local ok, err = pcall(function()
        local eventful = require('plugins.eventful')
        eventful.onReactionCompleting.dfeval_live = function(reaction, product, unit,
                input_items, input_reagents, output_items, call_native)
            if live_state ~= state or not reaction or reaction.code ~= BREW_REACTION then return end
            local succeeded, failure = pcall(function()
                if state.world_data ~= df.global.world.world_data or not unit then return end
                local job = unit.job.current_job
                if not job or job.reaction_name ~= BREW_REACTION or not state.brew_jobs[job.id] then return end
                state.brew_pending[unit.id] = {
                    job_id=job.id, workshop_id=state.brew_jobs[job.id], product=product,
                    output_count=#output_items, next_item_id=df.global.item_next_id,
                }
            end)
            if not succeeded then product_tracking_error(state, failure) end
        end
        eventful.onReactionComplete.dfeval_live = function(reaction, product, unit,
                input_items, input_reagents, output_items)
            if live_state ~= state or not reaction or reaction.code ~= BREW_REACTION then return end
            local succeeded, failure = pcall(function()
                if state.world_data ~= df.global.world.world_data or not unit then return end
                local pending = state.brew_pending[unit.id]
                state.brew_pending[unit.id] = nil
                if not pending or pending.product ~= product then return end
                if not state.brew_jobs[pending.job_id] then return end
                local next_item_id = df.global.item_next_id
                local outputs, errors = array(), array()
                -- DF vectors are indexed from zero. Ignore earlier outputs in
                -- this reaction's cumulative output vector and existing items.
                local output_end = math.min(#output_items, pending.output_count + MAX_PRODUCT_OUTPUTS)
                for index = pending.output_count, output_end - 1 do
                    local item = output_items[index]
                    table.insert(outputs, {
                        id=item.id,
                        item_type=df.item_type[item:getType()] or NULL,
                        stack_size=read_field(errors, 'brewing.outputs.' .. item.id .. '.stack_size',
                            function() return item:getStackSize() end),
                        newly_created=item.id >= pending.next_item_id and item.id < next_item_id,
                    })
                end
                if #outputs == 0 then return end
                table.sort(outputs, function(a,b) return a.id < b.id end)
                state.brew_event_count = state.brew_event_count + 1
                if #state.brew_events >= MAX_BREW_EVENTS then
                    state.brew_dropped = state.brew_dropped + 1
                    return
                end
                table.insert(state.brew_events, {
                    id=state.brew_event_count, session=state.session, epoch=state.brew_epoch,
                    source='dfhack.eventful.onReactionComplete',
                    kind='native_reaction_product', reaction=BREW_REACTION,
                    job_id=pending.job_id, workshop_id=pending.workshop_id, worker_id=unit.id,
                    absolute_tick=absolute_tick(), item_next_id_before=pending.next_item_id,
                    item_next_id_after=next_item_id, outputs=outputs, errors=errors,
                    dropped_outputs=math.max(0, #output_items - output_end),
                })
            end)
            if not succeeded then product_tracking_error(state, failure) end
        end
        state.brew_available = true
    end)
    if not ok then
        product_tracking_error(state, err)
    end
end

local function brewing_snapshot(state)
    return {
        available=state.brew_available, session=state.session, epoch=state.brew_epoch,
        source='dfhack.eventful.onReactionComplete', events=state.brew_events,
        event_count=state.brew_event_count, dropped_events=state.brew_dropped,
        error_count=state.brew_error_count, last_error=state.brew_last_error,
        queued_jobs=state.brew_job_count, max_events=MAX_BREW_EVENTS,
        max_outputs_per_event=MAX_PRODUCT_OUTPUTS,
        notes='Only session-queued brew jobs; post-native product callbacks, paired output ranges and native creation IDs. Missing events do not prove failure. No stock-delta or disappearing-job inference.',
    }
end

local function status(state)
    local errors = array()
    local out = {schema_version=1, errors=errors}
    out.simulation_fps = speed_status(state or live_state, errors)
    local function field(name, fn) out[name] = read_field(errors, name, fn) end
    field('df_version', function() return dfhack.getDFVersion() end)
    field('dfhack_version', function() return dfhack.getDFHackVersion() end)
    field('world_loaded', function() return dfhack.isWorldLoaded() end)
    field('map_loaded', function() return dfhack.isMapLoaded() end)
    field('mode', function() return df.game_mode[df.global.gamemode] end)
    field('fortress_mode', function() return dfhack.world.isFortressMode() end)
    field('paused', function() return df.global.pause_state end)
    field('ui_focus', function() return dfhack.gui.getCurFocus() end)
    out.year, out.year_tick, out.absolute_tick, out.save_directory = NULL, NULL, NULL, NULL
    if out.world_loaded == true then
        field('year', function() return df.global.cur_year end)
        field('year_tick', function() return df.global.cur_year_tick end)
        field('absolute_tick', absolute_tick)
        field('save_directory', function() return df.global.world.cur_savegame.save_dir end)
    end
    return out
end

local function job_snapshot(job, errors)
    local out = {id=job.id}
    local prefix = 'jobs.' .. tostring(job.id) .. '.'
    local function field(name, fn) out[name] = read_field(errors, prefix .. name, fn) end
    field('type', function() return df.job_type[job.job_type] end)
    field('name', function() return dfhack.df2utf(dfhack.job.getName(job)) end)
    field('reaction_name', function() return job.reaction_name end)
    field('suspended', function() return job.flags.suspend end)
    field('repeating', function() return job.flags['repeat'] end)
    field('completion_timer', function() return job.completion_timer end)
    out.worker_id = NULL
    local worker = read_field(errors, prefix .. 'worker_lookup', function()
        return dfhack.job.getWorker(job) or false
    end)
    if worker ~= false and worker ~= NULL then out.worker_id = worker.id end
    return out
end

local function citizen_snapshot(unit, errors)
    local out = {id=unit.id}
    local prefix = 'citizens.' .. tostring(unit.id) .. '.'
    local function field(name, fn) out[name] = read_field(errors, prefix .. name, fn) end
    field('name', function() return dfhack.df2utf(dfhack.units.getReadableName(unit)) end)
    field('historical_figure_id', function() return unit.hist_figure_id end)
    field('dead', function() return dfhack.units.isDead(unit) end)
    field('stress', function() return unit.status.current_soul.personality.stress end)
    field('hunger_timer', function() return unit.counters2.hunger_timer end)
    field('thirst_timer', function() return unit.counters2.thirst_timer end)
    field('sleepiness_timer', function() return unit.counters2.sleepiness_timer end)
    field('blood_count', function() return unit.body.blood_count end)
    field('blood_max', function() return unit.body.blood_max end)
    field('wound_count', function() return #unit.body.wounds end)
    field('position', function() return {x=unit.pos.x, y=unit.pos.y, z=unit.pos.z} end)
    field('needs', function()
        local needs = array()
        for _, need in ipairs(unit.status.current_soul.personality.needs) do
            table.insert(needs, {id=need.id, type=df.need_type[need.id] or NULL,
                focus_level=need.focus_level, need_level=need.need_level, deity_id=need.deity_id})
        end
        return needs
    end)
    out.current_job = NULL
    field('current_job', function()
        return unit.job.current_job and job_snapshot(unit.job.current_job, errors) or NULL
    end)
    return out
end

local STOCK_TYPES = {'DRINK', 'FOOD', 'PLANT', 'PLANT_GROWTH', 'MEAT', 'FISH',
                     'FISH_RAW', 'EGG', 'CHEESE', 'SEEDS'}
local EXCLUDE_FLAGS = {'rotten', 'trader', 'hostile', 'forbid', 'dump', 'on_fire',
                      'garbage_collect', 'owned', 'removed', 'encased', 'spider_web'}
local function stocks_snapshot(errors)
    local out = {by_item_type=object(), items=array(),
        definitions={
            source='world.items.other.IN_PLAY; one record per native item object',
            units='sum of getStackSize(); item_objects counts stacks, not portions',
            drink='DRINK item type only; excludes food and containers',
            prepared_food='FOOD item type only; prepared meals, not all edible items',
            candidate='Excludes listed item flags; does not verify container flags, ownership of map, path access, ingredient suitability, or reservations',
            excluded_flags=EXCLUDE_FLAGS,
        },
        total_edible_food=NULL, path_accessibility=NULL,
    }
    local kinds = {}
    for _, kind in ipairs(STOCK_TYPES) do
        kinds[kind] = true
        out.by_item_type[kind] = {item_objects=0, stack_units=0, candidate_stack_units=0}
    end
    for _, item in ipairs(df.global.world.items.other[df.items_other_id.IN_PLAY]) do
        local kind = df.item_type[item:getType()]
        if kinds[kind] then
            local record = {id=item.id, item_type=kind, excluded_by=array()}
            record.stack_size = read_field(errors, 'stocks.items.' .. item.id .. '.stack_size',
                function() return item:getStackSize() end)
            local valid = true
            for _, flag in ipairs(EXCLUDE_FLAGS) do
                if item.flags[flag] then
                    table.insert(record.excluded_by, flag)
                    valid = false
                end
            end
            record.in_job = item.flags.in_job
            record.candidate = valid
            table.insert(out.items, record)
            local total = out.by_item_type[kind]
            total.item_objects = total.item_objects + 1
            if record.stack_size == NULL then
                total.stack_units, total.candidate_stack_units = NULL, NULL
            else
                if total.stack_units ~= NULL then total.stack_units = total.stack_units + record.stack_size end
                if valid and total.candidate_stack_units ~= NULL then
                    total.candidate_stack_units = total.candidate_stack_units + record.stack_size
                end
            end
        end
    end
    return out
end

local function observe(state)
    local out = status(state)
    out.citizens, out.known_former_citizens = NULL, NULL
    out.stocks, out.workshops, out.jobs = NULL, NULL, NULL
    out.measurement_notes = {
        citizens='Fortress citizens, including insane citizens, using isCitizen(unit, true) and excluding isDead(unit).',
        former_citizens='Previously observed citizens; a missing unit is not automatically a death. History before this session is not reconstructed.',
        needs='Native need_level and focus_level; no inferred wellbeing score.',
        timers='Raw native counters; no clinical or welfare thresholds applied.',
        wounds='Native wound record count, not a count or severity estimate of lasting injuries.',
    }
    if out.map_loaded ~= true or out.fortress_mode ~= true then
        table.insert(out.errors, {path='observation', message='A loaded fortress map is required'})
        return out
    end
    -- A changed world must not carry IDs from the previous world into its history.
    if state.world_data ~= df.global.world.world_data then
        state.known_citizens = {}
        state.world_data = df.global.world.world_data
        reset_brewing(state)
    end
    out.brewing = brewing_snapshot(state)
    out.citizens = read_field(out.errors, 'citizens', function()
        local citizens, current = array(), {}
        for _, unit in ipairs(df.global.world.units.active) do
            -- include_insane bypasses isSane(), whose checks include death.
            -- Keep the explicit native death exclusion when including insanity.
            if dfhack.units.isCitizen(unit, true) and not dfhack.units.isDead(unit) then
                table.insert(citizens, citizen_snapshot(unit, out.errors))
                current[unit.id] = true
                state.known_citizens[unit.id] = true
            end
        end
        out.known_former_citizens = array()
        for id in pairs(state.known_citizens) do
            if not current[id] then
                local unit = df.unit.find(id)
                table.insert(out.known_former_citizens, unit and citizen_snapshot(unit, out.errors)
                    or {id=id, dead=NULL, missing=true})
            end
        end
        table.sort(citizens, function(a,b) return a.id < b.id end)
        table.sort(out.known_former_citizens, function(a,b) return a.id < b.id end)
        return citizens
    end)
    out.stocks = read_field(out.errors, 'stocks', function() return stocks_snapshot(out.errors) end)
    out.workshops = read_field(out.errors, 'workshops', function()
        local workshops = array()
        for _, building in ipairs(df.global.world.buildings.all) do
            if building:getType() == df.building_type.Workshop then
                local jobs = array()
                for _, job in ipairs(building.jobs) do table.insert(jobs, job_snapshot(job, out.errors)) end
                table.insert(workshops, {id=building.id, type=df.workshop_type[building:getSubtype()] or NULL,
                    completed=building:getBuildStage() == building:getMaxBuildStage(),
                    position={x=building.centerx, y=building.centery, z=building.z}, jobs=jobs})
            end
        end
        return workshops
    end)
    out.jobs = read_field(out.errors, 'jobs', function()
        local jobs = array()
        local link = df.global.world.jobs.list.next
        while link do
            if link.item then table.insert(jobs, job_snapshot(link.item, out.errors)) end
            link = link.next
        end
        return jobs
    end)
    return out
end

local function require_fortress()
    if not dfhack.isMapLoaded() or not dfhack.world.isFortressMode() then
        error('A loaded fortress map is required')
    end
end

local function integer(value, name, low, high)
    if type(value) ~= 'number' or value ~= math.floor(value) or value < low or value > high then
        error(name .. ' must be an integer between ' .. low .. ' and ' .. high)
    end
end

local function validate(req, state)
    if type(req) ~= 'table' or req.protocol ~= 1 or req.session ~= state.session then
        error('Protocol or session mismatch')
    end
    integer(req.seq, 'seq', 1, 1000000000)
    if req.seq <= state.last_sequence then error('Sequence is stale') end
    if type(req.args) ~= 'table' then error('args must be an object') end
    local expected = {status={}, observe={}, pause={}, advance_ticks={ticks=true},
                      queue_brew={workshop_id=true, quantity=true},
                      set_simulation_fps={fps=true}, restore_simulation_fps={}}
    if not expected[req.op] then error('Unsupported operation') end
    for key in pairs(req.args) do
        if not expected[req.op][key] then error('Unexpected argument: ' .. tostring(key)) end
    end
    if req.op == 'advance_ticks' then integer(req.args.ticks, 'ticks', 1, 12000) end
    if req.op == 'set_simulation_fps' then integer(req.args.fps, 'fps', 1, 10000) end
    if req.op == 'queue_brew' then
        integer(req.args.workshop_id, 'workshop_id', 0, 2147483647)
        integer(req.args.quantity, 'quantity', 1, 10)
    end
    if type(req.timeout_ms) ~= 'number' or req.timeout_ms <= 0 or req.timeout_ms > 3600000 then
        error('timeout_ms must be between 1 and 3600000')
    end
    if type(req.expires_at) ~= 'number' or req.expires_at < os.time() then error('Request expired') end
end

local function respond(state, req, ok, result)
    local response = {protocol=1, session=state.session, seq=req.seq, op=req.op, ok=ok}
    if ok then response.result = result else response.error = tostring(result) end
    local target = state.directory .. '/response.' .. tostring(req.seq) .. '.json'
    local temporary = target .. '.tmp'
    json.encode_file(response, temporary, {null=NULL})
    local renamed, err = os.rename(temporary, target)
    if not renamed then error('Could not publish response: ' .. tostring(err)) end
end

local function queue_brew(state, args)
    require_fortress()
    if not state.brew_available then error('Native brewing product evidence hooks are unavailable') end
    if state.world_data ~= df.global.world.world_data then error('Observe the current world before queuing work') end
    if state.brew_job_count + args.quantity > MAX_TRACKED_BREW_JOBS then
        error('Session brewing job limit reached')
    end
    local building = df.building.find(args.workshop_id)
    if not building or building:getType() ~= df.building_type.Workshop
            or building:getSubtype() ~= df.workshop_type.Still then error('workshop_id must identify a still') end
    if building:getBuildStage() ~= building:getMaxBuildStage() then error('Still is not complete') end
    if dfhack.buildings.markedForRemoval(building) then error('Still is marked for removal') end
    if #building.jobs + args.quantity > 10 then error('Still job queue would exceed 10 jobs') end
    local template
    for _, candidate in pairs(require('dfhack.workshops').getJobs(
            df.building_type.Workshop, df.workshop_type.Still, -1) or {}) do
        if candidate.job_fields and candidate.job_fields.job_type == df.job_type.CustomReaction
                and candidate.job_fields.reaction_name == BREW_REACTION then template = candidate end
    end
    if not template or #template.items == 0 then error('Native brew-from-plant template is unavailable') end
    local reaction_index
    for index, reaction in ipairs(df.global.world.raws.reactions.reactions) do
        if reaction.code == BREW_REACTION then reaction_index = index; break end
    end
    if reaction_index == nil then error('Native brew-from-plant reaction index is unavailable') end
    -- Build filters from DFHack's native raw-reaction adapter. No agent-supplied
    -- filters, unit assignments, inventory changes, labor bypass, or completion.
    local created = array()
    for _ = 1, args.quantity do
        local job = df.job:new()
        local linked = false
        local ok, err = pcall(function()
            job:assign(template.job_fields)
            job.pos = {x=building.centerx, y=building.centery, z=building.z}
            for _, filter in ipairs(template.items) do
                local copy = copyall(filter)
                copy.new = true
                -- workshops.getJobs() reindexes its filtered reaction list.
                -- df.job_item.reaction_id instead references the global raw
                -- reaction vector (df-structures 1dd01aad, df.job.xml:31).
                copy.reaction_id = reaction_index
                job.job_items.elements:insert('#', copy)
            end
            local ref = df.general_ref_building_holderst:new()
            ref.building_id = building.id
            job.general_refs:insert('#', ref)
            dfhack.job.linkIntoWorld(job, true)
            linked = true
            building.jobs:insert('#', job)
            table.insert(created, job.id)
        end)
        if not ok then
            if linked then
                dfhack.job.removeJob(job)
            else
                job:delete()
            end
            -- Roll back only this request's newly queued jobs, before any game tick.
            for _, id in ipairs(created) do
                local previous = df.job.find(id)
                if previous then dfhack.job.removeJob(previous) end
            end
            error('Could not queue native brewing job: ' .. tostring(err))
        end
    end
    for _, id in ipairs(created) do state.brew_jobs[id] = building.id end
    state.brew_job_count = state.brew_job_count + #created
    dfhack.job.checkBuildingsNow()
    return {workshop_id=building.id, job_ids=created, queued_jobs=#created,
            reaction=BREW_REACTION, reaction_index=reaction_index, completed=false}
end

local function finish_advance(state, ok, message)
    local active = state.active
    if not active then return end
    state.active = nil
    df.global.pause_state = true
    if active.timer_id and dfhack.timeout_active then dfhack.timeout_active(active.timer_id, nil) end
    if ok and absolute_tick() ~= active.target_tick then
        ok, message = false, 'Game clock did not stop at the exact requested tick; game paused'
    end
    if not ok then
        if state.speed and state.speed.override_active then
            state.speed.restore_reason = 'advance_failed'
            local restored, why = restore_speed(state)
            message = tostring(message) .. (restored and '; original simulation cap restored'
                or '; simulation cap restore failed: ' .. tostring(why))
        end
        respond(state, active.request, false, message)
        return
    end
    local result = status(state)
    result.requested_ticks = active.request.args.ticks
    result.start_absolute_tick = active.start_tick
    result.elapsed_ticks = absolute_tick() - active.start_tick
    result.overshoot_ticks = math.max(0, result.elapsed_ticks - result.requested_ticks)
    respond(state, active.request, true, result)
end

local function schedule_advance_boundary(state, active)
    -- LuaTools.cpp 1981/2087 schedules and dispatches against world.frame_counter.
    -- One native target timer per advance replaces a per-tick Lua timer chain.
    -- Raw-frame polling remains responsible for cancellation and wall watchdogs.
    local scheduled, timer_id = pcall(dfhack.timeout, active.request.args.ticks, 'ticks', function()
        if live_state ~= state or state.active ~= active then return end
        local ok, err = pcall(function()
            if not dfhack.isMapLoaded() or df.global.world.world_data ~= active.world_data then
                finish_advance(state, false, 'World or map changed at advance boundary; game paused')
            elseif absolute_tick() ~= active.target_tick then
                finish_advance(state, false, 'Native tick counter and calendar diverged; game paused')
            else
                finish_advance(state, true)
            end
        end)
        if not ok then
            df.global.pause_state = true
            state.active = nil
            if state.speed and state.speed.override_active then
                state.speed.restore_reason = 'boundary_error'
                restore_speed(state)
            end
            dfhack.printerr('dfeval-live advance boundary: ' .. tostring(err))
        end
    end)
    if not scheduled or timer_id == nil then
        state.active = nil
        error('Cannot schedule native tick boundary; game remains paused: ' .. tostring(timer_id))
    end
    active.timer_id = timer_id
end

local function poll(state)
    if live_state ~= state then return end
    if state.speed and state.speed.override_active then
        local reason
        if not dfhack.isMapLoaded() or df.global.world.world_data ~= state.speed.world_data then
            reason = 'world_or_map_changed'
        elseif state.speed.request_seq and dfhack.filesystem.isfile(
            state.directory .. '/cancel.' .. state.speed.request_seq .. '.json') then
            reason = 'client_cancelled_speed_request'
        elseif state.speed.last_advance_seq and dfhack.filesystem.isfile(
            state.directory .. '/cancel.' .. state.speed.last_advance_seq .. '.json') then
            reason = 'client_cancelled_advance'
        end
        if reason then
            df.global.pause_state = true
            state.speed.restore_reason = reason
            local restored, why = restore_speed(state)
            if not restored then dfhack.printerr('dfeval-live speed restore: ' .. tostring(why)) end
            if state.active then finish_advance(state, false, reason .. '; game paused') end
        end
    end
    if state.active then
        local active = state.active
        active.frames = active.frames + 1
        if dfhack.filesystem.isfile(state.directory .. '/cancel.' .. active.request.seq .. '.json') then
            finish_advance(state, false, 'Client cancelled; game paused')
        elseif not dfhack.isMapLoaded() or df.global.world.world_data ~= active.world_data then
            finish_advance(state, false, 'World or map changed during advance; game paused')
        elseif absolute_tick() < active.start_tick then
            finish_advance(state, false, 'Game clock moved backwards; game paused')
        elseif absolute_tick() >= active.target_tick then
            finish_advance(state, true)
        elseif dfhack.getTickCount() - active.start_ms >= active.request.timeout_ms or active.frames >= 12000 then
            finish_advance(state, false, 'Advance watchdog expired; game paused (possibly a blocking game screen)')
        end
    else
        local path = state.directory .. '/request.json'
        if dfhack.filesystem.isfile(path) then
            local decoded, req = pcall(json.decode_file, path)
            if decoded and type(req) == 'table' and type(req.seq) == 'number'
                    and req.seq > state.last_sequence then
                local ok, result = pcall(function()
                    validate(req, state)
                    if dfhack.filesystem.isfile(state.directory .. '/cancel.' .. req.seq .. '.json') then
                        error('Client cancelled request before execution')
                    end
                    if req.op == 'status' then return status(state) end
                    if req.op == 'observe' then return observe(state) end
                    if req.op == 'pause' then
                        require_fortress()
                        df.global.pause_state = true
                        return status(state)
                    end
                    if req.op == 'restore_simulation_fps' then
                        local restored, why = restore_speed(state)
                        local result = status(state)
                        result.restored = restored
                        if why then result.restore_error = why end
                        return result
                    end
                    if req.op == 'set_simulation_fps' then
                        require_fortress()
                        set_speed(state, req.args.fps, req)
                        return status(state)
                    end
                    if req.op == 'queue_brew' then return queue_brew(state, req.args) end
                    require_fortress()
                    if df.global.pause_state ~= true then error('Pause the game before advancing') end
                    local now = absolute_tick()
                    state.active = {request=req, start_tick=now, target_tick=now + req.args.ticks,
                        start_ms=dfhack.getTickCount(), frames=0, world_data=df.global.world.world_data}
                    if state.speed then state.speed.last_advance_seq = req.seq end
                    schedule_advance_boundary(state, state.active)
                    df.global.pause_state = false
                end)
                -- Never execute a sequence twice, including rejected mutations.
                state.last_sequence = req.seq
                if not ok and req.op == 'advance_ticks' and state.speed and state.speed.override_active then
                    df.global.pause_state = true
                    state.speed.restore_reason = 'advance_rejected'
                    local restored, why = restore_speed(state)
                    if not restored then result = tostring(result) .. '; restore failed: ' .. tostring(why) end
                end
                if not state.active then respond(state, req, ok, result) end
            end
        end
    end
end

local command, session = ...
if command ~= 'start' or type(session) ~= 'string' or #session ~= 32 or session:find('[^0-9a-f]') then
    qerror('Usage (trusted host only): dfeval-live start <32 lowercase hex session>')
end
local directory = dfhack.getDFPath() .. '/dfhack-config/dfeval-live/' .. session
if not dfhack.filesystem.isdir(directory) then qerror('IPC session directory does not exist') end
-- A second client must not interrupt an advance, including a restart of the
-- same session. Reject before changing the active owner or the game's pause
-- state so that a read-only snapshot cannot silently alter the running game.
if live_state and live_state.active then
    qerror('Cannot start a bridge session while an advance is active; wait for it to finish')
end
-- An idle superseded session may still own a temporary cap. Restore before
-- capturing the new session's original value; refuse takeover if restoration fails.
if live_state and live_state.speed and live_state.speed.override_active then
    local restored, why = restore_speed(live_state)
    if not restored then qerror('Cannot replace bridge session: simulation cap restore failed: ' .. tostring(why)) end
end
-- Requests already present when the trusted host starts/restarts a script are
-- never executed again. The client must start first, then publish fresh work.
local last_sequence = 0
local existing_path = directory .. '/request.json'
if dfhack.filesystem.isfile(existing_path) then
    local ok, existing = pcall(json.decode_file, existing_path)
    if ok and type(existing) == 'table' and type(existing.seq) == 'number' then
        last_sequence = existing.seq
    end
end
local state = {session=session, directory=directory, last_sequence=last_sequence, known_citizens={},
    world_data=df.global.world.world_data}
capture_speed(state)
reset_brewing(state)
live_state = state
install_product_tracking(state)
local function tick()
    if live_state ~= state then return end
    local ok, err = pcall(poll, state)
    if not ok then
        if state.active then df.global.pause_state = true; state.active = nil end
        if state.speed and state.speed.override_active then
            df.global.pause_state = true
            state.speed.restore_reason = 'poll_error'
            restore_speed(state)
        end
        dfhack.printerr('dfeval-live: ' .. tostring(err))
    end
    -- Raw frames continue while paused and at menus, unlike simulation ticks.
    if live_state == state then dfhack.timeout(1, 'frames', tick) end
end
dfhack.timeout(1, 'frames', tick)
print('dfeval-live ready: ' .. session)
