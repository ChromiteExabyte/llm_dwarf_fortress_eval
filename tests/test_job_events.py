"""Execute the shipped Lua in Lua 5.4 with fake DF/eventful objects.

These are runtime tests of instrumentation and job construction, not evidence of
real DF production. No game process, files, network, or model provider is used.
"""

import copy
from pathlib import Path

from lupa.lua54 import LuaRuntime, lua_type
import pytest

from dfeval.care import summarize_brewing, summarize_events


SCRIPT = Path(__file__).parents[1] / "src/dfeval/bridge/lua/dfeval-live.lua"
SESSION = "a" * 32

FAKE_DF = r'''
local normal_ipairs = ipairs
function ipairs(value)
    if type(value) == 'table' and value._dfvec then
        local index = -1
        return function()
            index = index + 1
            if index < value._size then return index, value[index] end
        end
    end
    return normal_ipairs(value)
end
function vector(values)
    local result = {_dfvec=true, _size=0}
    setmetatable(result, {
        __len=function(self) return self._size end,
        __index={insert=function(self, position, item)
            assert(position == '#')
            self[self._size] = item
            self._size = self._size + 1
        end},
    })
    for _, value in normal_ipairs(values or {}) do result:insert('#', value) end
    return result
end
function copyall(value)
    if type(value) ~= 'table' then return value end
    local copied = {}
    for key, child in pairs(value) do copied[key] = copyall(child) end
    return copied
end
package.preload['json.internal'] = function()
    return {newArray=function(self, value) return setmetatable(value, {_jsonarray=true}) end,
            newObject=function(self, value) return value end}
end
package.preload['json'] = function()
    return {decode_file=function(path) return request end,
            encode_file=function(value, path, options) response = value end}
end
eventful = {onReactionCompleting={}, onReactionComplete={}}
package.preload['plugins.eventful'] = function() return eventful end
local template = {job_fields={job_type=1, reaction_name='BREW_DRINK_FROM_PLANT'},
                  items={{reaction_id=999, reagent_index=0}, {reaction_id=999, reagent_index=1}}}
package.preload['dfhack.workshops'] = function()
    return {getJobs=function(...) return {template} end}
end
jobs = {}
next_job_id = 20
building = {id=7, centerx=10, centery=11, z=2, jobs=vector(),
    getType=function() return 1 end, getSubtype=function() return 1 end,
    getBuildStage=function() return 1 end, getMaxBuildStage=function() return 1 end}
df = {
    global={cur_year=1, cur_year_tick=100, item_next_id=100, pause_state=true,
        gamemode=0, world={world_data={}, cur_savegame={save_dir='test'},
            units={active=vector()}, buildings={all=vector({building})},
            jobs={list={}}, items={other={}}, raws={reactions={reactions=vector({
                {code='ANOTHER_REACTION'}, {code='BREW_DRINK_FROM_PLANT'}, {code='YET_ANOTHER'},
            })}}}},
    item_type={[1]='DRINK', [2]='BARREL'}, workshop_type={Still=1, [1]='Still'},
    building_type={Workshop=1}, job_type={CustomReaction=1}, game_mode={[0]='DWARF'},
    building={find=function(id) if id==7 then return building end end},
    unit={find=function(id) return nil end},
    job={find=function(id) return jobs[id] end,
         new=function()
             return {id=-1, flags={}, general_refs=vector(), job_items={elements=vector()},
                     assign=function(self, fields) for k,v in pairs(fields) do self[k]=v end end,
                     delete=function(self) self.deleted=true end}
         end},
    general_ref_building_holderst={new=function() return {} end},
}
dfhack = {
    getDFPath=function() return 'not-a-real-directory' end,
    getDFVersion=function() return '53.16' end,
    getDFHackVersion=function() return '53.16-r1.1' end,
    df2utf=function(value) return value end,
    isMapLoaded=function() return true end, isWorldLoaded=function() return true end,
    world={isFortressMode=function() return true end},
    gui={getCurFocus=function() return 'dwarfmode' end},
    filesystem={isdir=function() return true end,
        isfile=function(path) return request ~= nil and path:match('/request.json$') ~= nil end},
    timeout=function(interval, unit, callback) timer=callback end,
    printerr=function(text) last_error=text end,
    buildings={markedForRemoval=function() return false end},
    job={linkIntoWorld=function(job, new_id)
             assert(new_id); job.id=next_job_id; next_job_id=next_job_id+1; jobs[job.id]=job; return true
         end,
         removeJob=function(job) jobs[job.id]=nil; job.removed=true end,
         checkBuildingsNow=function() buildings_checked=true end},
}
os.rename=function(...) return true end
print=function(...) end
function qerror(text) error(text) end
function is_array(value) local mt=getmetatable(value); return mt and mt._jsonarray end
function send(op, args)
    request={protocol=1, session=live_state.session, seq=live_state.last_sequence+1,
             op=op, args=args or {}, timeout_ms=1000, expires_at=os.time()+60}
    timer()
    return response
end
function setup_product(job_id)
    reaction={code='BREW_DRINK_FROM_PLANT'}
    product={}
    worker={id=3, job={current_job=jobs[job_id]}}
    outputs=vector()
    call_native={value=true}
end
function before_produce()
    eventful.onReactionCompleting.dfeval_live(reaction, product, worker, vector(), vector(), outputs, call_native)
end
function append_item(id, kind, size)
    outputs:insert('#', {id=id, getType=function() return kind end, getStackSize=function() return size end})
end
function after_produce()
    eventful.onReactionComplete.dfeval_live(reaction, product, worker, vector(), vector(), outputs)
end
'''


class LuaBridge:
    def __init__(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(FAKE_DF)
        self.start = self.lua.execute("return function(...)\n" + SCRIPT.read_text(encoding="utf-8") + "\nend")
        self.start("start", SESSION)
    def python(self, value):
        if lua_type(value) != "table":
            return None if value == "\0" else value
        if self.lua.globals().is_array(value):
            return [self.python(value[index]) for index in range(1, len(value) + 1)]
        return {key: self.python(child) for key, child in value.items()}
    def call(self, operation, **args):
        return self.python(self.lua.globals().send(operation, self.lua.table_from(args)))
    def queue(self, quantity=1):
        response = self.call("queue_brew", workshop_id=7, quantity=quantity)
        assert response["ok"] is True, response
        job_id = response["result"]["job_ids"][0]
        self.lua.globals().setup_product(job_id)
        return job_id
    def evidence(self):
        return self.call("observe")["result"]["brewing"]
    def produce(self, units=25):
        self.lua.execute(f'''
            before_produce()
            append_item(df.global.item_next_id, 1, {units})
            df.global.item_next_id=df.global.item_next_id+1
            after_produce()
        ''')


@pytest.fixture
def bridge():
    return LuaBridge()


def test_real_lua_queues_native_job_with_global_reaction_index(bridge):
    job_id = bridge.queue()
    assert bridge.lua.eval(f"jobs[{job_id}].job_items.elements[0].reaction_id") == 1
    assert bridge.lua.eval(f"jobs[{job_id}].job_items.elements[1].reagent_index") == 1
    assert bridge.lua.eval(f"jobs[{job_id}].general_refs[0].building_id") == 7
    assert bridge.lua.eval(f"jobs[{job_id}].pos.x") == 10
    assert bridge.lua.eval("#building.jobs") == 1
    assert bridge.lua.eval("buildings_checked") is True
    assert bridge.evidence()["events"] == []


def test_only_paired_post_native_new_drink_output_is_evidence(bridge):
    job_id = bridge.queue()
    bridge.lua.execute("before_produce()")
    assert bridge.evidence()["events"] == []
    bridge.lua.execute("append_item(100,1,25); df.global.item_next_id=101; after_produce()")
    evidence = bridge.evidence()
    event = evidence["events"][0]
    assert event["job_id"] == job_id
    assert event["reaction"] == "BREW_DRINK_FROM_PLANT"
    assert event["outputs"] == [{"id": 100, "item_type": "DRINK", "stack_size": 25, "newly_created": True}]
    assert event["item_next_id_before"] == 100 and event["item_next_id_after"] == 101
    assert bridge.lua.eval("call_native.value") is True
    summary = summarize_brewing([{"brewing": evidence}])
    assert summary["jobs_with_confirmed_drink_products"] == 1
    assert summary["confirmed_new_drink_stack_units"] == 25


def test_existing_outputs_and_returned_containers_are_not_created_drinks(bridge):
    bridge.queue()
    bridge.lua.execute('''
        append_item(80, 1, 1000)
        before_produce()
        append_item(81, 2, 1)
        after_produce()
    ''')
    evidence = bridge.evidence()
    assert evidence["events"][0]["outputs"] == [{"id": 81, "item_type": "BARREL", "stack_size": 1, "newly_created": False}]
    summary = summarize_brewing([{"brewing": evidence}])
    assert summary["confirmed_drink_product_items"] == 0
    assert summary["jobs_with_confirmed_drink_products"] == 0


def test_missing_post_callback_disappearance_or_stock_change_is_not_success(bridge):
    job_id = bridge.queue()
    bridge.lua.execute(f"before_produce(); jobs[{job_id}]=nil; worker.job.current_job=nil")
    evidence = bridge.evidence()
    snapshot = {"brewing": evidence, "jobs": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": 500}}}}
    assert summarize_brewing([snapshot])["confirmed_drink_product_items"] == 0


@pytest.mark.parametrize("alteration", [
    "worker.job.current_job={id=999,reaction_name='BREW_DRINK_FROM_PLANT'}",
    "reaction.code='OTHER_REACTION'",
    "df.global.world.world_data={}",
])
def test_untracked_job_wrong_reaction_or_changed_world_cannot_create_evidence(bridge, alteration):
    bridge.queue()
    bridge.lua.execute(alteration)
    bridge.produce()
    assert bridge.lua.eval("#live_state.brew_events") == 0


def test_stale_session_callbacks_cannot_append_to_new_session(bridge):
    bridge.queue()
    old_after = bridge.lua.globals().eventful.onReactionComplete.dfeval_live
    bridge.lua.execute("before_produce(); append_item(100,1,25); df.global.item_next_id=101")
    bridge.start("start", "b" * 32)
    g = bridge.lua.globals()
    old_after(g.reaction, g.product, g.worker, g.vector(), g.vector(), g.outputs)
    assert bridge.evidence()["events"] == []
    assert bridge.evidence()["session"] == "b" * 32


def test_world_change_resets_tracked_jobs_and_event_epoch(bridge):
    bridge.queue()
    bridge.produce()
    first = bridge.evidence()
    bridge.lua.execute("df.global.world.world_data={}")
    second = bridge.evidence()
    assert second["events"] == [] and second["queued_jobs"] == 0
    assert second["epoch"] == first["epoch"] + 1


def test_lua_event_history_and_output_lists_are_bounded(bridge):
    bridge.queue()
    bridge.lua.execute('''
        for n=1,4100 do
            before_produce()
            append_item(df.global.item_next_id,1,1)
            df.global.item_next_id=df.global.item_next_id+1
            after_produce()
        end
    ''')
    evidence = bridge.evidence()
    assert len(evidence["events"]) == 4096
    assert evidence["dropped_events"] == 4
    assert summarize_brewing([{"brewing": evidence}])["evidence_complete"] is False
    other = LuaBridge()
    other.queue()
    other.lua.execute('''
        before_produce()
        for n=1,300 do append_item(df.global.item_next_id,1,1); df.global.item_next_id=df.global.item_next_id+1 end
        after_produce()
    ''')
    event = other.evidence()["events"][0]
    assert len(event["outputs"]) == 256
    assert event["dropped_outputs"] == 44


def test_runtime_hook_failure_is_recorded_without_modifying_native_permission(bridge):
    bridge.queue()
    bridge.lua.execute("before_produce(); df.global.item_next_id=nil; append_item(100,1,25); after_produce()")
    evidence = bridge.evidence()
    assert evidence["error_count"] == 1
    assert evidence["events"] == []
    assert bridge.lua.eval("call_native.value") is True


def test_missing_hook_prevents_uninstrumented_brew_queue(bridge):
    bridge.lua.execute("eventful.onReactionComplete=nil")
    bridge.start("start", "b" * 32)
    response = bridge.call("queue_brew", workshop_id=7, quantity=1)
    assert response["ok"] is False
    assert "evidence hooks are unavailable" in response["error"]
    assert bridge.lua.eval("#building.jobs") == 0


def test_replayed_cumulative_events_deduplicate_and_unknown_units_remain_unknown(bridge):
    bridge.queue()
    bridge.produce()
    evidence = bridge.evidence()
    snapshots = [{"brewing": evidence}, {"brewing": copy.deepcopy(evidence)}]
    summary = summarize_brewing(snapshots)
    assert summary["confirmed_drink_product_items"] == 1
    assert summary["confirmed_new_drink_stack_units"] == 25
    events = [{"kind": "snapshot", "snapshot": value} for value in snapshots]
    assert summarize_events(events)["brewing"] == summary
    evidence["events"][0]["outputs"][0]["stack_size"] = None
    summary = summarize_brewing([{"brewing": evidence}])
    assert summary["confirmed_drink_product_items"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["unknown_product_quantities"] == 1
    assert summary["evidence_complete"] is False


def test_missing_events_are_unknown_and_invalid_allocation_proof_is_rejected(bridge):
    assert summarize_brewing([{}])["confirmed_drink_product_items"] is None
    bridge.queue()
    bridge.produce()
    evidence = bridge.evidence()
    evidence["events"][0]["outputs"][0]["id"] = 50
    summary = summarize_brewing([{"brewing": evidence}])
    assert summary["confirmed_drink_product_items"] == 0
    assert summary["invalid_evidence_records"] == 1
