# agent-bus-mcp — STATUS

**Stato**: 🟢 funzionante, 19 test verdi, e2e verificato via protocollo MCP (21 set 2026).
**Owner**: Alita. **Nato da**: bug sessioni parallele Kai, 21 set 2026.

## Fatto
- Rimozione del bus a file (BR24-F5.5, ramo `feat/br24-f55-rimozione`): via `bus.py` e ogni ramo a
  file di `server.py`, `centralino.py` senza `attivo()` (sempre acceso). Sei tool v3 e cinque alias
  fino al 31/10/2026, battito di 60 s. 21 test. `src/` da 1.767 a 391 righe.
- v2.8 deleghe dichiarate (21 ott 2026, piano BS21, ramo `feat/bs21-deleghe`): `kind` obbligatorio
  (delega | comunicazione) su request/brief/handoff, la delega registra l'attesa da sola,
  `bus_gestisci` (chiuso, riarma, risposto, letto), `bus_inbox`/`bus_read` mostrano kind e aggancio,
  `bus_waits.unhandled_returns`. 9 tool, 80 test. Aggancio, sveglia a ogni ritorno, tetti e misura
  stanno in agent-team-os (`agent-team-os-wake.py`).
- v2.7 domande nella delega (18 ott 2026, DR18-F1.7): una `question` con il `task_id` della
  delega sveglia il delegante (una volta per domanda, la sveglia la fa agent-team-os-wake.py)
  senza consumare l'attesa dell'esito; chi chiede porta `waiting_agent` nella voce di registro
  finché non legge la risposta o manda l'esito (Orbit OR18-F4.3); `bus_waits.waiting_on_you`;
  i thread registrano `in_reply_to` e `task_id`. 61 test.
- v2.6 esiti attesi (17 ott 2026, ramo `feat/hook-di-ritorno`, piano F4): `task_id` ed
  `expect_result` su `bus_send`, eredità da `in_reply_to`, tool `bus_expect` e `bus_waits`,
  timbro `wake_chain`, giro immediato della sveglia dopo un esito. 8 tool, 54 test. Prova reale
  sul mini via tmux: sveglia, testo in casella, sessione occupata, catena, scadenza.
- v2.5 due macchine (14 ott 2026, ramo `feat/bus-mini`): indirizzi `agent@host[/slug]`,
  consegna via SSH laptop→mini con coda, outbox del mini raccolta dal laptop, `bus_status`
  con le sessioni del mini, `reply_to`. 23 test nuovi con `ssh` finto.
- `from_session` nel messaggio (era lavoro non committato su `main`, portato sul ramo).
- Layer filesystem (`bus.py`) con semantica allineata a bus v1.4 (`agent/slug`).
- 6 tool MCP: status, inbox, read, send, archive, thread.
- 19 test su AB_HOME isolato, incluso il caso che riproduce il bug.

## Da fare
- Registrare l'MCP in `~/.claude.json` e provarlo da una sessione vera.
- Valutare se `/inbox` e `/send` debbano diventare wrapper dei tool MCP (oggi
  duplicano la logica in bash). Non urgente: i due layer convivono.
- `delegate_inline` (previsto dal piano v2.0) non implementato.
