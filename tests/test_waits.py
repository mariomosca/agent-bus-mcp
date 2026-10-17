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
                   payload={"summary": "fai lo spike"}, sender_session="work-hub", task_id="t-1-abc")
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
    assert len(bus.list_waits("alita", None, include_closed=True)) == 2
