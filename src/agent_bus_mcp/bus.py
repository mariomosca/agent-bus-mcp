"""Filesystem layer of the Agent Team OS bus.

Mirrors the semantics of scripts/agent-team-os-lib.sh (v1.4, session identity).
Kept free of MCP concerns so it can be tested on its own.

Layout::

    ~/.agent-team-os/
      AGENT_MAP.json
      HOST                               this machine's name (v2.5, optional)
      registry/<agent>.d/<slug>.json     one entry per live session
      inboxes/<agent>/msg-*.json         broadcast: every session sees these
      inboxes/<agent>/@<slug>/msg-*.json one session only
                              .read/ .done/
      threads/<thread-id>.jsonl
      outbox/<agent>.jsonl
      outbox/<host>/<agent>/[@<slug>/]msg-*.json   not yet delivered to <host> (v2.5)
      hosts/<host>/last-collect[.json] status.json  collection stamp, remote status cache

Two machines (v2.5): the laptop (hub) and the mini share paths and AGENT_MAP.json.
Only the laptop opens SSH, so laptop → mini is an SSH write into the mini's inbox
(queued in outbox/mini/ when the mini is down) and mini → laptop is a file in the
mini's outbox/laptop/ that the laptop pulls. Same semantics as the bash lib.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A session with no heartbeat for this long is treated as gone. Matches the
# bash lib's AB_SESSION_TTL_MIN default.
SESSION_TTL_MIN = int(os.environ.get("AB_SESSION_TTL_MIN", "240"))

SLUG_SAFE = re.compile(r"[^a-z0-9._-]")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

# v2.6: a message sent within this many minutes of a wake carries wake_chain.
# Same name and default as agent-team-os-wake.py.
WAKE_CHAIN_MIN = int(os.environ.get("AB_WAKE_CHAIN_MIN", "30"))

# v2.5 transport knobs, same names and defaults as the bash lib.
SSH_BIN = os.environ.get("AB_SSH_BIN", "ssh")
SSH_TIMEOUT = int(os.environ.get("AB_SSH_TIMEOUT", "5"))
COLLECT_INTERVAL = int(os.environ.get("AB_COLLECT_INTERVAL", "30"))
REMOTE_STATUS_TTL = int(os.environ.get("AB_REMOTE_STATUS_TTL", "20"))


def bus_home() -> Path:
    return Path(os.environ.get("AB_HOME", str(Path.home() / ".agent-team-os")))


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"msg-{stamp}-{rand}"


def thread_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"thread-{stamp}-{rand}"


def new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"t-{stamp}-{rand}"


def slug_for_path(path: str | os.PathLike[str]) -> str:
    """Workspace slug: basename, lowercased, non-alphanumerics folded to '-'."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    base = p.name or "root"
    return SLUG_SAFE.sub("-", base.lower())


def parse_full_addr(addr: str) -> tuple[str, str | None, str | None]:
    """Agent, slug, host of an address.

    'kai' -> ('kai', None, None); 'kai/crewmoor' -> ('kai', 'crewmoor', None);
    'kai@mini/crewmoor' and 'kai/crewmoor@mini' -> ('kai', 'crewmoor', 'mini').
    Slugs never contain '@' (slug_for_path folds it), so the split is unambiguous.
    """
    addr = addr.strip()
    head, sep, tail = addr.partition("/")
    host = None
    if "@" in head:
        head, _, host = head.partition("@")
    elif "@" in tail:
        tail, _, host = tail.rpartition("@")
    slug = tail.strip() if sep else ""
    return head.strip(), (slug or None), ((host or "").strip() or None)


def parse_addr(addr: str) -> tuple[str, str | None]:
    """'kai/noi-calendar' -> ('kai', 'noi-calendar'); 'kai' -> ('kai', None).

    The host part, if any, is dropped: see parse_full_addr.
    """
    agent, slug, _ = parse_full_addr(addr)
    return agent, slug


def _read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class BusError(RuntimeError):
    """Refusal that is the caller's to fix (unknown agent, denied route, ...)."""


@dataclass
class Bus:
    home: Path = field(default_factory=bus_home)

    # ---------- agents ----------

    @property
    def map_path(self) -> Path:
        return self.home / "AGENT_MAP.json"

    def agent_map(self) -> dict:
        return _read_json(self.map_path) or {}

    def agents(self) -> dict:
        return self.agent_map().get("agents", {})

    def agent_exists(self, agent: str) -> bool:
        return agent in self.agents()

    def detect_agent(self, cwd: str | os.PathLike[str]) -> str | None:
        """Resolve agent from a path using AGENT_MAP rules (longest prefix wins).

        The bash lib takes the first matching rule; matching the longest prefix
        instead is strictly safer when rules nest, and identical otherwise.
        """
        raw = str(cwd)
        try:
            resolved = str(Path(raw).expanduser().resolve())
        except OSError:
            resolved = raw
        best: tuple[int, str] | None = None
        for rule in self.agent_map().get("rules", []):
            pattern = rule.get("pattern", "")
            if not pattern:
                continue
            # trailing "/" so the root of a rule like ".../07-Tooling/" matches too
            if (raw + "/").startswith(pattern) or (resolved + "/").startswith(pattern):
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), rule.get("agent", ""))
        return best[1] if best and best[1] else None

    # ---------- hosts (v2.5) ----------

    def hosts(self) -> dict:
        return self.agent_map().get("hosts") or {}

    def host_self(self) -> str | None:
        """This machine: AB_HOST, else the HOST file, else the hub host, else None."""
        env = os.environ.get("AB_HOST", "").strip()
        if env:
            return env
        f = self.home / "HOST"
        try:
            name = f.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if name:
                return name
        except (OSError, IndexError):
            pass
        return self.hub_host()

    def hub_host(self) -> str | None:
        for name, conf in self.hosts().items():
            if (conf or {}).get("hub") is True:
                return name
        return None

    def host_bus_home(self, host: str) -> str:
        return (self.hosts().get(host) or {}).get("bus_home") or str(self.home)

    def remote_hosts(self) -> list[str]:
        """Hosts this machine opens SSH to (an `ssh` target, and not us)."""
        me = self.host_self()
        return sorted(h for h, c in self.hosts().items() if h != me and (c or {}).get("ssh"))

    def resolve_host(self, agent: str, want: str | None = None) -> str | None:
        """Host a message must travel to; None = this machine.

        Explicit @host wins; then agents.<a>.host; then the hub agent on the hub
        host; otherwise here. An explicit unknown host is refused.
        """
        me = self.host_self()
        if want:
            if want == me:
                return None
            if want not in self.hosts():
                known = ", ".join(sorted(self.hosts())) or 'none ("hosts" missing in AGENT_MAP.json)'
                raise BusError(f"unknown host '{want}'. Known: {known}")
            return want
        pinned = (self.agents().get(agent) or {}).get("host")
        if not pinned:
            hub_agent = (self.agent_map().get("cc_awareness") or {}).get("hub")
            if hub_agent and agent == hub_agent:
                pinned = self.hub_host()
        if not pinned or pinned == me or pinned not in self.hosts():
            return None
        return pinned

    def ssh(self, host: str, command: str, data: bytes | None = None) -> subprocess.CompletedProcess | None:
        """Run a command on a host with the bus's SSH options; None if it cannot run.

        BatchMode never prompts; ControlMaster keeps one connection for 10 minutes
        so a call costs milliseconds. The remote shell may be zsh: no globs.
        """
        target = (self.hosts().get(host) or {}).get("ssh")
        if not target:
            return None
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(exist_ok=True)
        argv = [
            SSH_BIN, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={ssh_dir}/cm-%r@%h:%p",
            "-o", "ControlPersist=10m", target, command,
        ]
        try:
            return subprocess.run(argv, input=data if data is not None else b"",
                                  capture_output=True, timeout=SSH_TIMEOUT + 25)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def remote_put(self, host: str, rel: str, data: bytes) -> bool:
        """Place one message on a host atomically (tmp + mv). rel = inboxes/<agent>/[@slug/]<id>.json."""
        bh = self.host_bus_home(host)
        target = f"{bh}/{rel}"
        d = os.path.dirname(target)
        dirs = [shlex.quote(d)]
        if os.path.basename(d).startswith("@"):
            dirs += [shlex.quote(f"{d}/.read"), shlex.quote(f"{d}/.done")]
        q = shlex.quote(target)
        res = self.ssh(host, f"mkdir -p {' '.join(dirs)} && cat > {q}.tmp && mv {q}.tmp {q}", data)
        return res is not None and res.returncode == 0

    def queue_for_host(self, host: str, rel: str, data: bytes) -> Path:
        dest = self.home / "outbox" / host / rel.removeprefix("inboxes/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest

    def deliver_remote(self, host: str, agent: str, slug: str | None, record: dict) -> str:
        """'delivered' or 'queued'. Never raises: an unreachable host gets a queue."""
        rel = f"inboxes/{agent}" + (f"/@{slug}" if slug else "") + f"/{record['id']}.json"
        data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if (self.hosts().get(host) or {}).get("ssh") and self.remote_put(host, rel, data):
            return "delivered"
        self.queue_for_host(host, rel, data)
        return "queued"

    def _host_state(self, host: str) -> Path:
        d = self.home / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _age(path: Path) -> float:
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return float("inf")

    def collect(self, force: bool = False, only: str | None = None) -> list[dict]:
        """Exchange with every SSH-reachable host: push our queue, pull its outbox
        for us. Throttled per host to COLLECT_INTERVAL s unless forced. Copy,
        verify, then delete: nothing is removed remotely before it is in an inbox."""
        out = []
        for host in self.remote_hosts():
            if only and host != only:
                continue
            res = self._collect_host(host, force)
            if res is not None:
                out.append(res)
        return out

    def _collect_host(self, host: str, force: bool) -> dict | None:
        state = self._host_state(host)
        stamp = state / "last-collect"
        if not force and self._age(stamp) < COLLECT_INTERVAL:
            return None
        # Same lock as the bash collector (hooks, launchd): one at a time per host.
        lock = self.home / "locks" / f"collect-{host}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError:
            if self._age(lock) <= 120:
                return None
            try:
                lock.rmdir()
                lock.mkdir()
            except OSError:
                return None
        try:
            stamp.touch()
            pushed = pulled = 0
            reachable = True
            qdir = self.home / "outbox" / host
            for f in sorted(qdir.rglob("msg-*.json")) if qdir.is_dir() else []:
                rel = "inboxes/" + str(f.relative_to(qdir))
                if self.remote_put(host, rel, f.read_bytes()):
                    f.unlink()
                    pushed += 1
                else:
                    reachable = False
                    break
            me = self.host_self()
            if reachable and me:
                got = self._pull(host, me)
                if got is None:
                    reachable = False
                else:
                    pulled = got
            queued = len(list(qdir.rglob("msg-*.json"))) if qdir.is_dir() else 0
            res = {"host": host, "at": iso_now(), "status": "ok" if reachable else "unreachable",
                   "pushed": pushed, "pulled": pulled, "queued": queued}
            self._atomic_write(state / "last-collect.json", res)
            return res
        finally:
            try:
                lock.rmdir()
            except OSError:
                pass

    def _pull(self, host: str, me: str) -> int | None:
        """Pull host:outbox/<me>/ into our inboxes. None when the host is unreachable."""
        q = shlex.quote(f"{self.host_bus_home(host)}/outbox/{me}")
        res = self.ssh(host, f"cd {q} 2>/dev/null || exit 0; find . -name 'msg-*.json' -type f | tar -cf - -T -")
        if res is None or res.returncode != 0:
            return None
        pulled = 0
        done: list[str] = []
        if res.stdout:
            try:
                tar = tarfile.open(fileobj=io.BytesIO(res.stdout))
                members = [m for m in tar.getmembers() if m.isfile()]
            except tarfile.TarError:
                return 0
            for m in sorted(members, key=lambda m: m.name):
                rel = m.name.removeprefix("./")
                parts = rel.split("/")
                # <agent>/msg.json or <agent>/@slug/msg.json, nothing else.
                if ".." in parts or not (2 <= len(parts) <= 3) or not parts[-1].startswith("msg-"):
                    continue
                raw = tar.extractfile(m).read()
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # unreadable: leave it on the host
                if not isinstance(record, dict):
                    continue
                if not self.agent_exists(parts[0]):
                    continue
                dest = self.home / "inboxes" / rel
                if any(p.exists() for p in (dest, dest.parent / ".read" / dest.name, dest.parent / ".done" / dest.name)):
                    done.append("./" + rel)  # an earlier pull whose remote delete failed
                    continue
                if dest.parent.name.startswith("@"):
                    (dest.parent / ".read").mkdir(parents=True, exist_ok=True)
                    (dest.parent / ".done").mkdir(parents=True, exist_ok=True)
                self._atomic_write(dest, record)
                if _read_json(dest) is None:
                    continue
                done.append("./" + rel)
                pulled += 1
                self._append_thread(record)
        if done:
            self.ssh(host, f"cd {q} && xargs rm -f", ("\n".join(done) + "\n").encode())
        return pulled

    def remote_status(self, host: str, force: bool = False) -> dict:
        """A host's registry and pending inbox, cached REMOTE_STATUS_TTL s in
        hosts/<host>/status.json (shared with the bash lib). A failed fetch keeps
        the last sessions with ok=false."""
        cache = self._host_state(host) / "status.json"
        prev = _read_json(cache)
        if not force and prev is not None and self._age(cache) < REMOTE_STATUS_TTL:
            return prev
        q = shlex.quote(self.host_bus_home(host))
        cmd = (
            f"cd {q} 2>/dev/null || exit 0\n"
            "find registry -path 'registry/*.d/*.json' -type f 2>/dev/null | while read -r f; do "
            "printf 'R\\t'; tr '\\n' ' ' < \"$f\"; echo; done\n"
            "find inboxes -name 'msg-*.json' -type f -not -path '*/.read/*' -not -path '*/.done/*' "
            "2>/dev/null | awk '{print \"I\\t\" $0}'"
        )
        res = self.ssh(host, cmd)
        if res is not None and res.returncode == 0:
            sessions, pending = [], []
            for line in res.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("R\t"):
                    try:
                        d = json.loads(line[2:])
                        if isinstance(d, dict):
                            sessions.append(d)
                    except json.JSONDecodeError:
                        pass
                elif line.startswith("I\t"):
                    pending.append(line[2:])
            data = {"host": host, "fetched_at": iso_now(), "ok": True,
                    "sessions": sessions, "pending": pending}
        else:
            data = dict(prev or {"host": host, "fetched_at": None, "sessions": [], "pending": []})
            data.update({"ok": False, "error": "unreachable", "checked_at": iso_now()})
        self._atomic_write(cache, data)
        return data

    def remote_sessions(self, host: str) -> list[dict]:
        """Live sessions on a host, with pending = own queue + agent broadcasts."""
        snap = self.remote_status(host)
        now = datetime.now(timezone.utc)
        out = []
        for s in snap.get("sessions", []):
            name, slug = s.get("name"), s.get("slug")
            if not name or not slug:
                continue
            try:
                ts = datetime.fromisoformat((s.get("last_seen") or "").replace("Z", "+00:00"))
                if (now - ts) > timedelta(minutes=SESSION_TTL_MIN):
                    continue
            except ValueError:
                pass
            root, own = f"inboxes/{name}/", f"inboxes/{name}/@{slug}/"
            pend = sum(1 for p in snap.get("pending", [])
                       if p.startswith(own) or (p.startswith(root) and "/" not in p[len(root):]))
            out.append({**s, "host": host, "pending": pend})
        out.sort(key=lambda d: (d["name"], d.get("last_seen") or ""), reverse=False)
        return out

    def routing_denied(self, sender: str, recipient: str) -> str | None:
        rules = self.agent_map().get("routing_rules", {}).get(sender, {})
        if recipient in (rules.get("deny") or []):
            return rules.get("reason", "isolation rule")
        return None

    # ---------- sessions ----------

    def registry_dir(self, agent: str) -> Path:
        return self.home / "registry" / f"{agent}.d"

    def list_sessions(self, agent: str, include_stale: bool = False) -> list[dict]:
        """Live sessions for an agent, most recently seen first."""
        out: list[dict] = []
        now = datetime.now(timezone.utc)
        for entry in sorted(self.registry_dir(agent).glob("*.json")):
            data = _read_json(entry)
            if not data or not data.get("slug"):
                continue
            seen = data.get("last_seen") or ""
            stale = False
            try:
                ts = datetime.fromisoformat(seen.replace("Z", "+00:00"))
                stale = (now - ts) > timedelta(minutes=SESSION_TTL_MIN)
            except ValueError:
                pass
            if stale and not include_stale:
                continue
            data["stale"] = stale
            data["pending"] = self.count_inbox(agent, data["slug"])
            out.append(data)
        out.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
        return out

    def register_session(self, agent: str, workspace: str, slug: str | None = None) -> dict:
        slug = slug or slug_for_path(workspace)
        d = self.registry_dir(agent)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{slug}.json"
        prev = _read_json(f) or {}
        now = iso_now()
        rec = {
            "name": agent,
            "slug": slug,
            "active": True,
            "last_seen": now,
            "workspace_path": str(workspace),
            "session_started": prev.get("session_started") or now,
            "pid": os.getpid(),
        }
        if host := self.host_self():
            rec["host"] = host
        self._atomic_write(f, rec)
        return rec

    # ---------- inbox ----------

    def inbox_root(self, agent: str) -> Path:
        return self.home / "inboxes" / agent

    def session_dir(self, agent: str, slug: str) -> Path:
        return self.inbox_root(agent) / f"@{slug}"

    def list_inbox(self, agent: str, slug: str | None = None, all_sessions: bool = False) -> list[Path]:
        """Pending messages for a session: its own dir plus shared broadcasts.

        With all_sessions (or no slug) every session dir is listed, so nothing
        stays invisible to a caller that does not know its own slug.
        """
        root = self.inbox_root(agent)
        if not root.is_dir():
            return []
        files = list(root.glob("msg-*.json"))
        if slug and not all_sessions:
            files += list(self.session_dir(agent, slug).glob("msg-*.json"))
        else:
            for sub in root.iterdir():
                if sub.is_dir() and sub.name.startswith("@"):
                    files += list(sub.glob("msg-*.json"))
        return sorted(files)

    def count_inbox(self, agent: str, slug: str | None = None, all_sessions: bool = False) -> int:
        return len(self.list_inbox(agent, slug, all_sessions))

    def resolve_message(self, agent: str, partial_id: str) -> Path | None:
        """Find a message by (possibly partial) id: pending first, then archives."""
        root = self.inbox_root(agent)
        if not root.is_dir():
            return None
        pending = [p for p in self.list_inbox(agent, all_sessions=True) if p.name.startswith(partial_id)]
        if pending:
            return sorted(pending)[0]
        archived = [
            p
            for p in root.rglob(f"{partial_id}*.json")
            if any(part in (".read", ".done") for part in p.parts)
        ]
        return sorted(archived)[0] if archived else None

    def read_message(self, agent: str, partial_id: str) -> dict:
        path = self.resolve_message(agent, partial_id)
        if path is None:
            raise BusError(f"no message matching '{partial_id}' in {agent}'s inbox")
        data = _read_json(path)
        if data is None:
            raise BusError(f"{path.name} is not readable JSON")
        holder = path.parent.name
        data["_location"] = {
            "path": str(path),
            "scope": holder[1:] if holder.startswith("@") else "all-sessions",
            "state": "archived" if path.parent.name in (".read", ".done") else "pending",
        }
        return data

    def archive_message(self, agent: str, partial_id: str, state: str = "done") -> Path:
        """Move a message into .read/ or .done/ NEXT TO where it lives."""
        if state not in ("read", "done"):
            raise BusError("state must be 'read' or 'done'")
        path = self.resolve_message(agent, partial_id)
        if path is None:
            raise BusError(f"no message matching '{partial_id}' in {agent}'s inbox")
        if path.parent.name in (".read", ".done"):
            return path  # already archived; archiving twice is not an error
        dest_dir = path.parent / f".{state}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        path.rename(dest)
        return dest

    # ---------- send ----------

    def send(
        self,
        sender: str,
        to: str,
        msg_type: str,
        intent: str,
        payload: dict,
        priority: str = "normal",
        thread: str | None = None,
        in_reply_to: str | None = None,
        context_refs: list[str] | None = None,
        requires_response: bool = False,
        response_by: str | None = None,
        sender_session: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        agent, slug, want_host = parse_full_addr(to)
        if task_id is None and in_reply_to:
            # A reply to a delegated task carries its id even if the sender forgot it.
            task_id = self.task_of(sender, in_reply_to)
        if task_id is not None and not TASK_ID_RE.match(task_id):
            raise BusError(f"invalid task_id {task_id!r}: 3-64 chars of [A-Za-z0-9._-]")
        if not self.agent_exists(agent):
            known = ", ".join(sorted(self.agents())) or "none configured"
            raise BusError(f"unknown agent '{agent}'. Known: {known}")
        if (reason := self.routing_denied(sender, agent)) is not None:
            raise BusError(f"routing denied {sender} -> {agent} ({reason})")
        to_host = self.resolve_host(agent, want_host)

        warning = None
        # Liveness is only known for this machine's registry.
        if slug and not to_host:
            live = [s["slug"] for s in self.list_sessions(agent)]
            if live and slug not in live:
                # Delivering into a directory nobody reads is the failure mode this
                # whole feature exists to fix, so say it rather than swallow it.
                warning = (
                    f"no live session '{agent}/{slug}' (live: {', '.join(live) or 'none'}). "
                    "Delivered anyway; it will be read when that session starts."
                )

        mid = msg_id()
        record = {
            "id": mid,
            "version": "1.0",
            "from": sender,
            "to": agent,  # bare name: readers and threads stay unchanged
            "thread_id": thread or thread_id(),
            "in_reply_to": in_reply_to,
            "type": msg_type,
            "intent": intent,
            "priority": priority,
            "payload": payload,
            "context_refs": context_refs or [],
            "requires_response": requires_response,
            "ts": iso_now(),
        }
        if response_by:
            record["response_by"] = response_by
        if sender_session:
            # Chi scrive, sessione compresa: due sessioni dello stesso agente
            # (es. leo e la segretaria leo/nora) restano distinguibili. Campo in aggiunta.
            record["from_session"] = f"{sender}/{sender_session}"
            if chain := self.wake_chain(sender, sender_session):
                record["wake_chain"] = chain
        if task_id:
            record["task_id"] = task_id
        if me := self.host_self():
            # v2.5: the sender's machine, so a reply finds its way back.
            record["context"] = {"host": me}

        if to_host:
            outcome = self.deliver_remote(to_host, agent, slug, record)
            self._append_thread(record)
            self._append_outbox(sender, record, slug)
            addr = f"{agent}@{to_host}" + (f"/{slug}" if slug else "")
            if outcome == "queued":
                if (self.hosts().get(to_host) or {}).get("ssh"):
                    warning = (f"{to_host} unreachable now: queued in outbox/{to_host}/, "
                               "delivered at the next collection.")
                else:
                    warning = (f"queued in outbox/{to_host}/: {to_host} collects it "
                               "at its next round (it cannot be reached from here).")
            return {"id": mid, "delivered_to": addr, "host": to_host,
                    "queued": outcome == "queued", "path": None, "warning": warning,
                    "task_id": task_id}

        if slug:
            target_dir = self.session_dir(agent, slug)
            # Create the archives with the dir: the recipient files the message
            # right after reading, and a fresh session dir would have nowhere.
            (target_dir / ".read").mkdir(parents=True, exist_ok=True)
            (target_dir / ".done").mkdir(parents=True, exist_ok=True)
        else:
            target_dir = self.inbox_root(agent)
            target_dir.mkdir(parents=True, exist_ok=True)

        self._atomic_write(target_dir / f"{mid}.json", record)
        self._append_thread(record)
        self._append_outbox(sender, record, slug)
        return {"id": mid, "delivered_to": f"{agent}/{slug}" if slug else agent,
                "host": self.host_self(), "queued": False,
                "path": str(target_dir / f"{mid}.json"), "warning": warning,
                "task_id": task_id}

    # ---------- expected results (v2.6) ----------
    #
    # Same files as agent-team-os/scripts/agent-team-os-wake.py, which owns the
    # wake itself: this side only registers, lists and stamps.

    def waits_root(self) -> Path:
        return self.home / "waits"

    def wait_dir(self, agent: str, slug: str) -> Path:
        return self.waits_root() / agent / f"@{slug}"

    def find_wait(self, task_id: str) -> Path | None:
        root = self.waits_root()
        if not root.is_dir():
            return None
        return next(iter(root.glob(f"*/@*/{task_id}.json")), None)

    def expect(self, agent: str, slug: str, description: str, delegated_to: str | None = None,
               deadline: str | None = None, task_id: str | None = None,
               target: dict | None = None) -> dict:
        """Register an expected result on a session. One waiter per task_id, bus-wide:
        that is what keeps A→B→A from waking in a loop."""
        task_id = task_id or new_task_id()
        if not TASK_ID_RE.match(task_id):
            raise BusError(f"invalid task_id {task_id!r}: 3-64 chars of [A-Za-z0-9._-]")
        if deadline:
            try:
                datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            except ValueError:
                raise BusError(f"deadline is not ISO 8601: {deadline!r}") from None
        if (existing := self.find_wait(task_id)) is not None:
            raise BusError(f"task {task_id} already has a waiter "
                           f"({existing.parent.parent.name}/{existing.parent.name[1:]}): one task, one waiter")
        rec = {
            "v": 1,
            "task_id": task_id,
            "agent": agent,
            "slug": slug,
            "host": self.host_self(),
            "description": description.strip()[:200],
            "delegated_to": delegated_to,
            "created_at": iso_now(),
            "deadline": deadline,
            "state": "expected",
            "target": {k: v for k, v in (target or {}).items() if v},
        }
        self._atomic_write(self.wait_dir(agent, slug) / f"{task_id}.json", rec)
        return rec

    def list_waits(self, agent: str, slug: str | None = None, include_closed: bool = False) -> list[dict]:
        root = self.waits_root()
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.glob(f"{agent}/@{slug or '*'}/*.json")):
            if p.name.startswith("."):
                continue  # .woken.json and friends belong to the wake script
            rec = _read_json(p)
            if not rec or not rec.get("task_id"):
                continue
            if not include_closed and rec.get("state") not in ("expected", "expired"):
                continue
            out.append(rec)
        return out

    def wake_chain(self, agent: str, slug: str) -> dict | None:
        """The wake that touched this session in the last WAKE_CHAIN_MIN minutes, if any.
        A message sent meanwhile carries it, and the wake script will not let it wake
        anyone else: depth 1 per chain."""
        rec = _read_json(self.wait_dir(agent, slug) / ".woken.json")
        if not rec or not rec.get("at"):
            return None
        try:
            at = datetime.fromisoformat(rec["at"].replace("Z", "+00:00"))
        except ValueError:
            return None
        if datetime.now(timezone.utc) - at > timedelta(minutes=WAKE_CHAIN_MIN):
            return None
        return {"task_id": rec.get("task_id"), "at": rec["at"], "session": f"{agent}/{slug}"}

    def task_of(self, agent: str, message_id: str) -> str | None:
        """task_id carried by a message in this agent's inbox (pending or archived)."""
        path = self.resolve_message(agent, message_id)
        data = _read_json(path) if path else None
        tid = (data or {}).get("task_id") or ((data or {}).get("payload") or {}).get("task_id")
        return tid if isinstance(tid, str) else None

    def reply_address(self, record: dict) -> str:
        """Address that reaches a message's sender: its session when known
        (from_session) and its machine when it is not this one."""
        sender = record.get("from") or ""
        fs = record.get("from_session") or ""
        host = (record.get("context") or {}).get("host")
        slug = fs.partition("/")[2] if "/" in fs else ""
        if host and host != self.host_self():
            return f"{sender}@{host}" + (f"/{slug}" if slug else "")
        return f"{sender}/{slug}" if slug else sender

    # ---------- threads & log ----------

    def thread(self, tid: str) -> list[dict]:
        f = self.home / "threads" / f"{tid}.jsonl"
        if not f.is_file():
            return []
        out = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _append_thread(self, record: dict) -> None:
        d = self.home / "threads"
        d.mkdir(parents=True, exist_ok=True)
        entry = {k: record[k] for k in ("id", "from", "to", "type", "intent", "ts") if k in record}
        entry["summary"] = (record.get("payload") or {}).get("summary", "")
        with (d / f"{record['thread_id']}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _append_outbox(self, sender: str, record: dict, slug: str | None) -> None:
        d = self.home / "outbox"
        d.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": record["id"], "to": record["to"], "slug": slug,
            "type": record["type"], "intent": record["intent"], "ts": record["ts"],
        }
        with (d / f"{sender}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """Write via temp + rename so a reader never sees a half-written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        tmp.replace(path)
