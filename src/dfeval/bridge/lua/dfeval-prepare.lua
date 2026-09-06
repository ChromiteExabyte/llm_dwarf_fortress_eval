-- Trusted operator setup only. Never an operation of the evaluated model bridge.
-- Source-reviewed for DFHack 53.16-r1.1; native validation is still required.
-- Construction finalization follows DFHack/scripts build-now.lua at
-- 7549711a993e03bef19e90b27427096c1099853e, restricted to our own new building.
local json = require('json')
local json_types = require('json.internal')
require('dfhack.buildings')
local KEY = 'dfeval/fixture/brewing-v1'
local LABEL = 'DFEval brewing fixture v1'
local function array() return json_types:newArray{} end
local function fail(message) error(message, 0) end
local function need(condition, message) if not condition then fail(message) end end
local function nonzero(value) return value ~= nil and value ~= false and value ~= 0 end
local function copy_pos(p) return {x=p.x, y=p.y, z=p.z} end
local function same_pos(a, b) return a.x == b.x and a.y == b.y and a.z == b.z end

local function parse(args)
    local opts = {command=args[1]}
    need(opts.command == 'check' or opts.command == 'apply',
        'Usage: dfeval-prepare check|apply --worker ID [--pos X,Y,Z] --out TOKEN')
    local allowed = {['--worker']='worker', ['--pos']='pos', ['--out']='out'}
    local i = 2
    while i <= #args do
        local key = allowed[args[i]]
        need(key and opts[key] == nil and args[i+1], 'Unknown, duplicate, or incomplete option')
        opts[key] = args[i+1]
        i = i + 2
    end
    need(type(opts.worker) == 'string' and opts.worker:match('^%d+$'), 'Worker must be an integer ID')
    opts.worker = tonumber(opts.worker)
    need(opts.worker <= 2147483647, 'Worker ID is out of range')
    need(type(opts.out) == 'string' and #opts.out <= 64 and opts.out:match('^[a-z0-9][a-z0-9%-]*$'),
        'Output must be a new lowercase alphanumeric/hyphen token (maximum 64 characters)')
    if opts.pos then
        local x, y, z = opts.pos:match('^(%d+),(%d+),(%d+)$')
        need(x and #x <= 5 and #y <= 5 and #z <= 5, 'Position must be X,Y,Z with nonnegative integers')
        opts.pos = {x=tonumber(x), y=tonumber(y), z=tonumber(z)}
    end
    need(opts.command ~= 'apply' or opts.pos, 'Apply requires the explicit --pos returned by check')
    return opts
end

local function environment()
    need(dfhack.isWorldLoaded() and dfhack.isMapLoaded() and dfhack.world.isFortressMode(),
        'A loaded fortress map is required')
    need(df.global.pause_state == true, 'Pause the game before operator setup')
    need(not df.global.plotinfo.main.autosave_request, 'Wait until autosaving has finished')
    -- Read the existing environment without importing/running the live script.
    -- script_environment() can execute the bridge on first import, so avoid it.
    local scripts = dfhack.internal.scripts
    need(type(scripts) == 'table', 'Cannot inspect active bridge ownership on this DFHack version')
    local path = dfhack.findScript('dfeval-live')
    local record = path and scripts[path]
    local state = record and record.env and record.env.live_state
    need(not (state and state.active), 'An advance is active; operator setup is forbidden')
    return {df_version=dfhack.getDFVersion(), dfhack_version=dfhack.getDFHackVersion(),
        save_directory=df.global.world.cur_savegame.save_dir,
        absolute_tick=df.global.cur_year * 403200 + df.global.cur_year_tick, paused=true}
end

local function get_worker(id)
    local unit = df.unit.find(id)
    need(unit and dfhack.units.isActive(unit) and dfhack.units.isCitizen(unit)
        and not dfhack.units.isDead(unit) and dfhack.units.isAdult(unit)
        and dfhack.units.isSane(unit), 'Worker must be an active, sane, adult fortress citizen')
    need(dfhack.units.isValidLabor(unit, df.unit_labor.BREWER), 'Brewing is not a valid labor for this worker')
    need(unit.status.labors.BREWER == true,
        'Choose a worker with BREWER already enabled; setup does not rewrite work details or labors')
    need(not unit.job.current_job, 'Worker must be idle at fixture preparation')
    return unit
end

local function resource_pos(pos) return {x=pos.x+3, y=pos.y+1, z=pos.z} end

local function footprint(pos, worker)
    -- Four by three clear floor tiles: the still is the western 3x3; supplies
    -- sit at the center of the eastern column. No terrain/item/unit relocation.
    for x=pos.x,pos.x+3 do
        for y=pos.y,pos.y+2 do
            local tile = {x=x, y=y, z=pos.z}
            need(dfhack.maps.isValidTilePos(tile), 'Footprint is outside the map')
            local tt = dfhack.maps.getTileType(tile)
            local flags, occ = dfhack.maps.getTileFlags(tile)
            need(tt and flags and occ, 'Footprint has an unloaded tile')
            need(df.tiletype.attrs[tt].shape == df.tiletype_shape.FLOOR,
                'Footprint requires flat floor tiles')
            need(not flags.hidden and flags.flow_size == 0, 'Footprint must be visible and dry')
            need(flags.dig == df.tile_dig_designation.No, 'Footprint has a digging designation')
            need(occ.building == df.tile_building_occ.None and not nonzero(occ.item)
                and not nonzero(occ.unit) and not nonzero(occ.unit_grounded),
                'Footprint is occupied by a building, item, or unit')
            need(not dfhack.buildings.findAtTile(tile) and not dfhack.constructions.findAtTile(tile),
                'Footprint contains a building or construction')
            local zones = dfhack.buildings.findCivzonesAt(tile)
            need(not zones or #zones == 0, 'Footprint overlaps a zone')
            need(dfhack.maps.canWalkBetween(worker.pos, tile), 'Footprint is not in the worker walkability group')
        end
    end
end

local function materials()
    local out = {}
    for key, token in pairs({block='INORGANIC:GRANITE', plant='PLANT_MAT:MUSHROOM_HELMET_PLUMP:STRUCTURAL',
                            barrel='PLANT_MAT:OAK:WOOD'}) do
        local mat = dfhack.matinfo.find(token)
        need(mat, 'Required fixture material is unavailable: ' .. token)
        out[key] = mat
    end
    local reaction
    for _, raw in ipairs(df.global.world.raws.reactions.reactions) do
        if raw.code == 'BREW_DRINK_FROM_PLANT' then reaction = raw; break end
    end
    need(reaction, 'Native BREW_DRINK_FROM_PLANT reaction is missing')
    return out
end

local function candidates(worker)
    local out = array()
    -- Fixed bounded search, nearest Chebyshev ring first; deterministic x/y order.
    for radius=1,12 do
        for dx=-radius,radius do
            for dy=-radius,radius do
                if math.max(math.abs(dx), math.abs(dy)) == radius then
                    local pos = {x=worker.pos.x+dx, y=worker.pos.y+dy, z=worker.pos.z}
                    if pcall(footprint, pos, worker) then
                        table.insert(out, pos)
                        if #out == 8 then return out end
                    end
                end
            end
        end
    end
    return out
end

local function item_record(item, role)
    return {id=item.id, role=role, item_type=df.item_type[item:getType()],
        material=dfhack.matinfo.decode(item):getToken(), stack_size=item:getStackSize(),
        position=copy_pos(item.pos)}
end

local function validate_existing(marker, opts)
    need(marker.state == 'complete', 'A previous preparation is incomplete; restore the preserved checkpoint')
    need(marker.worker_id == opts.worker, 'Existing fixture belongs to a different worker')
    need(not opts.pos or same_pos(marker.position, opts.pos), 'Existing fixture has different coordinates')
    local bld = df.building.find(marker.workshop_id)
    need(bld and bld:getType() == df.building_type.Workshop and bld:getSubtype() == df.workshop_type.Still
        and bld.x1 == marker.position.x and bld.y1 == marker.position.y and bld.z == marker.position.z
        and bld:getBuildStage() == bld:getMaxBuildStage() and #bld.jobs == 0,
        'Existing fixture building has changed; restore the preserved checkpoint')
    for _, saved in ipairs(marker.created_items) do
        local item = df.item.find(saved.id)
        need(item and not item.flags.removed and not item.flags.garbage_collect
            and df.item_type[item:getType()] == saved.item_type
            and dfhack.matinfo.decode(item):getToken() == saved.material
            and item:getStackSize() == saved.stack_size,
            'Existing fixture item is missing or changed; restore the preserved checkpoint')
        if saved.role ~= 'building_material' then
            need(item.flags.on_ground and not item.flags.in_job and same_pos(item.pos, saved.position),
                'Existing fixture supplies moved or are in use; restore the preserved checkpoint')
            if saved.role == 'empty_barrel' then
                need(#dfhack.items.getContainedItems(item) == 0, 'Existing fixture barrel is no longer empty')
            end
        else
            need(item.flags.in_building and dfhack.items.getHolderBuilding(item) == bld,
                'Existing fixture building material has changed')
        end
    end
    return marker
end

local function make_item(worker, item_type, mat, count, pos, role, marker)
    local created = dfhack.items.createItem(worker, item_type, -1, mat.type, mat.index)
    -- Register all returned IDs before checking cardinality, so cleanup remains
    -- restricted to items this invocation actually created even on API mismatch.
    for _, item in ipairs(created or {}) do
        table.insert(marker.created_items, item_record(item, role))
    end
    need(created and #created == 1, 'Native item creation returned an unexpected number of items')
    local item = created[1]
    item:setStackSize(count)
    need(dfhack.items.moveToGround(item, pos), 'Cannot place a newly created fixture item')
    marker.created_items[#marker.created_items] = item_record(item, role)
    dfhack.persistent.saveSiteData(KEY, marker)
    return item
end

local function finish_own_building(bld, block)
    -- Pinned build-now.lua algorithm on exactly one new still. Do not invoke
    -- build-now itself: it also cycles buildingplan and may globally unsuspend.
    need(#bld.jobs == 1, 'New still has an unexpected construction-job count')
    local job = bld.jobs[0]
    need(job and job.job_type == df.job_type.ConstructBuilding and not job.flags.suspend,
        'New still has no unsuspended construction job')
    need(#job.items == 1 and job.items[0].item.id == block.id,
        'Construction job did not attach exactly our new block')
    need(dfhack.job.removeJob(job), 'Could not remove the fixture construction job')
    need(dfhack.items.moveToBuilding(block, bld, 2), 'Could not attach the fixture block to the still')
    if bld:needsDesign() then
        bld.design.flags.built = true
        bld.design.hitpoints = 80640
        bld.design.max_hitpoints = 80640
    end
    bld:setBuildStage(bld:getMaxBuildStage())
    dfhack.buildings.completeBuild(bld)
    df.global.world.reindex_pathfinding = true
    need(bld:getBuildStage() == bld:getMaxBuildStage() and #bld.jobs == 0,
        'Fixture still did not finish construction')
end

local function cleanup_failed(marker)
    local cleanup = {removed_loose_item_ids=array(), retained_item_ids=array(), errors=array()}
    if marker.workshop_id then
        local bld = df.building.find(marker.workshop_id)
        if bld and bld:getBuildStage() == 0 then
            local ok, removed = pcall(dfhack.buildings.deconstruct, bld)
            cleanup.unbuilt_workshop_removed = ok and removed == true
            if not ok then table.insert(cleanup.errors, tostring(removed)) end
        end
        cleanup.retained_workshop_id = df.building.find(marker.workshop_id) and marker.workshop_id or nil
    end
    for _, record in ipairs(marker.created_items) do
        local item = df.item.find(record.id)
        if item then
            if item.flags.in_building or item.flags.in_job then
                table.insert(cleanup.retained_item_ids, item.id)
            else
                local ok, err = pcall(dfhack.items.remove, item)
                if ok then table.insert(cleanup.removed_loose_item_ids, record.id)
                else table.insert(cleanup.errors, tostring(err)) end
            end
        end
    end
    return cleanup
end

local function apply(opts, worker, mats, report)
    local marker = {schema_version=1, fixture='brewing-v1', label=LABEL, state='applying',
        worker_id=worker.id, position=copy_pos(opts.pos), supplies_position=resource_pos(opts.pos),
        created_items=array(), setup_status=report.native,
        worker_labor={name='BREWER', before=true, after=true, changed=false},
        care_values_modified=false, queued_brewing_jobs=0}
    report.fixture = marker
    dfhack.persistent.saveSiteData(KEY, marker)
    local ok, err = pcall(function()
        local block = make_item(worker, df.item_type.BLOCKS, mats.block, 1,
            marker.supplies_position, 'building_material', marker)
        make_item(worker, df.item_type.PLANT, mats.plant, 20,
            marker.supplies_position, 'brewable_plants', marker)
        for _=1,4 do
            make_item(worker, df.item_type.BARREL, mats.barrel, 1,
                marker.supplies_position, 'empty_barrel', marker)
        end
        local bld, why = dfhack.buildings.constructBuilding{
            type=df.building_type.Workshop, subtype=df.workshop_type.Still,
            pos=opts.pos, width=3, height=3, full_rectangle=true, items={block}}
        need(bld, 'Could not construct the new still: ' .. tostring(why))
        marker.workshop_id = bld.id
        dfhack.persistent.saveSiteData(KEY, marker)
        finish_own_building(bld, block)
        marker.state = 'complete'
        dfhack.persistent.saveSiteData(KEY, marker)
        validate_existing(marker, opts)
    end)
    if not ok then
        marker.state, marker.error = 'failed', tostring(err)
        local cleaned, cleanup = pcall(cleanup_failed, marker)
        marker.cleanup = cleaned and cleanup or {error=tostring(cleanup), incomplete=true}
        pcall(dfhack.persistent.saveSiteData, KEY, marker)
        fail('Preparation failed: ' .. tostring(err) .. '; see the setup record and restore the checkpoint')
    end
end

local opts = parse({...})
local directory = dfhack.getDFPath() .. '/dfhack-config/dfeval-setup'
local output = directory .. '/' .. opts.out .. '.json'
need(not dfhack.filesystem.exists(output) and not dfhack.filesystem.exists(output .. '.tmp'),
    'Output token already exists; use a new token')
need(dfhack.filesystem.mkdir_recursive(directory), 'Cannot create setup record directory')
-- Reserve and prove the audit output is writable before mutating native state.
local handle = assert(io.open(output .. '.tmp', 'wb'))
local report = {schema_version=1, kind='operator_fixture_preparation', command=opts.command,
    fixture_spec='brewing-v1', label=LABEL, evaluated_model_phase=false,
    native_validated=false, notes=array()}
table.insert(report.notes, 'Setup is outside the scored model phase; no brewing job is queued.')
table.insert(report.notes, 'Walkability groups do not establish actual worker path access, burrow access, or job completion.')
local ok, err = pcall(function()
    report.native = environment()
    local worker = get_worker(opts.worker)
    report.worker = {id=worker.id, position=copy_pos(worker.pos), brewing_labor_enabled=true, labor_changed=false}
    local mats = materials()
    local existing = dfhack.persistent.getSiteData(KEY)
    if existing then
        report.fixture = validate_existing(existing, opts)
        report.outcome = 'existing_unchanged'
        return
    end
    if opts.pos then footprint(opts.pos, worker) end
    if opts.command == 'check' then
        report.candidates = opts.pos and array() or candidates(worker)
        if opts.pos then table.insert(report.candidates, opts.pos) end
        need(#report.candidates > 0, 'No clear reachable 4x3 floor footprint found within radius 12')
        report.outcome = 'preflight_passed'
    else
        apply(opts, worker, mats, report)
        report.outcome = 'prepared'
    end
    need(df.global.pause_state == true, 'Unexpected pause-state change during setup')
    need(df.global.cur_year * 403200 + df.global.cur_year_tick == report.native.absolute_tick,
        'Unexpected simulation advancement during setup')
end)
report.ok = ok
if not ok then report.error = tostring(err) end
local encoded = json.encode(report)
assert(handle:write(encoded))
assert(handle:close())
assert(os.rename(output .. '.tmp', output))
if not ok then qerror(report.error) end
print('dfeval-prepare ' .. report.outcome .. ': ' .. output)
