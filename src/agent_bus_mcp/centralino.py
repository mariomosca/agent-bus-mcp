"""Client sottile del centralino del bus.

Ogni tool è una chiamata al centralino sul socket locale (``$AB_HOME/centralino.sock``): il MCP
non risolve indirizzi, non legge e non scrive file del bus, e restituisce la risposta del
centralino così com'è. Se il centralino non risponde la tool fallisce con un errore chiaro.

Il processo MCP vive quanto la sessione Claude, quindi un battito ogni 60 s dice al
centralino che la sessione è viva anche durante un turno lungo e fuori da Onda.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

HEARTBEAT_S = 60
TIMEOUT_S = 60


def socket_path() -> Path:
    return Path(os.environ.get("AB_HOME", str(Path.home() / ".agent-team-os"))) / "centralino.sock"


class ErroreCentralino(RuntimeError):
    """Rifiuto del centralino (``code`` dal contratto) o centralino irraggiungibile (``code='giu'``)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def caller(cwd: str) -> dict:
    """Chi chiama: il centralino risolve agente e slug da qui (regola unica)."""
    return {
        "cwd": cwd,
        "agent": os.environ.get("AB_AGENT") or None,
        "slug": os.environ.get("AB_SESSION_SLUG") or None,
        # niente pid: il padre di questo processo è uv, non la sessione (il pid lo danno gli hook)
        "claude_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "onda_terminal_id": os.environ.get("ONDA_TERMINAL_ID"),
        "tmux_pane": os.environ.get("TMUX_PANE"),
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    }


def _richiesta(req: dict, path: Path) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT_S)
    try:
        for tentativo in range(5):
            try:
                s.connect(str(path))
                break
            except (ConnectionRefusedError, BlockingIOError):
                if tentativo == 4 or not path.exists():
                    raise
                time.sleep(0.05 * (tentativo + 1))
                s.close()
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(TIMEOUT_S)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise OSError("connessione chiusa senza risposta")
    return json.loads(buf)


def chiama(op: str, args: dict, cwd: str) -> Any:
    path = socket_path()
    req = {"v": 1, "id": f"r-{os.getpid()}-{time.monotonic_ns()}", "op": op,
           "caller": caller(cwd), "args": {k: v for k, v in args.items() if v is not None}}
    try:
        resp = _richiesta(req, path)
    except (OSError, ValueError) as exc:
        raise ErroreCentralino(
            f"il centralino del bus non risponde su {path} ({exc}). Il bus non e' utilizzabile da "
            "questa sessione finche' il centralino non riparte: avvisa Mario o Alita.", "giu") from None
    if not resp.get("ok"):
        err = resp.get("error") or {}
        raise ErroreCentralino(err.get("message") or "errore del centralino", err.get("code") or "internal")
    return resp.get("result")


def avvia_heartbeat(cwd_fn: Callable[[], str]) -> threading.Thread:
    """Un battito subito e poi ogni 60 s, finché vive il processo. Gli errori si ignorano:
    il battito dopo riprova, e la vivacità la decide il centralino."""
    def ciclo():
        while True:
            try:
                chiama("session.heartbeat", {}, cwd_fn())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(HEARTBEAT_S)
    t = threading.Thread(target=ciclo, name="centralino-heartbeat", daemon=True)
    t.start()
    return t
