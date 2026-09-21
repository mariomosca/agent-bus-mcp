"""Filesystem layer of the Agent Team OS bus.

Mirrors the semantics of scripts/agent-team-os-lib.sh (v1.4, session identity).
Kept free of MCP concerns so it can be tested on its own.

Layout::

    ~/.agent-team-os/
      AGENT_MAP.json
      registry/<agent>.d/<slug>.json     one entry per live session
      inboxes/<agent>/msg-*.json         broadcast: every session sees these
      inboxes/<agent>/@<slug>/msg-*.json one session only
                              .read/ .done/
      threads/<thread-id>.jsonl
      outbox/<agent>.jsonl
"""

from __future__ import annotations

import json
import os
import random
import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A session with no heartbeat for this long is treated as gone. Matches the
# bash lib's AB_SESSION_TTL_MIN default.
SESSION_TTL_MIN = int(os.environ.get("AB_SESSION_TTL_MIN", "240"))

SLUG_SAFE = re.compile(r"[^a-z0-9._-]")


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


def slug_for_path(path: str | os.PathLike[str]) -> str:
    """Workspace slug: basename, lowercased, non-alphanumerics folded to '-'."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    base = p.name or "root"
    return SLUG_SAFE.sub("-", base.lower())


def parse_addr(addr: str) -> tuple[str, str | None]:
    """'kai/noi-calendar' -> ('kai', 'noi-calendar'); 'kai' -> ('kai', None)."""
    addr = addr.strip()
    if "/" in addr:
        agent, _, slug = addr.partition("/")
        return agent.strip(), (slug.strip() or None)
    return addr, None


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
            if raw.startswith(pattern) or resolved.startswith(pattern):
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), rule.get("agent", ""))
        return best[1] if best and best[1] else None

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
    ) -> dict:
        agent, slug = parse_addr(to)
        if not self.agent_exists(agent):
            known = ", ".join(sorted(self.agents())) or "none configured"
            raise BusError(f"unknown agent '{agent}'. Known: {known}")
        if (reason := self.routing_denied(sender, agent)) is not None:
            raise BusError(f"routing denied {sender} -> {agent} ({reason})")

        warning = None
        if slug:
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
                "path": str(target_dir / f"{mid}.json"), "warning": warning}

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
