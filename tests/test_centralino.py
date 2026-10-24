"""BR24-F5.5: le tool sono un client sottile del centralino, non c'è altro percorso."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from agent_bus_mcp import server
from agent_bus_mcp import centralino
from agent_bus_mcp.server import _SERVER_DIR, BusError, _caller_cwd


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


def test_chiusura_per_task_id(finto):
    server.bus_gestisci(esito="chiuso", task_id="t-20261024-abcdef")
    req = finto.richieste[-1]
    assert req["op"] == "handle" and req["args"] == {"esito": "chiuso", "task_id": "t-20261024-abcdef"}


def test_v3_reply_close_read_thread(finto):
    server.bus_reply(message_id="msg-1", summary="fatto")
    assert finto.richieste[-1]["op"] == "reply"
    assert finto.richieste[-1]["args"] == {"message_id": "msg-1", "summary": "fatto", "message_type": "response"}
    server.bus_close(task_id="t-20261024-abcdef")
    assert finto.richieste[-1]["args"] == {"task_id": "t-20261024-abcdef", "esito": "chiuso"}
    server.bus_read(thread_id="thread-1")
    assert finto.richieste[-1]["op"] == "thread"


def test_tutte_le_tool_e_gli_alias_sono_registrati():
    import asyncio
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "bus_send", "bus_reply", "bus_close", "bus_inbox", "bus_read", "bus_status",  # v3
        "bus_gestisci", "bus_archive", "bus_waits", "bus_expect", "bus_thread"}      # alias fino al 31/10/2026


def test_status_senza_cwd_risponde_comunque(finto, monkeypatch):
    """Lanciato nella sua stessa cartella il server non sa chi chiama: il centralino risponde con me.error."""
    monkeypatch.delenv("AB_CWD")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(_SERVER_DIR)
    assert server.bus_status(agent="kai") == {"op": "status"}
    assert finto.richieste[-1]["caller"]["cwd"] == "" and finto.richieste[-1]["args"] == {"agent": "kai"}


def test_gli_argomenti_none_non_partono(finto):
    server.bus_inbox()
    assert finto.richieste[-1]["args"] == {"all_sessions": False, "unread_only": False}
    server.bus_close(message_id="msg-1")
    assert finto.richieste[-1]["args"] == {"message_id": "msg-1", "esito": "chiuso"}


def test_heartbeat_un_battito_subito(finto, monkeypatch):
    import time
    monkeypatch.setattr(centralino, "HEARTBEAT_S", 3600)
    centralino.avvia_heartbeat(lambda: "/tmp")
    for _ in range(50):
        if finto.richieste:
            break
        time.sleep(0.05)
    assert finto.richieste[0]["op"] == "session.heartbeat" and finto.richieste[0]["caller"]["cwd"] == "/tmp"


def test_errore_interno_del_centralino(finto):
    finto.risposta = lambda req: {"v": 1, "id": req["id"], "ok": False, "error": {"code": "not_found", "message": "no message"}}
    with pytest.raises(BusError, match="no message"):
        server.bus_read(message_id="msg-9")
    with pytest.raises(centralino.ErroreCentralino) as e:
        centralino.chiama("read", {}, "/tmp")
    assert e.value.code == "not_found"


# ---------- chi chiama: la cartella della sessione, non quella del server ----------

def test_segnaposto_non_espanso_si_ignora(monkeypatch, tmp_path):
    monkeypatch.setenv("AB_CWD", "${CLAUDE_PROJECT_DIR:-${PWD}}")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _caller_cwd() == str(tmp_path)


def test_percorso_vero_nell_ambiente_vince_sulla_cwd(monkeypatch, tmp_path):
    real = tmp_path / "workspace"
    real.mkdir()
    monkeypatch.setenv("AB_CWD", str(real))
    assert _caller_cwd() == str(real)


def test_la_cartella_del_server_e_rifiutata(monkeypatch):
    monkeypatch.delenv("AB_CWD", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(_SERVER_DIR)
    with pytest.raises(BusError, match="own directory"):
        _caller_cwd()
