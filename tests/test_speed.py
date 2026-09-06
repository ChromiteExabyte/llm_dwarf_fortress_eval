"""Native pacing and exact-boundary behavior under fake DF, never a game call."""

from concurrent.futures import ThreadPoolExecutor
import json
import time

from lupa.lua54 import LuaRuntime
import pytest

from dfeval.live import LiveBridge, MAX_SIMULATION_FPS, _atomic_json
from test_job_events import FAKE_DF, LuaBridge, SCRIPT, SESSION


PACING_DF = r'''
df.global.enabler={fps=100,gfps=50,fps_per_gfps=2}
df.global.world.frame_counter=100
now_ms=0
cancelled={}
tick_schedule_count=0
dfhack.getTickCount=function() return now_ms end
dfhack.filesystem.isfile=function(path)
    local seq=path:match('/cancel%.(%d+)%.json$')
    if seq then return cancelled[tonumber(seq)]==true end
    return request~=nil and path:match('/request.json$')~=nil
end
local next_timer_id=0
dfhack.timeout=function(interval, unit, callback)
    next_timer_id=next_timer_id+1
    if unit=='frames' then timer=callback
    else
        assert(unit=='ticks')
        tick_schedule_count=tick_schedule_count+1
        tick_callback=callback; tick_due=df.global.world.frame_counter+interval
        tick_id=next_timer_id
    end
    return next_timer_id
end
dfhack.timeout_active=function(id,callback)
    if tick_id==id then tick_callback=callback end
end
function send(op,args)
    request={protocol=1,session=live_state.session,seq=live_state.last_sequence+1,
        op=op,args=args or {},timeout_ms=5000,expires_at=os.time()+60}
    response=nil
    timer()
    return response
end
function step(ticks)
    for i=1,ticks do
        if df.global.pause_state then break end
        df.global.cur_year_tick=df.global.cur_year_tick+1
        df.global.world.frame_counter=df.global.world.frame_counter+1
        now_ms=now_ms+1
        if tick_callback and df.global.world.frame_counter>=tick_due then
            local callback=tick_callback; tick_callback=nil; callback()
        end
    end
end
'''


class SpeedBridge(LuaBridge):
    def __init__(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(FAKE_DF + PACING_DF)
        self.start = self.lua.execute("return function(...)\n" + SCRIPT.read_text(encoding="utf-8") + "\nend")
        self.start("start", SESSION)


@pytest.fixture
def bridge():
    return SpeedBridge()


def test_default_is_unchanged_and_explicit_override_restores_original(bridge):
    original = bridge.call("status")["result"]["simulation_fps"]
    assert original["original"] == original["effective"] == 100
    assert original["override_active"] is False
    assert original["requested"] is None
    result = bridge.call("set_simulation_fps", fps=10000)["result"]["simulation_fps"]
    assert result["original"] == 100 and result["effective"] == result["requested"] == 10000
    assert result["override_active"] is True and result["graphics_cap"] == 50
    assert bridge.lua.eval("df.global.enabler.fps_per_gfps") == 200
    for _ in range(2):
        restored = bridge.call("restore_simulation_fps")["result"]
        assert restored["restored"] is True
        assert restored["simulation_fps"]["effective"] == 100
        assert restored["simulation_fps"]["override_active"] is False
        assert restored["simulation_fps"]["graphics_cap"] == 50


@pytest.mark.parametrize("fps", [0, -1, 10001, True, 1.5, "1000", None, float("inf"), float("nan")])
def test_python_fps_bounds_reject_before_transport(tmp_path, fps):
    bridge = LiveBridge(tmp_path)
    with pytest.raises(ValueError):
        bridge.set_simulation_fps(fps)
    assert bridge._sequence == 0


@pytest.mark.parametrize("fps", [0, -1, 10001, True, 1.5, "1000"])
def test_lua_independently_rejects_invalid_caps(bridge, fps):
    assert bridge.call("set_simulation_fps", fps=fps)["ok"] is False
    assert bridge.lua.eval("df.global.enabler.fps") == 100


def test_fps_request_schema_stays_explicit_and_protocol_one(tmp_path):
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    def server():
        path = bridge.ipc_dir / "request.json"
        deadline = time.monotonic() + 2
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.001)
        request = json.loads(path.read_text())
        assert request["protocol"] == 1 and request["op"] == "set_simulation_fps"
        assert request["args"] == {"fps": MAX_SIMULATION_FPS}
        _atomic_json(bridge.ipc_dir / "response.1.json", {**request, "ok": True, "result": {"accepted": True}})
    with ThreadPoolExecutor() as pool:
        task = pool.submit(server)
        assert bridge.set_simulation_fps(MAX_SIMULATION_FPS) == {"accepted": True}
        task.result()
    with pytest.raises(ValueError):
        bridge._request("restore_simulation_fps", {"fps": 100})


@pytest.mark.parametrize("fps", [1, 100, 1000, 10000])
def test_normal_advance_stops_exactly_with_one_native_target_timer_and_keeps_cap(bridge, fps):
    bridge.call("set_simulation_fps", fps=fps)
    assert bridge.call("advance_ticks", ticks=1200) is None
    bridge.lua.globals().step(1300)
    response = bridge.python(bridge.lua.globals().response)
    assert response["ok"] is True
    assert response["result"]["elapsed_ticks"] == 1200
    assert response["result"]["overshoot_ticks"] == 0
    assert bridge.lua.eval("tick_schedule_count") == 1
    assert bridge.lua.eval("df.global.pause_state") is True
    assert bridge.lua.eval("df.global.enabler.fps") == fps


@pytest.mark.parametrize("cause", ["cancel", "watchdog", "changed_world", "clock_skip"])
def test_failed_advance_pauses_and_restores_cap(bridge, cause):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.call("advance_ticks", ticks=100)
    bridge.lua.globals().step(10)
    if cause == "cancel":
        bridge.lua.execute("cancelled[live_state.active.request.seq]=true; timer()")
    elif cause == "watchdog":
        bridge.lua.execute("now_ms=6000; timer()")
    elif cause == "changed_world":
        bridge.lua.execute("df.global.world.world_data={}; timer()")
    else:
        bridge.lua.execute("df.global.cur_year_tick=df.global.cur_year_tick+1000; timer()")
    response = bridge.python(bridge.lua.globals().response)
    assert response["ok"] is False
    assert bridge.lua.eval("df.global.pause_state") is True
    assert bridge.lua.eval("df.global.enabler.fps") == 100
    assert bridge.lua.eval("live_state.speed.override_active") is False


def test_cancel_after_boundary_still_restores_even_if_host_missed_response(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.call("advance_ticks", ticks=10)
    bridge.lua.globals().step(10)
    bridge.lua.execute("cancelled[live_state.speed.last_advance_seq]=true; timer()")
    assert bridge.lua.eval("df.global.enabler.fps") == 100


def test_new_session_restores_before_capturing_original_and_rejects_active_takeover(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.call("advance_ticks", ticks=10)
    with pytest.raises(Exception, match="advance is active"):
        bridge.start("start", "b" * 32)
    assert bridge.lua.eval("df.global.enabler.fps") == 1000
    bridge.lua.globals().step(10)
    bridge.start("start", "b" * 32)
    assert bridge.lua.eval("df.global.enabler.fps") == 100
    assert bridge.lua.eval("live_state.speed.original") == 100


def test_world_unload_restores_while_idle(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.lua.execute("dfhack.isMapLoaded=function() return false end; timer()")
    assert bridge.lua.eval("df.global.enabler.fps") == 100
    assert bridge.lua.eval("df.global.pause_state") is True


def test_unavailable_native_fields_are_visible_and_never_claim_restored(bridge):
    bridge.lua.execute("df.global.enabler=nil")
    bridge.start("start", "b" * 32)
    assert bridge.call("set_simulation_fps", fps=1000)["ok"] is False
    result = bridge.call("restore_simulation_fps")["result"]
    assert result["restored"] is False
    assert result["simulation_fps"]["effective"] is None
    assert result["errors"]


def test_restore_failure_is_visible_and_blocks_session_takeover(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.lua.execute("df.global.enabler.gfps=0")
    result = bridge.call("restore_simulation_fps")["result"]
    assert result["restored"] is False
    assert result["simulation_fps"]["override_active"] is True
    with pytest.raises(Exception, match="restore failed"):
        bridge.start("start", "b" * 32)


@pytest.mark.parametrize("failure", ["return nil", "error('timer unavailable')"])
def test_unavailable_tick_timer_refuses_unpause_and_restores(bridge, failure):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.lua.execute("old_timeout=dfhack.timeout; dfhack.timeout=function(n,mode,callback) "
                       "if mode=='ticks' then " + failure + " end; return old_timeout(n,mode,callback) end")
    response = bridge.call("advance_ticks", ticks=100)
    assert response["ok"] is False and "Cannot schedule" in response["error"]
    assert bridge.lua.eval("df.global.pause_state") is True
    assert bridge.lua.eval("live_state.active") is None
    assert bridge.lua.eval("df.global.enabler.fps") == 100


def test_lost_speed_acknowledgment_cancellation_restores_while_idle(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.lua.execute("cancelled[live_state.speed.request_seq]=true; timer()")
    assert bridge.lua.eval("df.global.enabler.fps") == 100
    assert bridge.lua.eval("df.global.pause_state") is True


def test_native_boundary_rejects_calendar_counter_divergence(bridge):
    bridge.call("set_simulation_fps", fps=1000)
    bridge.call("advance_ticks", ticks=10)
    bridge.lua.execute("df.global.cur_year_tick=df.global.cur_year_tick+1")
    bridge.lua.globals().step(10)
    response = bridge.python(bridge.lua.globals().response)
    assert response["ok"] is False and "diverged" in response["error"]
    assert bridge.lua.eval("df.global.pause_state") is True
    assert bridge.lua.eval("df.global.enabler.fps") == 100
