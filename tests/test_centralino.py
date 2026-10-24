"""BR24-F3.3: con AB_CENTRALINO=1 le tool sono un client sottile del centralino."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from agent_bus_mcp import server
from agent_bus_mcp.bus import BusError


class FintoCentralino:
    """Socket che registra le richieste e risponde con quello che gli si dice."""

    def __init__(self, home: Path):
        self.path = home / "centralino.sock"
        self.richieste: list[dict] = []
        self.risposta = lambda req: {"v": 1, "id": req["id"], "ok": True, "result": {"op": req["op"]}}
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(16)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf)
                self.richieste.append(req)
                conn.sendall((json.dumps(self.risposta(req)) + "\n").encode())

    def chiudi(self):
        self.sock.close()


@pytest.fixture
def finto(monkeypatch, tmp_path):
    home = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp"))  # path corto per il socket
    ws = tmp_path / "Projects" / "orbit"
    ws.mkdir(parents=True)
    monkeypatch.setenv("AB_CENTRALINO", "1")
    monkeypatch.setenv("AB_HOME", str(home))
    monkeypatch.setenv("AB_CWD", str(ws))
    monkeypatch.setenv("ONDA_TERMINAL_ID", "pane-1")
    monkeypatch.delenv("AB_AGENT", raising=False)
    monkeypatch.delenv("AB_SESSION_SLUG", raising=False)
    f = FintoCentralino(home)
    yield f
    f.chiudi()


def test_le_tool_chiamano_il_centralino(finto):
    assert server.bus_send(to="alita/work-hub", intent="x", summary="ciao", message_type="request",
                           kind="delega") == {"op": "send"}
    req = finto.richieste[-1]
    assert req["v"] == 1 and req["op"] == "send"
    assert req["args"] == {"to": "alita/work-hub", "intent": "x", "summary": "ciao", "message_type": "request",
                           "kind": "delega", "priority": "normal", "requires_response": False,
                           "expect_result": False}
    assert req["caller"]["cwd"].endswith("/Projects/orbit")
    assert req["caller"]["onda_terminal_id"] == "pane-1"
    assert req["caller"]["agent"] is None  # l'identità la risolve il centralino


@pytest.mark.parametrize("call,op", [
    (lambda: server.bus_status(), "status"),
    (lambda: server.bus_inbox(), "inbox"),
    (lambda: server.bus_read(message_id="msg-1"), "read"),
    (lambda: server.bus_expect(description="d"), "expect"),
    (lambda: server.bus_waits(), "waits"),
    (lambda: server.bus_gestisci(message_id="msg-1", esito="chiuso"), "handle"),
    (lambda: server.bus_archive(message_id="msg-1"), "archive"),
    (lambda: server.bus_thread(thread_id="thread-1"), "thread"),
])
def test_mappa_delle_tool(finto, call, op):
    assert call() == {"op": op}
    assert finto.richieste[-1]["op"] == op


def test_rifiuto_del_centralino(finto):
    finto.risposta = lambda req: {"v": 1, "id": req["id"], "ok": False,
                                  "error": {"code": "kind_required", "message": "say what this brief is"}}
    with pytest.raises(BusError, match="say what this brief is"):
        server.bus_send(to="kai", intent="x", summary="y", message_type="brief")


def test_centralino_giu_errore_chiaro(finto):
    finto.chiudi()
    finto.path.unlink()
    with pytest.raises(BusError, match="il centralino del bus non risponde"):
        server.bus_inbox()


def test_senza_interruttore_codice_di_oggi(finto, monkeypatch):
    monkeypatch.delenv("AB_CENTRALINO")
    with pytest.raises(BusError):  # cwd non mappato nel bus vero di prova: il percorso a file
        server.bus_inbox()
    assert finto.richieste == []


def test_chiusura_per_task_id(finto):
    server.bus_gestisci(esito="chiuso", task_id="t-20261024-abcdef")
    req = finto.richieste[-1]
    assert req["op"] == "handle" and req["args"] == {"esito": "chiuso", "task_id": "t-20261024-abcdef"}
