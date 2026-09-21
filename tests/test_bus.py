"""Tests for the bus filesystem layer, on a throwaway AB_HOME.

The case that matters: two sessions of the same agent must not see each other's
targeted messages. That is the bug this whole layer exists to prevent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bus_mcp.bus import Bus, BusError, parse_addr, slug_for_path  # noqa: E402

AGENT_MAP = {
    "agents": {
        "alita": {"role": "hub", "domain": "side"},
        "kai": {"role": "tech", "domain": "side"},
        "nico": {"role": "tech (isolated domain)", "domain": "work"},
    },
    "rules": [
        {"pattern": "/w/work-hub", "agent": "alita"},
        {"pattern": "/w/Projects/02-Experiments", "agent": "kai"},
        {"pattern": "/w/Projects/07-Tooling", "agent": "kai"},
    ],
    "routing_rules": {"nico": {"deny": ["kai"], "reason": "isolation"}},
}


@pytest.fixture()
def bus(tmp_path: Path, monkeypatch) -> Bus:
    monkeypatch.setenv("AB_HOME", str(tmp_path))
    (tmp_path / "inboxes" / "kai").mkdir(parents=True)
    (tmp_path / "inboxes" / "alita").mkdir(parents=True)
    (tmp_path / "AGENT_MAP.json").write_text(json.dumps(AGENT_MAP), encoding="utf-8")
    return Bus(home=tmp_path)


# ---------- addressing ----------

def test_parse_addr_splits_session():
    assert parse_addr("kai/noi-calendar") == ("kai", "noi-calendar")
    assert parse_addr("kai") == ("kai", None)


def test_slug_is_basename_lowercased():
    assert slug_for_path("/w/Projects/02-Experiments/Noi-Calendar") == "noi-calendar"


def test_detect_agent_prefers_longest_prefix(bus: Bus):
    assert bus.detect_agent("/w/Projects/02-Experiments/noi-calendar") == "kai"
    assert bus.detect_agent("/w/work-hub") == "alita"
    assert bus.detect_agent("/elsewhere") is None


# ---------- the bug ----------

def test_two_sessions_do_not_overwrite_each_other(bus: Bus):
    bus.register_session("kai", "/w/Projects/07-Tooling/mcp/microsoft-mcp")
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    assert {s["slug"] for s in bus.list_sessions("kai")} == {"microsoft-mcp", "noi-calendar"}


def test_targeted_message_reaches_only_its_session(bus: Bus):
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    bus.register_session("kai", "/w/Projects/07-Tooling/mcp/microsoft-mcp")
    bus.send("alita", "kai/noi-calendar", "brief", "new-app", {"summary": "for noi-calendar"})

    assert bus.count_inbox("kai", "noi-calendar") == 1
    assert bus.count_inbox("kai", "microsoft-mcp") == 0


def test_broadcast_is_visible_to_every_session(bus: Bus):
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    bus.register_session("kai", "/w/Projects/07-Tooling/mcp/microsoft-mcp")
    bus.send("alita", "kai", "event", "event", {"summary": "for everyone"})

    assert bus.count_inbox("kai", "noi-calendar") == 1
    assert bus.count_inbox("kai", "microsoft-mcp") == 1


def test_session_sees_its_own_plus_broadcast(bus: Bus):
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    bus.send("alita", "kai/noi-calendar", "brief", "new-app", {"summary": "mine"})
    bus.send("alita", "kai", "event", "event", {"summary": "shared"})

    seen = {
        bus.read_message("kai", p.stem)["payload"]["summary"]
        for p in bus.list_inbox("kai", "noi-calendar")
    }
    assert seen == {"mine", "shared"}


# ---------- delivery mechanics ----------

def test_recipient_field_keeps_bare_agent_name(bus: Bus):
    res = bus.send("alita", "kai/noi-calendar", "brief", "x", {"summary": "s"})
    assert json.loads(Path(res["path"]).read_text())["to"] == "kai"


def test_session_dir_is_born_with_its_archives(bus: Bus):
    bus.send("alita", "kai/never-seen", "brief", "x", {"summary": "s"})
    d = bus.session_dir("kai", "never-seen")
    assert (d / ".read").is_dir() and (d / ".done").is_dir()


def test_unknown_live_session_warns_but_still_delivers(bus: Bus):
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    res = bus.send("alita", "kai/typo-here", "brief", "x", {"summary": "s"})
    assert res["warning"] is not None and "typo-here" in res["warning"]
    assert Path(res["path"]).is_file()


def test_unknown_agent_is_refused(bus: Bus):
    with pytest.raises(BusError, match="unknown agent"):
        bus.send("alita", "nobody", "brief", "x", {"summary": "s"})


def test_routing_deny_is_enforced(bus: Bus):
    with pytest.raises(BusError, match="routing denied"):
        bus.send("nico", "kai", "brief", "x", {"summary": "s"})


# ---------- archiving ----------

def test_archive_keeps_message_in_its_own_session_dir(bus: Bus):
    bus.send("alita", "kai/noi-calendar", "brief", "x", {"summary": "s"})
    mid = bus.list_inbox("kai", "noi-calendar")[0].stem
    dest = bus.archive_message("kai", mid, "done")

    assert dest.parent == bus.session_dir("kai", "noi-calendar") / ".done"
    assert bus.count_inbox("kai", "noi-calendar") == 0


def test_archiving_twice_is_not_an_error(bus: Bus):
    bus.send("alita", "kai", "brief", "x", {"summary": "s"})
    mid = bus.list_inbox("kai")[0].stem
    first = bus.archive_message("kai", mid, "done")
    assert bus.archive_message("kai", mid, "done") == first


def test_read_finds_archived_messages(bus: Bus):
    bus.send("alita", "kai/noi-calendar", "brief", "x", {"summary": "findable"})
    mid = bus.list_inbox("kai", "noi-calendar")[0].stem
    bus.archive_message("kai", mid, "done")
    assert bus.read_message("kai", mid)["payload"]["summary"] == "findable"


def test_read_reports_where_the_message_lives(bus: Bus):
    bus.send("alita", "kai/noi-calendar", "brief", "x", {"summary": "s"})
    mid = bus.list_inbox("kai", "noi-calendar")[0].stem
    loc = bus.read_message("kai", mid)["_location"]
    assert loc["scope"] == "noi-calendar" and loc["state"] == "pending"


def test_missing_message_is_refused(bus: Bus):
    with pytest.raises(BusError, match="no message matching"):
        bus.read_message("kai", "msg-does-not-exist")


# ---------- threads ----------

def test_thread_replays_in_order(bus: Bus):
    first = bus.send("alita", "kai", "request", "x", {"summary": "one"})
    tid = json.loads(Path(first["path"]).read_text())["thread_id"]
    bus.send("alita", "kai", "request", "x", {"summary": "two"}, thread=tid)

    assert [e["summary"] for e in bus.thread(tid)] == ["one", "two"]


def test_stale_session_is_hidden(bus: Bus, monkeypatch):
    bus.register_session("kai", "/w/Projects/02-Experiments/noi-calendar")
    f = bus.registry_dir("kai") / "noi-calendar.json"
    rec = json.loads(f.read_text())
    rec["last_seen"] = "2020-01-01T00:00:00Z"
    f.write_text(json.dumps(rec))

    assert bus.list_sessions("kai") == []
    assert len(bus.list_sessions("kai", include_stale=True)) == 1


# ---------- caller identity (found by using the server for real) ----------

def test_unexpanded_placeholder_is_ignored(bus: Bus, monkeypatch, tmp_path):
    """A config that wrote "${CLAUDE_PROJECT_DIR}" literally must not be trusted:
    it maps to no agent and would silently land on a wrong fallback."""
    from agent_bus_mcp.server import _caller_cwd

    monkeypatch.setenv("AB_CWD", "${CLAUDE_PROJECT_DIR:-${PWD}}")
    monkeypatch.chdir(tmp_path)
    assert _caller_cwd() == str(tmp_path)


def test_real_path_in_env_wins_over_cwd(monkeypatch, tmp_path):
    from agent_bus_mcp.server import _caller_cwd

    real = tmp_path / "workspace"
    real.mkdir()
    monkeypatch.setenv("AB_CWD", str(real))
    assert _caller_cwd() == str(real)


def test_server_own_directory_is_refused(monkeypatch):
    """Being launched in our own folder would attribute every call to whichever
    agent owns it — better to fail with an actionable message."""
    from agent_bus_mcp.server import _SERVER_DIR, _caller_cwd

    monkeypatch.delenv("AB_CWD", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(_SERVER_DIR)
    with pytest.raises(BusError, match="own directory"):
        _caller_cwd()
