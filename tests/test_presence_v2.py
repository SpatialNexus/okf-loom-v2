import json
import threading
import multiprocessing
import time

import pytest

from okf_loom.presence import PresenceStore, PresenceUnavailable
from okf_loom.studio import Studio


def store(tmp_path, clock=lambda: 1000.0):
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    return PresenceStore(studio.presence_path, studio._run_presence_transaction, clock=clock), studio


def row(label):
    return {"kind": "browser", "actor": "browser", "state": "watching",
            "label": label, "viewport": {"schema": "graph-viewport-v1", "x": .2, "y": .8}}


def test_v1_migrates_and_two_browsers_merge_without_clobber(tmp_path):
    authority, studio = store(tmp_path)
    studio.presence_path.write_text(json.dumps({"actor": "agent", "state": "editing", "focus": "a"}))
    authority.mutate("page-a", 1, row("A"))
    authority.mutate("page-b", 1, row("B"))
    snapshot = authority.snapshot()
    assert {item["session_id"] for item in snapshot["records"]} == {"agent", "page-a", "page-b"}
    assert json.loads(studio.presence_path.read_text())["version"] == 2


@pytest.mark.parametrize(("legacy", "expected"), [
    # v1 stored whatever --actor / POST /__presence passed, verbatim.
    ({"actor":"v1-agent","state":"editing","focus":"datasets/orders"},
     {"actor":"agent","state":"editing","focus":"datasets/orders"}),
    ({"actor":"user","state":"reviewing"}, {"actor":"user","state":"idle"}),
    ({"actor":"agent","state":"editing","focus":"../bad"}, {"actor":"agent","state":"editing"}),
    ({"actor":"agent","state":"thinking","focus":{"id":"a"},"message":"x"*202},
     {"actor":"agent","state":"thinking"}),
])
def test_v1_flat_presence_is_normalised_instead_of_rejected(tmp_path,legacy,expected):
    authority,studio=store(tmp_path); studio.presence_path.write_text(json.dumps(legacy))
    [agent]=authority.snapshot()["records"]
    assert {k:agent[k] for k in ("actor","state","focus","message") if k in agent}==expected
    authority.mutate("page-a",1,row("A"))
    assert json.loads(studio.presence_path.read_text())["records"]["agent"]["actor"]==expected["actor"]


@pytest.mark.parametrize("legacy", [
    {"actor":"agent","state":"idle","extra":True},
    {"actor":7,"state":"idle"},
    {"state":"idle"},
])
def test_unrecognised_flat_presence_still_fails_closed(tmp_path,legacy):
    authority,studio=store(tmp_path); studio.presence_path.write_text(json.dumps(legacy))
    with pytest.raises(PresenceUnavailable): authority.snapshot()


def test_serve_starts_over_v1_custom_actor_presence(tmp_path,monkeypatch):
    import okf_loom.server as server_module
    (tmp_path/"note.md").write_text("---\ntype: note\n---\nBody\n")
    studio=Studio.for_bundle(tmp_path); studio.ensure_session()
    studio.presence_path.write_text(json.dumps({"actor":"v1-agent","state":"editing","focus":"note"}))
    class StubServer:
        def __init__(self,address,_handler): self.server_address=address
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server_module,"ThreadingHTTPServer",StubServer)
    server_module.run_server(tmp_path,host="127.0.0.1",port=0,watch=False,open_browser=False)
    disk=json.loads(studio.presence_path.read_text())
    assert disk["version"]==2 and disk["records"]["agent"]["actor"]=="agent"
    assert disk["records"]["agent"]["state"]=="idle"


def test_invalid_v2_presence_error_names_file_and_reset(tmp_path,capsys):
    from okf_loom.cli import main
    studio=Studio.for_bundle(tmp_path); studio.ensure_session()
    studio.presence_path.write_text(json.dumps({"version":2,"presence_rev":0,"records":{"agent":{}},"tombstones":{},"issued":{}}))
    with pytest.raises(PresenceUnavailable) as raised: studio.set_presence(state="editing")
    message=str(raised.value)
    assert str(studio.presence_path) in message and "stop the Studio" in message and "delete" in message
    assert main(["presence",str(tmp_path),"--state","editing"])==1
    err=capsys.readouterr().err
    assert str(studio.presence_path) in err and "delete" in err


def test_rejected_presence_update_does_not_blame_the_file(tmp_path):
    studio=Studio.for_bundle(tmp_path); studio.ensure_session()
    with pytest.raises(PresenceUnavailable) as raised: studio.set_presence(actor="v1-agent",state="editing")
    message=str(raised.value)
    assert message and "actor" in message and "delete" not in message
    assert studio.get_presence_snapshot()["records"]==[]


def test_concurrent_sessions_merge_under_cross_process_lock(tmp_path):
    authority, _ = store(tmp_path)
    gate = threading.Barrier(3)
    def write(sid):
        gate.wait(); authority.mutate(sid, 1, row(sid))
    threads = [threading.Thread(target=write, args=(sid,)) for sid in ("page-a", "page-b")]
    for thread in threads: thread.start()
    gate.wait()
    for thread in threads: thread.join(3); assert not thread.is_alive()
    assert len(authority.snapshot()["records"]) == 2


def test_sequence_leave_tombstone_blocks_stale_resurrection(tmp_path):
    authority, _ = store(tmp_path)
    assert authority.mutate("page-a", 2, row("A"))[1]
    assert not authority.mutate("page-a", 2, row("duplicate"))[1]
    assert authority.mutate("page-a", 3, None)[1]
    assert not authority.mutate("page-a", 2, row("stale"))[1]
    assert authority.snapshot()["records"] == []
    assert authority.mutate("page-a", 4, row("returned"))[1]


def test_get_hides_expired_without_mutating_and_sweep_persists(tmp_path):
    now = [1000.0]
    authority, studio = store(tmp_path, lambda: now[0])
    authority.mutate("page-a", 1, row("A")); before = studio.presence_path.read_bytes()
    now[0] += 16
    assert authority.snapshot()["records"] == []
    assert studio.presence_path.read_bytes() == before
    assert authority.sweep()[1]
    assert json.loads(studio.presence_path.read_text())["records"] == {}


@pytest.mark.parametrize("bad", [b"not-json", b"[]", b'{"version":2}'])
def test_corrupt_authority_fails_closed(tmp_path, bad):
    authority, studio = store(tmp_path)
    studio.presence_path.write_bytes(bad)
    with pytest.raises(PresenceUnavailable): authority.snapshot()


def test_symlink_authority_is_never_followed(tmp_path):
    authority, studio = store(tmp_path)
    victim = tmp_path / "victim"; victim.write_text("secret")
    studio.presence_path.symlink_to(victim)
    with pytest.raises(PresenceUnavailable): authority.snapshot()
    assert victim.read_text() == "secret"


def test_browser_presence_is_ephemeral_not_retained_event(tmp_path):
    authority, studio = store(tmp_path)
    authority.mutate("page-a", 1, row("A"))
    assert not studio.events_path.exists()


@pytest.mark.parametrize("target", ["static", "single-file"])
def test_portable_runtime_output_has_no_presence_credentials(tmp_path, target):
    from okf_loom.model import Bundle
    from okf_loom.render import build_site
    root=tmp_path/"bundle"; root.mkdir(); (root/"note.md").write_text("---\ntype: note\n---\nSafe\n")
    output=tmp_path/target; build_site(Bundle.load(root),output,target=target)
    rendered="\n".join(path.read_text(errors="ignore") for path in output.rglob("*.html"))
    assert "browser_session" not in rendered and "presence-v2:" not in rendered


def _process_presence_write(root, sid):
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    studio.presence_store.mutate(sid, 1, row(sid), capability_expires_at=2000.0)


def test_separate_process_rmw_merges(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.ensure_session()
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_process_presence_write, args=(tmp_path, sid))
                 for sid in ("process-a", "process-b")]
    for process in processes: process.start()
    for process in processes: process.join(5); assert process.exitcode == 0
    assert {r["session_id"] for r in studio.get_presence_snapshot()["records"]} == {"process-a", "process-b"}


def test_more_than_old_tombstone_boundary_retains_replay_floors(tmp_path):
    authority, _ = store(tmp_path)
    for index in range(300):
        sid = f"session-{index:04d}"
        authority.mutate(sid, 1, row(sid), capability_expires_at=2000.0)
        authority.mutate(sid, 2, None, capability_expires_at=2000.0)
    data = json.loads(authority.path.read_text())
    assert len(data["tombstones"]) == 300
    assert not authority.mutate("session-0000", 1, row("stale"), capability_expires_at=2000.0)[1]


@pytest.mark.parametrize("mutation", [
    lambda d: d["records"].update({"x": {"session_id":"x"}}),
    lambda d: d["tombstones"].update({"x":{"client_seq":True,"left_at":1,"retain_until":2}}),
    lambda d: d["issued"].update({"x":{"generation":"g","issued_at":float("inf"),"expires_at":2}}),
    lambda d: d["records"].update({"x":{"session_id":"x","kind":"browser","actor":"browser","state":"watching","client_seq":1,"updated_at":1,"expires_at":2,"capability_expires_at":3,"label":"A\u202eB","focus":"../bad","viewport":{"schema":"graph-viewport-v1","x":.5,"y":.5}}}),
    lambda d: d["records"].update({"agent":{"session_id":"agent","kind":"agent","actor":"agent","state":"editing","client_seq":1,"updated_at":1,"expires_at":2,"focus":"../bad","message":"x"*202}}),
    lambda d: d.update(unknown=True),
])
def test_malformed_nested_authority_is_unavailable(tmp_path, mutation):
    authority, _ = store(tmp_path)
    data = {"version":2,"presence_rev":0,"records":{},"tombstones":{},"issued":{}}; mutation(data)
    authority.path.write_text(json.dumps(data))
    with pytest.raises(PresenceUnavailable): authority.snapshot()


def test_failed_agent_commit_changes_neither_cache_file_nor_bus(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.ensure_session()
    before = studio.get_presence(); subscriber = studio.bus.subscribe()
    victim = tmp_path / "victim"; victim.write_text("outside")
    studio.presence_path.symlink_to(victim)
    with pytest.raises(PresenceUnavailable): studio.set_presence(state="editing")
    assert studio.get_presence() == before and subscriber.empty() and victim.read_text() == "outside"


@pytest.mark.parametrize("target", ["session", "lock"])
def test_session_or_sidecar_lock_symlink_is_rejected(tmp_path, target):
    studio = Studio.for_bundle(tmp_path); outside = tmp_path / "outside"; outside.mkdir()
    if target == "session":
        studio.session_dir.parent.mkdir(parents=True); studio.session_dir.symlink_to(outside, target_is_directory=True)
    else:
        studio.ensure_session(); (studio.session_dir / ".presence.lock").symlink_to(outside)
    with pytest.raises(PresenceUnavailable): studio.get_presence_snapshot()
    assert list(outside.iterdir()) == []


def test_pinned_session_survives_ancestor_substitution_without_outside_write(tmp_path, monkeypatch):
    authority, studio = store(tmp_path); outside=tmp_path/"outside"; outside.mkdir()
    pinned=tmp_path/"pinned-session"; entered=threading.Event(); release=threading.Event()
    original=authority._write
    def paused(data,dir_fd):
        entered.set(); assert release.wait(3); return original(data,dir_fd)
    monkeypatch.setattr(authority,"_write",paused)
    error=[]
    thread=threading.Thread(target=lambda: authority.mutate("page-a",1,row("A"),capability_expires_at=time.time()+60))
    thread.start(); assert entered.wait(3)
    studio.session_dir.rename(pinned); studio.session_dir.symlink_to(outside,target_is_directory=True)
    release.set(); thread.join(3); assert not thread.is_alive() and not error
    assert not (outside/"presence.json").exists() and not (outside/".presence.lock").exists()
    assert (pinned/"presence.json").is_file() and (pinned/".presence.lock").is_file()


def test_bounded_delta_bus_and_no_retained_browser_event(tmp_path):
    studio = Studio.for_bundle(tmp_path); studio.ensure_session(); q = studio.bus.subscribe()
    _, _, upsert = studio.presence_store.mutate("page-a", 1, row("A"), capability_expires_at=time.time()+60)
    studio.bus.publish(upsert)
    event = q.get(timeout=1)
    assert set(event) == {"type","presence_version","presence_rev","action","record"}
    _, _, leave = studio.presence_store.mutate("page-a", 2, None, capability_expires_at=time.time()+60)
    studio.bus.publish(leave)
    assert q.get(timeout=1)["action"] == "leave"
    assert not studio.events_path.exists()


def test_watch_false_independent_sweeper_emits_one_expiry_delta(tmp_path):
    from okf_loom.server import _PresenceSweeper
    studio=Studio.for_bundle(tmp_path); studio.ensure_session(); q=studio.bus.subscribe()
    now=time.time(); studio.presence_store.mutate("page-a",1,row("A"),capability_expires_at=now+60)
    data=json.loads(studio.presence_path.read_text()); data["records"]["page-a"]["expires_at"]=now-1
    studio.presence_path.write_text(json.dumps(data))
    owner=_PresenceSweeper(studio,interval=.02); owner.start()
    event=q.get(timeout=1); time.sleep(.08); owner.stop()
    assert event["action"]=="leave" and event["reason"]=="expired" and q.empty()


def test_post_start_failure_cleanup_leaves_no_sweeper_thread(tmp_path):
    from okf_loom.server import _PresenceSweeper
    studio=Studio.for_bundle(tmp_path); owner=_PresenceSweeper(studio,interval=.02)
    try:
        owner.start()
        raise RuntimeError("serve_forever failed after start")
    except RuntimeError:
        pass
    finally:
        if owner.is_alive(): owner.stop()
    assert not owner.is_alive()


@pytest.mark.parametrize("fail",[False,True])
def test_actual_run_server_owns_exactly_one_sweeper_and_always_stops(tmp_path,monkeypatch,fail):
    import okf_loom.server as server_module
    (tmp_path/"note.md").write_text("---\ntype: note\n---\nBody\n")
    counts={"start":0,"stop":0}; generations=[]
    class StubServer:
        def __init__(self,address,_handler): self.server_address=address; generations.append(self)
        def serve_forever(self):
            if fail: raise RuntimeError("post-start failure")
        def shutdown(self): pass
        def server_close(self): pass
    class SpySweeper:
        def __init__(self,_studio): self.alive=False
        def start(self): counts["start"]+=1; self.alive=True
        def is_alive(self): return self.alive
        def stop(self): counts["stop"]+=1; self.alive=False
    monkeypatch.setattr(server_module,"ThreadingHTTPServer",StubServer)
    monkeypatch.setattr(server_module,"_PresenceSweeper",SpySweeper)
    if fail:
        with pytest.raises(RuntimeError): server_module.run_server(tmp_path,host="127.0.0.1",port=0,watch=False,open_browser=False)
    else:
        server_module.run_server(tmp_path,host="127.0.0.1",port=0,watch=False,open_browser=False)
    assert counts=={"start":1,"stop":1}


def test_two_actual_run_server_lifecycles_get_distinct_generations(tmp_path,monkeypatch):
    import okf_loom.server as server_module
    (tmp_path/"note.md").write_text("---\ntype: note\n---\nBody\n")
    seen=[]
    class StubServer:
        def __init__(self,address,_handler): self.server_address=address; seen.append(self)
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(server_module,"ThreadingHTTPServer",StubServer)
    for _ in range(2): server_module.run_server(tmp_path,host="127.0.0.1",port=0,watch=False,open_browser=False)
    assert len(seen)==2 and seen[0]._presence_generation!=seen[1]._presence_generation


def test_get_issue_and_other_mutation_do_not_opportunistically_sweep(tmp_path):
    now=[1000.0]; authority, studio=store(tmp_path,lambda:now[0])
    authority.mutate("expired",1,row("old"),capability_expires_at=1100)
    now[0]=1016
    before=json.loads(studio.presence_path.read_text())["presence_rev"]
    assert authority.snapshot()["records"]==[]
    authority.issue("fresh-cap","generation",1016,1076)
    authority.mutate("other",1,row("other"),capability_expires_at=1076)
    disk=json.loads(studio.presence_path.read_text())
    assert "expired" in disk["records"] and disk["presence_rev"]==before+1
    _,deltas=authority.sweep()
    assert len(deltas)==1 and deltas[0]["session_id"]=="expired" and deltas[0]["presence_rev"]==before+2


def test_authorization_and_mutation_are_one_transaction_at_one_clock(tmp_path,monkeypatch):
    now=[1000.0]; authority,studio=store(tmp_path,lambda:now[0])
    authority.issue("page-a","generation",1000,1001)
    entered=threading.Event(); release=threading.Event(); original=authority._load
    def paused(dir_fd):
        data=original(dir_fd); entered.set(); assert release.wait(3); return data
    monkeypatch.setattr(authority,"_load",paused)
    result=[]
    request=threading.Thread(target=lambda: result.append(authority.mutate_authorized(
        "page-a","generation",1000,1001,1,row("A"))))
    request.start(); assert entered.wait(3); now[0]=1002
    swept=[]; sweeper=threading.Thread(target=lambda:swept.append(authority.sweep())); sweeper.start()
    release.set(); request.join(3); sweeper.join(3)
    assert result==[None]
    assert json.loads(studio.presence_path.read_text())["records"]=={}


def test_agent_sequence_is_store_owned_across_clock_rollback(tmp_path):
    now=[2000.0]; authority,studio=store(tmp_path,lambda:now[0])
    authority.set_agent({"kind":"agent","actor":"agent","state":"editing"})
    now[0]=1000.0
    snapshot,applied,_=authority.set_agent({"kind":"agent","actor":"agent","state":"thinking"})
    assert applied and snapshot["records"][0]["client_seq"]==1
    assert json.loads(studio.presence_path.read_text())["records"]["agent"]["state"]=="thinking"


def test_concurrent_agent_writers_allocate_distinct_sequences(tmp_path):
    first=Studio.for_bundle(tmp_path); second=Studio.for_bundle(tmp_path)
    gate=threading.Barrier(3); errors=[]
    def write(studio,state):
        try: gate.wait(); studio.set_presence(state=state)
        except Exception as exc: errors.append(exc)
    threads=[threading.Thread(target=write,args=(first,"editing")),threading.Thread(target=write,args=(second,"thinking"))]
    for thread in threads: thread.start()
    gate.wait()
    for thread in threads: thread.join(3)
    disk=json.loads(first.presence_path.read_text())
    assert not errors and disk["records"]["agent"]["client_seq"]==1


def test_late_sweep_cannot_overwrite_fresh_agent_heartbeat(tmp_path,monkeypatch):
    serving=Studio.for_bundle(tmp_path); cli=Studio.for_bundle(tmp_path)
    serving.presence_ttl_s=cli.presence_ttl_s=.01; serving.set_presence(state="editing")
    time.sleep(.02); entered=threading.Event(); release=threading.Event()
    original=PresenceStore._write
    def paused(self,data,dir_fd):
        if threading.current_thread().name=="deterministic-sweep":
            entered.set(); assert release.wait(3)
        return original(self,data,dir_fd)
    monkeypatch.setattr(PresenceStore,"_write",paused)
    sweep=threading.Thread(name="deterministic-sweep",target=serving.sweep_stale_presence_and_claims)
    heartbeat=threading.Thread(target=lambda:cli.set_presence(state="thinking"))
    sweep.start(); assert entered.wait(3); serving.presence_ttl_s=cli.presence_ttl_s=10; heartbeat.start(); time.sleep(.05); release.set()
    sweep.join(3); heartbeat.join(3); serving.sweep_stale_presence_and_claims()
    assert serving.get_presence()["state"]=="thinking"
    assert json.loads(serving.presence_path.read_text())["records"]["agent"]["state"]=="thinking"


def test_append_event_and_set_presence_lock_order_completes_coherently(tmp_path,monkeypatch):
    studio=Studio.for_bundle(tmp_path); studio.ensure_session()
    sidecar_held=threading.Event(); release=threading.Event(); errors=[]
    original=studio._append_event_locked
    def paused(event,*,publish=True):
        if event.get("type")=="ordinary":
            sidecar_held.set(); assert release.wait(3)
        return original(event,publish=publish)
    monkeypatch.setattr(studio,"_append_event_locked",paused)
    ordinary=threading.Thread(target=lambda:studio.append_event({"type":"ordinary"}))
    presence=threading.Thread(target=lambda:studio.set_presence(state="editing"))
    ordinary.start(); assert sidecar_held.wait(3); presence.start()
    deadline=time.time()+3
    while time.time()<deadline:
        try:
            if json.loads(studio.presence_path.read_text())["records"]["agent"]["state"]=="editing": break
        except (FileNotFoundError,json.JSONDecodeError): pass
        time.sleep(.01)
    else: errors.append("presence authority did not commit while sidecar was held")
    release.set(); ordinary.join(3); presence.join(3)
    rows=studio.read_events(limit=10)
    assert not errors and not ordinary.is_alive() and not presence.is_alive()
    assert [row["type"] for row in rows[-2:]]==["ordinary","presence"]
    assert studio.get_presence()["state"]=="editing"


def test_watch_false_sweep_publishes_cross_process_agent_correction_once(tmp_path):
    serving=Studio.for_bundle(tmp_path); cli=Studio.for_bundle(tmp_path)
    q=serving.bus.subscribe(); cli.set_presence(state="thinking",focus=None)
    serving.sweep_stale_presence_and_claims()
    event=q.get(timeout=1)
    assert event["type"]=="presence" and event["action"]=="upsert"
    assert event["reason"]=="cross_process" and event["record"]["state"]=="thinking"
    serving.sweep_stale_presence_and_claims()
    assert q.empty()


def test_cross_process_expiry_publish_precedes_fresh_commit_and_correction(tmp_path,monkeypatch):
    serving=Studio.for_bundle(tmp_path); cli=Studio.for_bundle(tmp_path)
    serving.presence_ttl_s=cli.presence_ttl_s=.01; serving.set_presence(state="editing")
    time.sleep(.02); q=serving.bus.subscribe(); idle_publish=threading.Event(); release=threading.Event()
    original=serving.bus.publish
    def paused(event):
        record=event.get("record",{})
        if event.get("reason")=="expired" and record.get("state")=="idle":
            idle_publish.set(); assert release.wait(3)
        return original(event)
    monkeypatch.setattr(serving.bus,"publish",paused)
    sweep=threading.Thread(target=serving.sweep_stale_presence_and_claims)
    heartbeat=threading.Thread(target=lambda:cli.set_presence(state="thinking"))
    sweep.start(); assert idle_publish.wait(3); serving.presence_ttl_s=cli.presence_ttl_s=10
    heartbeat.start(); time.sleep(.05)
    assert heartbeat.is_alive(), "fresh cross-process commit bypassed pinned expiry publication"
    release.set(); sweep.join(3); heartbeat.join(3)
    serving.sweep_stale_presence_and_claims()
    first=q.get(timeout=1); second=q.get(timeout=1)
    assert first["record"]["state"]=="idle" and first["reason"]=="expired"
    assert second["record"]["state"]=="thinking" and second["reason"]=="cross_process"
    assert first["presence_rev"] < second["presence_rev"]
    authority=json.loads(serving.presence_path.read_text())["records"]["agent"]
    assert authority["state"]=="thinking" and authority["client_seq"]==second["record"]["client_seq"]
    serving.sweep_stale_presence_and_claims(); assert q.empty()


ASYNC_LIFECYCLE_MATRIX = """
Scenario | Deadline/abort owner | Mutation phase | Public code/result | Persisted state | Late-result behavior | Retry/reconnect behavior | Required proof
capability expiry | server generation/clock | before RMW | 403 forbidden | unchanged | rejected | GET obtains fresh capability | HTTP expiry/restart tests
watch=False TTL | presence sweeper | committed sweep | bounded leave delta | tombstone | stale seq rejected | canonical GET resync | sweeper/bus tests
leave then late upsert | PresenceStore | sequence arbitration | 200 applied:false | tombstone retained to capability expiry | ignored | higher seq accepted | tombstone tests
authority unavailable | pinned transaction | before commit | 503 unavailable | unchanged | no publish/cache update | retry after repair | failure atomicity tests
server shutdown | run_server | no in-flight mutation | clean stop | unchanged | no duplicate owner | next server gets new generation | lifecycle-owner tests
opportunistic expiry discovery | sweeper only | GET/issue/other mutation read | expired hidden/no leave yet | expired row retained | sweeper emits exactly once | canonical GET after delta | concurrent discovery test
authorization plus mutation | PresenceStore pinned transaction | pre-commit authorization | 403 or atomic success | unchanged on expiry | no TOCTOU commit | fresh capability retries | paused authorization/sweep test
actual restart generation | new server lifecycle | before mutation | 403 prior-server | unchanged | old capability fenced | GET from restarted server | two-server HTTP proof
late agent heartbeat during expiry | shared cache lock + pinned store lock | sweep commits idle then heartbeat allocates next store sequence | fresh state | fresh heartbeat wins | stale sweep cannot publish idle afterward | next sweep reads fresh authority | deterministic paused-write interleaving test
run_server normal/failure cleanup | run_server finally owner | after sweeper start | return or propagated failure | unchanged | exactly one sweeper stopped | next run creates one new owner/generation | actual run_server stub lifecycle tests
"""
