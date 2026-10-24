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


# ---------- BS21-F2.2, F2.3: explicit consumption with bus_gestisci ----------

def _delegation(bus: Bus) -> tuple[dict, dict]:
    d = bus.send(sender="alita", to="kai/f3", msg_type="request", intent="spike",
                 payload={"summary": "fai"}, sender_session="work-hub", kind="delega")
    r = bus.send(sender="kai", to="alita/work-hub", msg_type="response", intent="esito",
                 payload={"summary": "a meta'"}, sender_session="f3", in_reply_to=d["id"])
    return d, r


def _wait(bus: Bus, tid: str) -> dict:
    return json.loads((bus.home / "waits" / "alita" / "@work-hub" / f"{tid}.json").read_text())


def test_chiuso_closes_the_wait_and_files_the_message(bus: Bus):
    d, r = _delegation(bus)
    out = bus.handle("alita", "work-hub", r["id"], "chiuso")
    assert out["wait"] == {"task_id": d["task_id"], "state": "closed"}
    assert bus.count_inbox("alita", "work-hub") == 0
    filed = json.loads(Path(out["archived_to"]).read_text())
    assert Path(out["archived_to"]).parent.name == ".done"
    assert filed["handled"]["esito"] == "chiuso" and filed["handled"]["by"] == "alita/work-hub"
    w = _wait(bus, d["task_id"])
    assert w["closed_by"] == r["id"] and w["returns"][r["id"]]["state"] == "handled"


def test_riarma_keeps_waiting_and_sends_nothing(bus: Bus):
    d, r = _delegation(bus)
    before = len(list((bus.home / "inboxes" / "kai").rglob("msg-*.json")))
    out = bus.handle("alita", "work-hub", r["id"], "riarma")
    assert out["wait"]["state"] == "expected" and "sent" not in out
    assert len(list((bus.home / "inboxes" / "kai").rglob("msg-*.json"))) == before
    assert "rearmed_at" in _wait(bus, d["task_id"])


def test_riarma_reopens_a_closed_wait(bus: Bus):
    d, r = _delegation(bus)
    bus.handle("alita", "work-hub", r["id"], "chiuso")
    r2 = bus.send(sender="kai", to="alita/work-hub", msg_type="event", intent="ancora",
                  payload={"summary": "un'altra cosa"}, sender_session="f3", task_id=d["task_id"])
    out = bus.handle("alita", "work-hub", r2["id"], "riarma")
    assert out["wait"]["state"] == "expected"
    assert _wait(bus, d["task_id"])["reopened_from"] == "closed"


def test_risposto_answers_with_the_task_id_and_keeps_the_wait(bus: Bus):
    d, _ = _delegation(bus)
    q = bus.send(sender="kai", to="alita/work-hub", msg_type="question", intent="domanda",
                 payload={"summary": "tema scuro?"}, sender_session="f3", task_id=d["task_id"])
    out = bus.handle("alita", "work-hub", q["id"], "risposto", reply="sì, scuro")
    assert out["wait"]["state"] == "expected"
    ans = json.loads(next((bus.home / "inboxes" / "kai" / "@f3").glob(f"{out['sent']['id']}.json")).read_text())
    assert ans["type"] == "response" and ans["task_id"] == d["task_id"] and ans["in_reply_to"] == q["id"]
    assert ans["payload"]["summary"] == "sì, scuro"
    with pytest.raises(BusError, match="needs the answer text"):
        bus.handle("alita", "work-hub", _delegation(bus)[1]["id"], "risposto")


def test_letto_files_to_read_and_leaves_the_wait(bus: Bus):
    d, r = _delegation(bus)
    out = bus.handle("alita", "work-hub", r["id"], "letto")
    assert Path(out["archived_to"]).parent.name == ".read"
    assert _wait(bus, d["task_id"])["state"] == "expected"


def test_handle_refuses_what_it_cannot_do(bus: Bus):
    m = bus.send(sender="kai", to="alita/work-hub", msg_type="event", intent="x", payload={"summary": "s"})
    with pytest.raises(BusError, match="riarma needs a return"):
        bus.handle("alita", "work-hub", m["id"], "riarma")
    with pytest.raises(BusError, match="esito must be"):
        bus.handle("alita", "work-hub", m["id"], "boh")
    bus.handle("alita", "work-hub", m["id"], "chiuso")  # a plain message: just filed as handled
    with pytest.raises(BusError, match="already filed"):
        bus.handle("alita", "work-hub", m["id"], "letto")


def test_da_agganciare_is_decided_by_the_receiver(bus: Bus):
    d1, _ = _delegation(bus)
    d2 = bus.send(sender="alita", to="kai/f3", msg_type="brief", intent="altro",
                  payload={"summary": "altro"}, sender_session="work-hub", kind="delega")
    m = bus.send(sender="kai", to="alita/work-hub", msg_type="event", intent="x", payload={"summary": "?"},
                 sender_session="f3")
    p = Path(m["path"])
    rec = json.loads(p.read_text())
    rec["task_link"] = {"by": "da_agganciare", "candidates": [d1["task_id"], d2["task_id"]]}
    p.write_text(json.dumps(rec))
    with pytest.raises(BusError, match="say which task"):
        bus.handle("alita", "work-hub", m["id"], "chiuso")
    with pytest.raises(BusError, match="not a delegation of alita"):
        bus.handle("alita", "work-hub", m["id"], "chiuso", task_id="t-20261021-altrui")
    out = bus.handle("alita", "work-hub", m["id"], "chiuso", task_id=d2["task_id"])
    assert out["wait"] == {"task_id": d2["task_id"], "state": "closed"}
    filed = json.loads(Path(out["archived_to"]).read_text())
    assert filed["task_id"] == d2["task_id"] and filed["task_link"]["by"] == "scelto"
    assert _wait(bus, d1["task_id"])["state"] == "expected"


# ---------- BR24 passo 0: casi del 24 ottobre ----------

def _register(bus: Bus, agent: str, slug: str, age_min: float = 0) -> None:
    d = bus.home / "registry" / f"{agent}.d"
    d.mkdir(parents=True, exist_ok=True)
    seen = (datetime.now(timezone.utc) - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / f"{slug}.json").write_text(json.dumps({"name": agent, "slug": slug, "last_seen": seen}))


def test_br24_close_wait_by_task_id_with_returns_already_filed(bus: Bus):
    # caso iv: ritorni gia' archiviati, bus_gestisci rifiutava ("already filed")
    d, r = _delegation(bus)
    bus.archive_message("alita", r["id"], "done")
    with pytest.raises(BusError, match="already filed"):
        bus.handle("alita", "work-hub", r["id"], "chiuso")
    out = bus.close_wait("alita", "work-hub", d["task_id"], note="lavoro concluso")
    assert (out["state"], out["was"], out["filed"]) == ("closed", "expected", [])
    w = _wait(bus, d["task_id"])
    assert w["state"] == "closed" and w["note"] == "lavoro concluso" and w["closed_by"] == "close:alita/work-hub"


def test_br24_close_wait_files_pending_returns_and_works_from_another_session(bus: Bus):
    # t-20261021-xn9lv0: una delega di una sessione precedente, chiusa dalla sessione di oggi
    d, r = _delegation(bus)
    out = bus.close_wait("alita", "nuova", d["task_id"])
    assert out["state"] == "closed" and [f["id"] for f in out["filed"]] == [r["id"]]
    assert bus.count_inbox("alita", "work-hub") == 0
    assert _wait(bus, d["task_id"])["returns"][r["id"]]["handled"] == "chiuso"
    with pytest.raises(BusError, match="not a delegation of kai"):
        bus.close_wait("kai", "f3", d["task_id"])


def test_br24_server_gestisci_without_message_id_closes(bus: Bus, monkeypatch):
    from agent_bus_mcp import server
    d, _ = _delegation(bus)
    monkeypatch.setenv("AB_AGENT", "alita")
    monkeypatch.setenv("AB_SESSION_SLUG", "work-hub")
    monkeypatch.setenv("AB_CWD", str(bus.home))
    monkeypatch.setattr(server, "_bus", lambda: bus)
    fn = getattr(server.bus_gestisci, "fn", server.bus_gestisci)
    with pytest.raises(BusError, match="without message_id"):
        fn(esito="letto", task_id=d["task_id"])
    out = fn(esito="chiuso", task_id=d["task_id"])
    assert out["state"] == "closed" and out["remaining"] == 0


def test_br24_wait_belongs_to_its_session_while_it_lives(bus: Bus):
    # caso iii: bus_read diceva a kai/orbit "hai delegato" per l'attesa di kai/agent-team-os
    d, r = _delegation(bus)
    _register(bus, "alita", "work-hub")
    assert bus.own_wait("alita", d["task_id"], "work-hub") is not None
    assert bus.own_wait("alita", d["task_id"], "altra") is None
    m = bus.send(sender="kai", to="alita", msg_type="event", intent="x", payload={"summary": "s"},
                 sender_session="f3", task_id=d["task_id"])
    with pytest.raises(BusError, match="riarma needs a return"):
        bus.handle("alita", "altra", m["id"], "riarma")
    # la sessione che ha delegato non c'e' piu': l'attesa passa a chi c'e'
    _register(bus, "alita", "work-hub", age_min=300)
    assert bus.own_wait("alita", d["task_id"], "altra") is not None
    assert bus.handle("alita", "altra", r["id"], "chiuso")["wait"]["state"] == "closed"


def test_br24_handle_keeps_what_the_wake_round_wrote(bus: Bus):
    # F2.4: l'attesa si rilegge dentro il lock, non si riscrive la copia letta prima
    d, r = _delegation(bus)
    f = bus.home / "waits" / "alita" / "@work-hub" / f"{d['task_id']}.json"
    real = bus.own_wait

    def stale(agent, task_id, slug=None):
        got = real(agent, task_id, slug)
        if got is not None:  # il giro della sveglia scrive dopo la lettura
            rec = json.loads(f.read_text())
            rec["executor_session"] = "kai/f3"
            rec.setdefault("returns", {})["m-x"] = {"state": "pending", "pending_wake": {"reason": "tetto_task"}}
            f.write_text(json.dumps(rec))
        return got
    bus.own_wait = stale
    bus.handle("alita", "work-hub", r["id"], "chiuso")
    w = _wait(bus, d["task_id"])
    assert w["state"] == "closed" and w["executor_session"] == "kai/f3" and "m-x" in w["returns"]
    assert "pending_wake" not in w  # F2.7: un'attesa chiusa non ha rimandi


def test_br24_onda_session_counts_as_live(bus: Bus):
    # caso ii: "no live session 'kai/orbit'" con la sessione al lavoro in Onda
    import os
    _register(bus, "kai", "onda-electron")
    (bus.home / "onda").mkdir()
    doc = {"v": 1, "ondaPid": os.getpid(), "writtenAt": datetime.now(timezone.utc).isoformat(),
           "sessions": [{"agent": "kai", "slug": "orbit", "state": "working", "cwd": "/p/Orbit"},
                        {"agent": "kai", "slug": "vecchia", "state": "exited"}]}
    (bus.home / "onda" / "sessions.json").write_text(json.dumps(doc))
    assert {s["slug"] for s in bus.list_sessions("kai")} == {"onda-electron", "orbit"}
    out = bus.send(sender="alita", to="kai/orbit", msg_type="brief", intent="x", payload={"summary": "s"},
                   sender_session="work-hub", kind="comunicazione")
    assert not out.get("warning")
    out = bus.send(sender="alita", to="kai/vecchia", msg_type="brief", intent="x", payload={"summary": "s"},
                   sender_session="work-hub", kind="comunicazione")
    assert "no live session 'kai/vecchia'" in out["warning"]
    # Onda spento: solo il registro
    doc["ondaPid"] = 999999
    (bus.home / "onda" / "sessions.json").write_text(json.dumps(doc))
    assert {s["slug"] for s in bus.list_sessions("kai")} == {"onda-electron"}
