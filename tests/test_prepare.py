"""Operator fixture boundaries, with optional isolated Lua 5.3 execution.

The Lua runtime is loaded without DF/DFHack. Native objects are explicit fakes;
these checks do not claim a game run or validate engine memory layouts.
"""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/dfeval/bridge/lua/dfeval-prepare.lua"
SOURCE = SCRIPT.read_text(encoding="utf-8")
FUNCTIONS = SOURCE.split("local opts = parse({...})", 1)[0]


@pytest.fixture(scope="module")
def lua():
    runtime = pytest.importorskip("lupa.lua53", reason="Optional isolated Lua 5.3 test runtime is not installed")

    def run(source, *, execute=True):
        state = runtime.LuaRuntime()
        chunk = state.compile(source)
        if execute:
            chunk()

    return run


PRELUDE = """
package.preload['json'] = function() return {} end
package.preload['json.internal'] = function() return {newArray=function(_, x) return x end} end
package.preload['dfhack.buildings'] = function() return {} end
"""


def test_operator_script_has_no_general_command_or_gameplay_mutator():
    assert "dfhack.run_command(" not in SOURCE
    assert "dfhack.run_script(" not in SOURCE
    assert "queue_brew" not in SOURCE
    assert not re.search(r"(?:hunger_timer|thirst_timer|stress|focus_level|skill_rating)\s*=", SOURCE)
    assert not re.search(r"status\.labors\.\w+\s*=[^=]", SOURCE)
    assert "dfhack.units.create(" not in SOURCE
    assert not re.search(r"df\.global\.pause_state\s*=[^=]", SOURCE)


def test_audit_reservation_precedes_application_and_scored_bridge_has_no_setup_operation():
    assert SOURCE.index("io.open(output .. '.tmp', 'wb')") < SOURCE.index("apply(opts, worker, mats, report)",
                                                                                   SOURCE.index("local opts = parse"))
    bridge = (SCRIPT.parent / "dfeval-live.lua").read_text(encoding="utf-8")
    assert "dfeval-prepare" not in bridge
    assert "queued_brewing_jobs=0" in SOURCE


def test_lua_compiles_without_loading_df(lua):
    lua(SOURCE, execute=False)


def test_strict_operator_arguments(lua):
    lua(PRELUDE + FUNCTIONS + """
local opts = parse({'apply','--worker','662','--pos','1,2,3','--out','fixture-1'})
assert(opts.worker == 662 and opts.pos.z == 3)
for _, args in ipairs({
    {'run','--worker','1','--out','a'},
    {'apply','--worker','1','--out','a'},
    {'check','--worker','1','--out','../outside'},
    {'check','--worker','1','--out','a','--lua','anything'},
    {'check','--worker','1','--out','a','--worker','2'},
    {'check','--worker','1.5','--out','a'},
    {'check','--worker','2147483648','--out','a'},
    {'check','--worker','1','--out','a','--pos','-1,2,3'},
    {'check','--worker','1','--out',string.rep('a',65)},
}) do assert(not pcall(parse, args), 'Unsafe argument accepted') end
""")


MAP_FAKE = """
local flags = {hidden=false, flow_size=0, dig=0}
local occ = {building=0,item=0,unit=false,unit_grounded=false}
local zones
local walks = true
df = {tiletype={attrs={[1]={shape=1}}}, tiletype_shape={FLOOR=1},
      tile_dig_designation={No=0}, tile_building_occ={None=0}}
dfhack = {
 maps={isValidTilePos=function(p) return p.x >= 0 and p.y >= 0 end,
       getTileType=function() return 1 end,
       getTileFlags=function() return flags,occ end,
       canWalkBetween=function() return walks end},
 buildings={findAtTile=function() return nil end, findCivzonesAt=function() return zones end},
 constructions={findAtTile=function() return nil end}}
local worker = {pos={x=20,y=20,z=10}}
local pos = {x=30,y=30,z=10}
"""


def test_preflight_handles_nil_zones_and_refuses_occupied_or_disconnected_floor(lua):
    lua(PRELUDE + FUNCTIONS + MAP_FAKE + """
footprint(pos,worker)
for _, key in ipairs({'item','unit','unit_grounded','building'}) do
    local old=occ[key]; occ[key]=1
    assert(not pcall(footprint,pos,worker), key)
    occ[key]=old
end
flags.hidden=true; assert(not pcall(footprint,pos,worker)); flags.hidden=false
flags.flow_size=1; assert(not pcall(footprint,pos,worker)); flags.flow_size=0
flags.dig=1; assert(not pcall(footprint,pos,worker)); flags.dig=0
zones={{}}; assert(not pcall(footprint,pos,worker)); zones=nil
walks=false; assert(not pcall(footprint,pos,worker)); walks=true
df.tiletype.attrs[1].shape=2; assert(not pcall(footprint,pos,worker))
""")


def test_candidate_search_is_bounded_and_deterministic(lua):
    lua(PRELUDE + FUNCTIONS + MAP_FAKE + """
local first=candidates(worker)
assert(#first == 8 and first[1].x == 19 and first[1].y == 19)
local second=candidates(worker)
for i,p in ipairs(first) do assert(same_pos(p,second[i])) end
local calls=0
dfhack.maps.isValidTilePos=function() calls=calls+1; return false end
assert(#candidates(worker)==0 and calls==624)
""")


def test_preparation_refuses_active_advance_without_importing_or_pausing_bridge(lua):
    lua(PRELUDE + FUNCTIONS + """
local state={active={target_tick=9}}
df={global={pause_state=true,plotinfo={main={autosave_request=false}},cur_year=1,cur_year_tick=2,
            world={cur_savegame={save_dir='region-fixture'}}}}
dfhack={isWorldLoaded=function() return true end,isMapLoaded=function() return true end,
 world={isFortressMode=function() return true end},
 internal={scripts={bridge={env={live_state=state}}}},findScript=function() return 'bridge' end,
 getDFVersion=function() return '53.16' end,getDFHackVersion=function() return '53.16-r1.1' end,
 script_environment=function() error('Bridge import must not happen') end}
assert(not pcall(environment))
assert(df.global.pause_state==true and state.active.target_tick==9)
state.active=nil
assert(environment().absolute_tick==403202)
df.global.pause_state=false
assert(not pcall(environment) and df.global.pause_state==false)
""")


def test_disabled_or_busy_worker_is_rejected_without_rewriting_labor(lua):
    lua(PRELUDE + FUNCTIONS + """
local worker={status={labors={BREWER=false}},job={}}
local yes=function() return true end
df={unit={find=function() return worker end},unit_labor={BREWER=7}}
dfhack={units={isActive=yes,isCitizen=yes,isDead=function() return false end,
              isAdult=yes,isSane=yes,isValidLabor=yes}}
assert(not pcall(get_worker,662) and worker.status.labors.BREWER==false)
worker.status.labors.BREWER=true
assert(get_worker(662)==worker)
worker.job.current_job={}
assert(not pcall(get_worker,662) and worker.status.labors.BREWER==true)
""")


def test_existing_fixture_is_checked_without_replenishment(lua):
    lua(PRELUDE + FUNCTIONS + """
local pos={x=1,y=2,z=3}
local bld={x1=1,y1=2,z=3,jobs={},getType=function() return 1 end,getSubtype=function() return 2 end,
           getBuildStage=function() return 3 end,getMaxBuildStage=function() return 3 end}
local count=20
local item={pos=pos,flags={on_ground=true},getType=function() return 5 end,getStackSize=function() return count end}
local marker={state='complete',worker_id=662,position=pos,workshop_id=8,
 created_items={{id=9,role='brewable_plants',material='plump',item_type='PLANT',stack_size=20,position=pos}}}
df={building={find=function() return bld end},item={find=function() return item end},
    building_type={Workshop=1},workshop_type={Still=2},item_type={[5]='PLANT'}}
dfhack={matinfo={decode=function() return {getToken=function() return 'plump' end} end},
 items={createItem=function() error('Must not replenish') end}}
assert(validate_existing(marker,{worker=662,pos=pos})==marker)
count=19
assert(not pcall(validate_existing,marker,{worker=662,pos=pos}))
assert(count==19)
marker.state='failed'
assert(not pcall(validate_existing,marker,{worker=662,pos=pos}))
""")


def test_finalization_touches_exactly_the_created_building_and_material(lua):
    lua(PRELUDE + FUNCTIONS + """
local function vec(value) return setmetatable({[0]=value},{__len=function() return 1 end}) end
local block={id=42}
local job={job_type=5,flags={suspend=false},items=vec({item=block})}
local calls={}
local bld={id=9,jobs=vec(job),stage=0}
function bld:needsDesign() return false end
function bld:getBuildStage() return self.stage end
function bld:getMaxBuildStage() return 3 end
function bld:setBuildStage(stage) self.stage=stage; table.insert(calls,'stage') end
df={job_type={ConstructBuilding=5},global={world={}}}
dfhack={job={removeJob=function(value) assert(value==job); bld.jobs={}; table.insert(calls,'remove'); return true end},
 items={moveToBuilding=function(item,building,mode) assert(item==block and building==bld and mode==2); table.insert(calls,'attach'); return true end},
 buildings={completeBuild=function(building) assert(building==bld); table.insert(calls,'complete') end}}
finish_own_building(bld,block)
assert(table.concat(calls,',')=='remove,attach,stage,complete')
assert(df.global.world.reindex_pathfinding==true)
bld.jobs=vec(job); calls={}
assert(not pcall(finish_own_building,bld,{id=99}))
assert(#calls==0)
""")


def test_failure_cleanup_never_removes_attached_or_unrelated_items(lua):
    lua(PRELUDE + FUNCTIONS + """
local items={[1]={id=1,flags={}},[2]={id=2,flags={in_building=true}},[3]={id=3,flags={in_job=true}},
             [999]={id=999,flags={}}}
local removed={}
local bld={getBuildStage=function() return 3 end}
df={item={find=function(id) return items[id] end},building={find=function() return bld end}}
dfhack={items={remove=function(item) table.insert(removed,item.id) end},
        buildings={deconstruct=function() error('Must not deconstruct a completed still') end}}
local result=cleanup_failed({workshop_id=7,created_items={{id=1},{id=2},{id=3}}})
assert(#removed==1 and removed[1]==1)
assert(#result.retained_item_ids==2 and result.retained_workshop_id==7)
assert(items[999].id==999)
""")
