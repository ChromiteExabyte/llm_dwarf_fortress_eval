-- dfeval-bridge.lua - resident bridge between a running Dwarf Fortress and the
-- dfeval harness.
--
-- Install:  copy into  <DF>/hack/scripts/dfeval-bridge.lua
-- Start:    in the DFHack console,  dfeval-bridge start
-- Stop:     dfeval-bridge stop
--
-- Transport is a pair of JSON files under <DF>/dfeval/. The script polls for a
-- request whose `seq` it has not served yet, serves it, and writes the reply.
-- Files rather than a socket because DFHack's Lua has no portable async socket
-- and because a file-based exchange survives the game being paused, saved, or
-- alt-tabbed - which, over a four-year fortress, it will be.
--
-- The same approach is used by xuruiyang/df-ai-agent, which is where the
-- pattern is borrowed from.
--
-- ---------------------------------------------------------------------------
-- VERIFY ON HARDWARE. The harness has been exercised end to end against the
-- mock bridge only. The calls below are written against the documented DFHack
-- Lua API but the DF 50.x surface moves, and three areas are known forks:
--
--   * labour assignment. Pre-v50 used unit.status.labors[]; v50 moved most of
--     it to plotinfo.labor_info.work_details. Both are attempted, in that
--     order, and `capabilities.labor` reports which one took.
--   * item counting. Walking world.items.all and switching on getType() is
--     slow on a large fortress but stable across versions; the faster
--     items.other[] buckets are version-sensitive. Slow and stable wins here.
--   * announcements. Field names on the announcement struct have changed
--     before. Guarded by pcall; if it fails the harness simply gets no
--     narration, which degrades the observation but not the ledger.
--
-- Every capability is probed at connect and reported. A verb the installed
-- DFHack cannot do returns a structured error the agent can read, rather than
-- failing silently - an eval that quietly drops half the action space would
-- produce numbers about the bridge instead of the model.
-- ---------------------------------------------------------------------------

local json = require('json')
local utils = require('utils')

local BRIDGE_DIR = 'dfeval'
local REQ = BRIDGE_DIR .. '/request.json'
local RES = BRIDGE_DIR .. '/response.json'
local POLL_FRAMES = 10

bridge_state = bridge_state or { running = false, last_seq = -1 }

local function ensure_dir()
    if not dfhack.filesystem.isdir(BRIDGE_DIR) then
        dfhack.filesystem.mkdir(BRIDGE_DIR)
    end
end

local function try(fn, ...)
    local ok, res = pcall(fn, ...)
    if ok then return res end
    return nil
end

-- -- capability probe -------------------------------------------------------

local function capabilities()
    local caps = {}
    caps.units = (try(function() return #df.global.world.units.active end) or 0) > 0
    caps.announcements = try(function()
        return df.global.world.status.announcements ~= nil
    end) == true
    caps.items = try(function() return df.global.world.items.all ~= nil end) == true
    caps.work_details = try(function()
        return df.global.plotinfo.labor_info.work_details ~= nil
    end) == true
    caps.classic_labors = try(function()
        local u = df.global.world.units.active[0]
        return u.status.labors ~= nil
    end) == true
    caps.labor = caps.work_details and 'work_details'
        or (caps.classic_labors and 'classic' or 'none')
    caps.buildings = try(function() return dfhack.buildings.constructBuilding ~= nil end) == true
    caps.designations = try(function() return dfhack.maps.getTileBlock ~= nil end) == true
    caps.df_version = try(function() return dfhack.getDFVersion() end) or 'unknown'
    caps.dfhack_version = try(function() return dfhack.getDFHackVersion() end) or 'unknown'
    return caps
end

-- -- observation -----------------------------------------------------------

local function unit_name(u)
    local n = try(dfhack.units.getReadableName, u)
    if n then return n end
    n = try(function() return dfhack.TranslateName(dfhack.units.getVisibleName(u)) end)
    return n or ('unit-' .. tostring(u.id))
end

local function unit_wounds(u)
    local n = 0
    local parts = try(function() return u.body.components.body_part_status end)
    if parts then
        for _, p in ipairs(parts) do
            if p.whole_broken or p.severed or p.motor_nerve_severed then n = n + 1 end
        end
    end
    return n
end

local function skills_of(u)
    local out = {}
    local soul = try(function() return u.status.current_soul end)
    if not soul then return out end
    for _, sk in ipairs(soul.skills) do
        local name = try(function() return df.job_skill[sk.id] end)
        if name then out[tostring(name)] = sk.rating end
    end
    return out
end

local function happiness_of(u)
    -- DF's own stress value. Negative is content, positive is stressed - the
    -- harness flips it into its own -1..1 mood scale on the Python side.
    return try(function() return u.status.current_soul.personality.stress end) or 0
end

local function collect_units()
    local citizens, dead = {}, {}
    for _, u in ipairs(df.global.world.units.active) do
        local is_citizen = try(dfhack.units.isCitizen, u)
        local alive = try(dfhack.units.isAlive, u)
        local rec = {
            id = u.id,
            name = unit_name(u),
            profession = try(dfhack.units.getProfessionName, u) or 'dwarf',
            age = math.floor(try(dfhack.units.getAge, u) or 0),
            child = try(dfhack.units.isChild, u) == true,
            alive = alive == true,
            wounds = unit_wounds(u),
            stress = happiness_of(u),
            skills = skills_of(u),
            citizen = is_citizen == true,
        }
        if is_citizen and alive then
            table.insert(citizens, rec)
        elseif is_citizen then
            table.insert(dead, rec)
        end
    end
    return citizens, dead
end

local FOOD_TYPES = { 'FOOD', 'MEAT', 'PLANT', 'FISH', 'CHEESE', 'EGG' }

local function collect_stocks()
    local s = { food = 0, drink = 0, wood = 0, stone = 0, cloth = 0, medicine = 0 }
    local ok = pcall(function()
        for _, item in ipairs(df.global.world.items.all) do
            if not item.flags.dead_dwarf and not item.flags.garbage_collect then
                local t = df.item_type[item:getType()]
                for _, ft in ipairs(FOOD_TYPES) do
                    if t == ft then s.food = s.food + 1 end
                end
                if t == 'DRINK' then s.drink = s.drink + 1
                elseif t == 'WOOD' then s.wood = s.wood + 1
                elseif t == 'BOULDER' or t == 'BLOCKS' then s.stone = s.stone + 1
                elseif t == 'CLOTH' then s.cloth = s.cloth + 1
                elseif t == 'POWDER_MISC' then s.medicine = s.medicine + 1
                end
            end
        end
    end)
    s.counted = ok
    return s
end

local function collect_announcements(since_year, since_tick)
    local out = {}
    pcall(function()
        local anns = df.global.world.status.announcements
        local first = math.max(0, #anns - 60)
        for i = first, #anns - 1 do
            local a = anns[i]
            if a.year > since_year or (a.year == since_year and a.time >= since_tick) then
                table.insert(out, a.text)
            end
        end
    end)
    return out
end

local function observe(args)
    local citizens, dead = collect_units()
    local caps = capabilities()
    return {
        year = df.global.cur_year,
        tick = df.global.cur_year_tick,
        -- DF's year is 403200 ticks; 12 months of 33600.
        month = math.floor(df.global.cur_year_tick / 33600),
        fortress = try(function()
            return dfhack.TranslateName(df.global.world.world_data.active_site[0].name)
        end) or 'fortress',
        citizens = citizens,
        dead = dead,
        stocks = collect_stocks(),
        announcements = collect_announcements(args.since_year or 0, args.since_tick or 0),
        capabilities = caps,
    }
end

-- -- actions ---------------------------------------------------------------

local function find_unit(id_or_name)
    for _, u in ipairs(df.global.world.units.active) do
        if u.id == id_or_name or unit_name(u) == id_or_name then return u end
    end
    return nil
end

local function set_labor(args)
    local u = find_unit(args.dwarf)
    if not u then return { ok = false, error = 'no such dwarf: ' .. tostring(args.dwarf) } end
    local labor = df.unit_labor[args.labor]
    if labor == nil then return { ok = false, error = 'unknown labor: ' .. tostring(args.labor) } end
    local ok = pcall(function() u.status.labors[labor] = args.enabled ~= false end)
    if ok then return { ok = true, via = 'classic' } end
    -- v50 work details. Named detail must already exist in the fortress.
    local ok2 = pcall(function()
        for _, wd in ipairs(df.global.plotinfo.labor_info.work_details) do
            if wd.name == args.labor then
                utils.insert_or_update(wd.assigned_units, u.id)
                return
            end
        end
        error('no work detail named ' .. tostring(args.labor))
    end)
    if ok2 then return { ok = true, via = 'work_details' } end
    return { ok = false, error = 'labour assignment unsupported on this build' }
end

local function run_command(args)
    -- The escape hatch. Anything DFHack itself can do, the harness can ask for
    -- by name. Everything that arrives this way is marked `spontaneous` in the
    -- ledger, because it did not come from the harness's own vocabulary.
    local out = {}
    local ok, err = pcall(function()
        out = { dfhack.run_command_silent(table.unpack(args.argv or {})) }
    end)
    return { ok = ok, output = out[1], error = ok and nil or tostring(err) }
end

local function designate_dig(args)
    local ok, err = pcall(function()
        local pos = xyz2pos(args.x, args.y, args.z)
        local block = dfhack.maps.getTileBlock(pos)
        if not block then error('no map block at that position') end
        block.designation[args.x % 16][args.y % 16].dig =
            df.tile_dig_designation[args.mode or 'Default']
        block.flags.designated = true
    end)
    return { ok = ok, error = ok and nil or tostring(err) }
end

local function build(args)
    local ok, err = pcall(function()
        dfhack.buildings.constructBuilding{
            type = df.building_type[args.building],
            pos = xyz2pos(args.x, args.y, args.z),
        }
    end)
    return { ok = ok, error = ok and nil or tostring(err) }
end

local function pause(args)
    df.global.pause_state = args.paused ~= false
    return { ok = true, paused = df.global.pause_state }
end

local HANDLERS = {
    ping = function() return { ok = true, pong = true, capabilities = capabilities() } end,
    observe = function(a) return { ok = true, state = observe(a) } end,
    set_labor = set_labor,
    run_command = run_command,
    designate_dig = designate_dig,
    build = build,
    pause = pause,
}

-- -- loop -------------------------------------------------------------------

local function serve()
    if not bridge_state.running then return end

    local raw = nil
    pcall(function()
        local f = io.open(REQ, 'r')
        if f then raw = f:read('*a'); f:close() end
    end)

    if raw and #raw > 0 then
        local ok, req = pcall(json.decode, raw)
        if ok and req and req.seq and req.seq ~= bridge_state.last_seq then
            bridge_state.last_seq = req.seq
            local handler = HANDLERS[req.op or '']
            local reply
            if handler then
                local hok, hres = pcall(handler, req.args or {})
                reply = hok and hres or { ok = false, error = tostring(hres) }
            else
                reply = { ok = false, error = 'unknown op: ' .. tostring(req.op) }
            end
            reply.seq = req.seq
            local out = io.open(RES, 'w')
            if out then out:write(json.encode(reply)); out:close() end
        end
    end

    dfhack.timeout(POLL_FRAMES, 'frames', serve)
end

local args = { ... }
local cmd = args[1] or 'status'

if cmd == 'start' then
    ensure_dir()
    bridge_state.running = true
    bridge_state.last_seq = -1
    print('dfeval-bridge: serving on ' .. REQ)
    for k, v in pairs(capabilities()) do print('  ' .. k .. ' = ' .. tostring(v)) end
    serve()
elseif cmd == 'stop' then
    bridge_state.running = false
    print('dfeval-bridge: stopped')
else
    print('dfeval-bridge: ' .. (bridge_state.running and 'running' or 'stopped'))
end
