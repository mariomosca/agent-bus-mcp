"""v2.6: expected results, task_id on messages, wake_chain stamp.

The wake itself lives in agent-team-os/scripts/agent-team-os-wake.py; here only
what the MCP side writes, which that script reads.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bus_mcp.bus import Bus, BusError  # noqa: E402

AGENT_MAP = {"agents": {"alita": {}, "kai": {}}, "rules": []}


@pytest.fixture()
def bus(tmp_path: Path, monkeypatch) -> Bus:
    monkeypatch.setenv("AB_HOME", str(tmp_path))
    monkeypatch.delenv("AB_HOST", raising=False)
    (tmp_path / "AGENT_MAP.json").write_text(json.dumps(AGENT_MAP), encoding="utf-8")
    return Bus(home=tmp_path)


def test_expect_writes_wait_file(bus: Bus):
    rec = bus.expect("alita", "work-hub", "spike orbit", delegated_to="kai/f3",
                     deadline="2026-10-17T18:00:00Z", target={"onda_terminal_id": "pane-1", "tmux_pane": None})
    f = bus.home / "waits" / "alita" / "@work-hub" / f"{rec['task_id']}.json"
    data = json.loads(f.read_text())
    assert data["state"] == "expected"
    assert data["target"] == {"onda_terminal_id": "pane-1"}
    assert data["delegated_to"] == "kai/f3"
    assert rec["task_id"].startswith("t-")


def test_one_waiter_per_task(bus: Bus):
    bus.expect("alita", "work-hub", "x", task_id="t-1-abc")
    with pytest.raises(BusError, match="one task, one waiter"):
        bus.expect("kai", "f3", "y", task_id="t-1-abc")


def test_bad_task_id_and_deadline_refused(bus: Bus):
    with pytest.raises(BusError):
        bus.expect("alita", "work-hub", "x", task_id="../evil")
    with pytest.raises(BusError):
        bus.expect("alita", "work-hub", "x", deadline="domani")


def test_send_carries_task_id(bus: Bus):
    out = bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
                   payload={"summary": "fatto"}, sender_session="f3", task_id="t-1-abc")
    assert out["task_id"] == "t-1-abc"
    assert json.loads(Path(out["path"]).read_text())["task_id"] == "t-1-abc"


def test_reply_inherits_task_id(bus: Bus):
    req = bus.send(sender="alita", to="kai/f3", msg_type="request", intent="spike",
                   payload={"summary": "fai lo spike"}, sender_session="work-hub", task_id="t-1-abc", kind="delega")
    out = bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
                   payload={"summary": "fatto"}, sender_session="f3", in_reply_to=req["id"])
    assert out["task_id"] == "t-1-abc"


def test_no_task_id_by_default(bus: Bus):
    out = bus.send(sender="kai", to="alita", msg_type="event", intent="x", payload={"summary": "s"})
    assert "task_id" not in json.loads(Path(out["path"]).read_text())
    assert out["task_id"] is None


def test_wake_chain_stamped_after_wake(bus: Bus):
    d = bus.home / "waits" / "kai" / "@f3"
    d.mkdir(parents=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / ".woken.json").write_text(json.dumps({"task_id": "t-9-zzz", "at": now}))
    out = bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
                   payload={"summary": "s"}, sender_session="f3", task_id="t-1-abc")
    rec = json.loads(Path(out["path"]).read_text())
    assert rec["wake_chain"] == {"task_id": "t-9-zzz", "at": now, "session": "kai/f3"}


def test_old_wake_does_not_stamp(bus: Bus):
    d = bus.home / "waits" / "kai" / "@f3"
    d.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / ".woken.json").write_text(json.dumps({"task_id": "t-9-zzz", "at": old}))
    out = bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
                   payload={"summary": "s"}, sender_session="f3")
    assert "wake_chain" not in json.loads(Path(out["path"]).read_text())


def test_list_waits_hides_closed(bus: Bus):
    a = bus.expect("alita", "work-hub", "a")
    b = bus.expect("alita", "work-hub", "b")
    f = bus.home / "waits" / "alita" / "@work-hub" / f"{b['task_id']}.json"
    rec = json.loads(f.read_text()); rec["state"] = "woken"; f.write_text(json.dumps(rec))
    assert [w["task_id"] for w in bus.list_waits("alita", "work-hub")] == [a["task_id"]]
    (f.parent / ".woken.json").write_text(json.dumps({"task_id": b["task_id"], "at": "2026-10-17T10:00:00Z"}))
    assert len(bus.list_waits("alita", None, include_closed=True)) == 2


# ---------- v2.7: questions inside a delegation (DR18-F1.7) ----------

def _registered(bus: Bus, agent: str, slug: str) -> Path:
    bus.register_session(agent, f"/w/{slug}", slug)
    return bus.registry_dir(agent) / f"{slug}.json"


def test_question_with_task_id_marks_waiting_agent(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    q = bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
                 payload={"summary": "uso il tema   scuro?"}, sender_session="f3", task_id="t-1-abc")
    w = json.loads(reg.read_text())["waiting_agent"]
    assert w == {"agent": "alita", "address": "alita/work-hub", "task_id": "t-1-abc",
                 "msg_id": q["id"], "since": w["since"], "summary": "uso il tema scuro?"}
    # the heartbeat keeps it
    bus.register_session("kai", "/w/f3", "f3")
    assert "waiting_agent" in json.loads(reg.read_text())


def test_question_without_task_id_marks_nothing(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
             payload={"summary": "?"}, sender_session="f3")
    assert "waiting_agent" not in json.loads(reg.read_text())


def test_question_inherits_task_id_from_in_reply_to(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    d = bus.send(sender="alita", to="kai/f3", msg_type="request", intent="delega",
                 payload={"summary": "fai"}, sender_session="work-hub", task_id="t-1-abc", kind="delega")
    q = bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
                 payload={"summary": "?"}, sender_session="f3", in_reply_to=d["id"])
    assert q["task_id"] == "t-1-abc"
    assert json.loads(reg.read_text())["waiting_agent"]["task_id"] == "t-1-abc"


def test_reading_the_answer_clears_waiting_agent(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    q = bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
                 payload={"summary": "?"}, sender_session="f3", task_id="t-1-abc")
    a = bus.send(sender="alita", to="kai/f3", msg_type="response", intent="risposta",
                 payload={"summary": "sì"}, sender_session="work-hub", in_reply_to=q["id"])
    data = bus.read_message("kai", a["id"])
    assert bus.clear_waiting_agent("kai", "f3", answer=data) is True
    assert "waiting_agent" not in json.loads(reg.read_text())


def test_unrelated_message_keeps_waiting_agent(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
             payload={"summary": "?"}, sender_session="f3", task_id="t-1-abc")
    m = bus.send(sender="alita", to="kai/f3", msg_type="brief", intent="altro",
                 payload={"summary": "altro"}, sender_session="work-hub", kind="comunicazione")
    assert bus.clear_waiting_agent("kai", "f3", answer=bus.read_message("kai", m["id"])) is False
    assert "waiting_agent" in json.loads(reg.read_text())


def test_sending_the_result_clears_waiting_agent(bus: Bus):
    reg = _registered(bus, "kai", "f3")
    bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
             payload={"summary": "?"}, sender_session="f3", task_id="t-1-abc")
    bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
             payload={"summary": "fatto"}, sender_session="f3", task_id="t-1-abc")
    assert "waiting_agent" not in json.loads(reg.read_text())


def test_thread_entry_carries_in_reply_to_and_task_id(bus: Bus):
    q = bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
                 payload={"summary": "?"}, sender_session="f3", task_id="t-1-abc")
    rec = json.loads(Path(q["path"]).read_text())
    entry = bus.thread(rec["thread_id"])[-1]
    assert entry["task_id"] == "t-1-abc"
    assert "in_reply_to" not in entry


# ---------- BS21: declared kind, delegation = wait (BS21-F1.1, F1.2) ----------

@pytest.mark.parametrize("mtype", ["request", "brief", "handoff"])
def test_declared_types_refuse_a_send_without_kind(bus: Bus, mtype: str):
    with pytest.raises(BusError, match="'delega' if you want the outcome back"):
        bus.send(sender="alita", to="kai", msg_type=mtype, intent="x", payload={"summary": "s"},
                 sender_session="work-hub")
    assert not list((bus.home / "inboxes").rglob("msg-*.json"))


@pytest.mark.parametrize("mtype", ["response", "question", "event"])
def test_returns_need_no_kind(bus: Bus, mtype: str):
    out = bus.send(sender="kai", to="alita", msg_type=mtype, intent="x", payload={"summary": "s"})
    assert "kind" not in json.loads(Path(out["path"]).read_text())


def test_bad_kind_and_delegating_response_refused(bus: Bus):
    with pytest.raises(BusError, match="kind must be"):
        bus.send(sender="alita", to="kai", msg_type="request", intent="x", payload={"summary": "s"},
                 kind="forse")
    with pytest.raises(BusError, match="cannot be a delegation"):
        bus.send(sender="kai", to="alita", msg_type="response", intent="x", payload={"summary": "s"},
                 sender_session="f3", kind="delega")


def test_comunicazione_registers_nothing(bus: Bus):
    out = bus.send(sender="kai", to="alita", msg_type="brief", intent="nota", payload={"summary": "s"},
                   sender_session="f3", kind="comunicazione")
    rec = json.loads(Path(out["path"]).read_text())
    assert rec["kind"] == "comunicazione" and "task_id" not in rec
    assert out["task_id"] is None and "expecting" not in out
    assert not (bus.home / "waits").exists()


def test_delega_registers_the_wait_between_any_two_agents(bus: Bus):
    # kai delegates to alita: no roles, the rule is the same both ways.
    out = bus.send(sender="kai", to="alita/work-hub", msg_type="request", intent="review",
                   payload={"summary": "guarda il diff"}, sender_session="f3", kind="delega",
                   target={"onda_terminal_id": "pane-9"})
    tid = out["task_id"]
    assert tid.startswith("t-") and out["expecting"]["task_id"] == tid
    assert out["note"].startswith(f"attesa registrata {tid}")
    msg = json.loads(Path(out["path"]).read_text())
    assert msg["kind"] == "delega" and msg["task_id"] == tid and msg["requires_response"] is True
    wait = json.loads((bus.home / "waits" / "kai" / "@f3" / f"{tid}.json").read_text())
    assert wait["state"] == "expected" and wait["delegated_to"] == "alita/work-hub"
    assert wait["delegation"] == {"msg_id": msg["id"], "thread_id": msg["thread_id"]}
    assert wait["target"] == {"onda_terminal_id": "pane-9"}


def test_delega_keeps_a_given_task_id_and_deadline(bus: Bus):
    out = bus.send(sender="alita", to="kai", msg_type="brief", intent="x", payload={"summary": "s"},
                   sender_session="work-hub", kind="delega", task_id="t-1-abc",
                   response_by="2026-10-30T18:00:00Z")
    assert out["task_id"] == "t-1-abc"
    wait = json.loads((bus.home / "waits" / "alita" / "@work-hub" / "t-1-abc.json").read_text())
    assert wait["deadline"] == "2026-10-30T18:00:00Z"


def test_failed_delega_leaves_no_wait(bus: Bus):
    with pytest.raises(BusError, match="unknown agent"):
        bus.send(sender="alita", to="nessuno", msg_type="request", intent="x", payload={"summary": "s"},
                 sender_session="work-hub", kind="delega")
    assert not list(bus.home.glob("waits/*/@*/t-*.json"))


def test_delega_needs_the_sender_session(bus: Bus):
    with pytest.raises(BusError, match="sender's session"):
        bus.send(sender="alita", to="kai", msg_type="request", intent="x", payload={"summary": "s"},
                 kind="delega")
