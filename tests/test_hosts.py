"""v2.5 — one bus, two machines. Laptop and mini are two AB_HOMEs under tmp_path;
`ssh` is a fake in PATH that runs the "remote" command locally (in zsh, like a
macOS login shell). No real SSH.

The laptop reaches the mini, never the reverse: laptop → mini is an SSH write
(queued when the mini is down), mini → laptop waits in the mini's outbox until
the laptop collects it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_bus_mcp.bus import Bus, BusError, parse_addr, parse_full_addr  # noqa: E402

FAKE_SSH = r"""#!/bin/bash
while [[ $# -gt 0 ]]; do case "$1" in -o) shift 2;; -*) shift;; *) break;; esac; done
target="$1"; shift
if [[ -e "$FAKE_SSH_DOWN" ]]; then echo "ssh: connect to host $target: refused" >&2; exit 255; fi
echo "$target $*" >> "$FAKE_SSH_LOG"
if command -v zsh >/dev/null 2>&1; then exec zsh -c "$*"; else exec bash -c "$*"; fi
"""


def _map(mini_home: Path) -> dict:
    return {
        "agents": {"alita": {"domain": "side"}, "kai": {"domain": "side"}, "leo": {"domain": "work"}},
        "rules": [],
        "cc_awareness": {"enabled": True, "hub": "alita"},
        "hosts": {"laptop": {"hub": True}, "mini": {"ssh": "mini", "bus_home": str(mini_home)}},
    }


class Net:
    def __init__(self, tmp: Path):
        self.down = tmp / "mini-down"
        self.log = tmp / "ssh.log"

    def mini_down(self) -> None:
        self.down.touch()

    def mini_up(self) -> None:
        self.down.unlink(missing_ok=True)

    def calls(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0


@pytest.fixture()
def net(tmp_path: Path, monkeypatch) -> Net:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("AB_HOST", raising=False)
    n = Net(tmp_path)
    monkeypatch.setenv("FAKE_SSH_DOWN", str(n.down))
    monkeypatch.setenv("FAKE_SSH_LOG", str(n.log))
    return n


@pytest.fixture()
def machines(tmp_path: Path, net: Net) -> tuple[Bus, Bus]:
    laptop, mini = tmp_path / "laptop", tmp_path / "mini"
    for home in (laptop, mini):
        for a in ("alita", "kai", "leo"):
            (home / "inboxes" / a).mkdir(parents=True)
        (home / "AGENT_MAP.json").write_text(json.dumps(_map(mini)), encoding="utf-8")
    (mini / "HOST").write_text("mini\n")  # the laptop has none: the hub host
    return Bus(home=laptop), Bus(home=mini)


def _pending(root: Path) -> list[Path]:
    return [p for p in root.rglob("msg-*.json") if ".read" not in p.parts and ".done" not in p.parts]


# ---------- addressing ----------

def test_full_address_forms():
    assert parse_full_addr("kai") == ("kai", None, None)
    assert parse_full_addr("kai/crewmoor") == ("kai", "crewmoor", None)
    assert parse_full_addr("kai@mini") == ("kai", None, "mini")
    assert parse_full_addr("kai@mini/crewmoor") == ("kai", "crewmoor", "mini")
    assert parse_full_addr("kai/crewmoor@mini") == ("kai", "crewmoor", "mini")


def test_parse_addr_keeps_its_two_tuple_and_drops_host():
    assert parse_addr("kai@mini/crewmoor") == ("kai", "crewmoor")


def test_who_am_i(machines):
    laptop, mini = machines
    assert laptop.host_self() == "laptop"
    assert mini.host_self() == "mini"


def test_where_a_message_goes(machines):
    laptop, mini = machines
    assert laptop.resolve_host("kai") is None
    assert laptop.resolve_host("kai", "mini") == "mini"
    assert laptop.resolve_host("kai", "laptop") is None
    assert mini.resolve_host("alita") == "laptop"  # the hub agent lives on the hub host
    assert mini.resolve_host("kai") is None


def test_unknown_host_is_a_clear_error(machines):
    laptop, _ = machines
    with pytest.raises(BusError, match="unknown host 'marte'"):
        laptop.send("alita", "kai@marte", "request", "x", {"summary": "s"})


# ---------- laptop → mini ----------

def test_laptop_writes_straight_into_the_mini_inbox(machines):
    laptop, mini = machines
    res = laptop.send("alita", "kai@mini/crewmoor", "brief", "x", {"summary": "ciao"})
    assert res["delivered_to"] == "kai@mini/crewmoor" and res["queued"] is False
    f = mini.session_dir("kai", "crewmoor") / f"{res['id']}.json"
    rec = json.loads(f.read_text())
    assert rec["context"]["host"] == "laptop" and rec["to"] == "kai"
    assert (f.parent / ".done").is_dir()
    assert _pending(laptop.home / "inboxes") == []
    assert not list(mini.home.rglob("*.tmp"))


def test_mini_down_queues_and_does_not_fail(machines, net):
    laptop, mini = machines
    net.mini_down()
    res = laptop.send("alita", "kai@mini", "request", "x", {"summary": "mentre dormi"})
    assert res["queued"] is True and "unreachable" in res["warning"]
    assert (laptop.home / "outbox" / "mini" / "kai" / f"{res['id']}.json").is_file()
    assert _pending(mini.home / "inboxes") == []


# ---------- mini → laptop ----------

def test_mini_leaves_messages_for_the_laptop_in_its_outbox(machines):
    _, mini = machines
    res = mini.send("kai", "alita", "question", "x", {"summary": "per alita"}, sender_session="crewmoor")
    assert res["queued"] is True and res["host"] == "laptop"
    rec = json.loads((mini.home / "outbox" / "laptop" / "alita" / f"{res['id']}.json").read_text())
    assert rec["context"]["host"] == "mini" and rec["from_session"] == "kai/crewmoor"


def test_session_slug_travels_in_the_path(machines):
    _, mini = machines
    res = mini.send("kai", "kai@laptop/agent-team-os", "event", "x", {"summary": "s"})
    assert (mini.home / "outbox" / "laptop" / "kai" / "@agent-team-os" / f"{res['id']}.json").is_file()


def test_mini_to_mini_stays_local(machines):
    _, mini = machines
    res = mini.send("alita", "kai", "event", "x", {"summary": "s"})
    assert res["queued"] is False and Path(res["path"]).is_file()


# ---------- collection ----------

def test_collect_with_mini_down_loses_nothing(machines, net):
    laptop, mini = machines
    net.mini_down()
    laptop.send("alita", "kai@mini", "request", "x", {"summary": "q"})
    mini.send("kai", "alita", "request", "x", {"summary": "m"})
    [res] = laptop.collect(force=True)
    assert res["status"] == "unreachable" and res["queued"] == 1
    assert len(_pending(laptop.home / "outbox" / "mini")) == 1
    assert len(_pending(mini.home / "outbox" / "laptop")) == 1


def test_collect_pushes_the_queue_and_pulls_the_outbox(machines, net):
    laptop, mini = machines
    net.mini_down()
    q = laptop.send("alita", "kai@mini/crewmoor", "request", "x", {"summary": "q"})
    net.mini_up()
    m = mini.send("kai", "alita", "request", "x", {"summary": "m"}, priority="urgent")
    s = mini.send("kai", "kai@laptop/agent-team-os", "event", "x", {"summary": "s"})

    [res] = laptop.collect(force=True)
    assert (res["status"], res["pushed"], res["pulled"], res["queued"]) == ("ok", 1, 2, 0)
    assert (mini.session_dir("kai", "crewmoor") / f"{q['id']}.json").is_file()
    assert (laptop.inbox_root("alita") / f"{m['id']}.json").is_file()
    assert (laptop.session_dir("kai", "agent-team-os") / f"{s['id']}.json").is_file()
    assert _pending(mini.home / "outbox" / "laptop") == []
    assert [e["id"] for e in laptop.thread(json.loads(
        (laptop.inbox_root("alita") / f"{m['id']}.json").read_text())["thread_id"])] == [m["id"]]


def test_pull_is_idempotent_and_keeps_what_it_cannot_read(machines):
    laptop, mini = machines
    m = mini.send("kai", "alita", "request", "x", {"summary": "m"})
    laptop.collect(force=True)
    # The remote delete failed last time: the same file is back on the mini.
    out = mini.home / "outbox" / "laptop" / "alita"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(laptop.inbox_root("alita") / f"{m['id']}.json", out)
    (out / "msg-20990101T000000Z-broken01.json").write_text("{rotto")
    [res] = laptop.collect(force=True)
    assert res["pulled"] == 0
    assert len(laptop.list_inbox("alita")) == 1
    assert not (out / f"{m['id']}.json").exists()
    assert (out / "msg-20990101T000000Z-broken01.json").exists()


def test_archived_message_is_not_reborn(machines):
    laptop, mini = machines
    m = mini.send("kai", "alita", "request", "x", {"summary": "m"})
    laptop.collect(force=True)
    laptop.archive_message("alita", m["id"])
    out = mini.home / "outbox" / "laptop" / "alita"
    shutil.copy(laptop.inbox_root("alita") / ".done" / f"{m['id']}.json", out)
    laptop.collect(force=True)
    assert laptop.list_inbox("alita") == []


def test_collection_is_throttled(machines, net):
    laptop, _ = machines
    laptop.collect(force=True)
    before = net.calls()
    assert laptop.collect() == []
    assert net.calls() == before


def test_the_mini_never_tries_to_reach_the_laptop(machines, net):
    _, mini = machines
    assert mini.remote_hosts() == []
    assert mini.collect(force=True) == []
    assert net.calls() == 0


def test_bash_and_python_share_the_collector_lock(machines):
    laptop, mini = machines
    mini.send("kai", "alita", "request", "x", {"summary": "m"})
    (laptop.home / "locks" / "collect-mini.lock").mkdir(parents=True)
    assert laptop.collect(force=True) == []  # someone else is collecting
    assert laptop.list_inbox("alita") == []


# ---------- replies ----------

def test_reply_goes_back_to_the_session_on_the_mini(machines):
    laptop, mini = machines
    m = mini.send("kai", "alita", "question", "x", {"summary": "?"}, sender_session="crewmoor")
    laptop.collect(force=True)
    rec = laptop.read_message("alita", m["id"])
    assert laptop.reply_address(rec) == "kai@mini/crewmoor"
    r = laptop.send("alita", laptop.reply_address(rec), "response", "x", {"summary": "!"},
                    thread=rec["thread_id"], in_reply_to=m["id"])
    back = json.loads((mini.session_dir("kai", "crewmoor") / f"{r['id']}.json").read_text())
    assert back["thread_id"] == rec["thread_id"] and back["in_reply_to"] == m["id"]


def test_reply_address_on_one_machine_is_unchanged(machines):
    laptop, _ = machines
    assert laptop.reply_address({"from": "kai", "context": {"host": "laptop"}}) == "kai"
    assert laptop.reply_address({"from": "kai"}) == "kai"


# ---------- status ----------

def test_status_lists_the_mini_sessions(machines, net):
    laptop, mini = machines
    mini.register_session("kai", "/x/Projects/crewmoor")
    laptop.send("alita", "kai@mini/crewmoor", "request", "x", {"summary": "s"})
    laptop.send("alita", "kai@mini", "event", "x", {"summary": "b"})
    [s] = laptop.remote_sessions("mini")
    assert (s["name"], s["slug"], s["host"], s["pending"]) == ("kai", "crewmoor", "mini", 2)
    assert json.loads((mini.registry_dir("kai") / "crewmoor.json").read_text())["host"] == "mini"


def test_status_is_cached_and_survives_the_mini_going_down(machines, net):
    laptop, mini = machines
    mini.register_session("kai", "/x/Projects/crewmoor")
    laptop.remote_status("mini")
    net.mini_down()
    before = net.calls()
    assert [s["slug"] for s in laptop.remote_sessions("mini")] == ["crewmoor"]
    assert net.calls() == before  # served from the 20 s cache
    snap = laptop.remote_status("mini", force=True)
    assert snap["ok"] is False and snap["sessions"][0]["slug"] == "crewmoor"


def test_bus_status_tool_shows_hosts(machines, monkeypatch, tmp_path):
    laptop, mini = machines
    from agent_bus_mcp import server

    mini.register_session("kai", "/x/Projects/crewmoor")
    monkeypatch.setenv("AB_HOME", str(laptop.home))
    monkeypatch.setenv("AB_AGENT", "alita")
    monkeypatch.setenv("AB_SESSION_SLUG", "work-hub")
    monkeypatch.setenv("AB_CWD", str(tmp_path))
    fn = getattr(server.bus_status, "fn", server.bus_status)
    out = fn()
    assert out["me"]["host"] == "laptop"
    kai = next(r for r in out["roster"] if r["agent"] == "kai")
    assert [s["address"] for s in kai["live_sessions"]] == ["kai@mini/crewmoor"]
    assert out["hosts"]["mini"]["reachable"] is True


# ---------- one machine: unchanged ----------

def test_single_machine_bus_is_unchanged(tmp_path, net):
    home = tmp_path / "solo"
    (home / "inboxes" / "kai").mkdir(parents=True)
    (home / "AGENT_MAP.json").write_text(json.dumps({"agents": {"alita": {}, "kai": {}}}))
    bus = Bus(home=home)
    res = bus.send("alita", "kai", "request", "x", {"summary": "s"})
    assert "context" not in json.loads(Path(res["path"]).read_text())
    assert "host" not in bus.register_session("kai", "/x/solo")
    with pytest.raises(BusError, match="unknown host"):
        bus.send("alita", "kai@mini", "request", "x", {"summary": "s"})
    assert bus.collect(force=True) == [] and net.calls() == 0
