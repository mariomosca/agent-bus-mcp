# agent-bus-mcp

MCP server per il bus Agent Team OS. Sostituisce heredoc bash e scrittura diretta di JSON
nelle inbox con sei tool tipizzati.

Nato il 21 set 2026 dal bug delle sessioni parallele: due Kai aperti insieme
(`microsoft-mcp` e `noi-calendar`) si rubavano i messaggi perché il bus li vedeva come un
unico destinatario. Il fix (bus v1.4, `agent/slug`) vive nella lib bash; questo server lo
espone come API di prima classe.

## Tool

| Tool | A cosa serve |
|---|---|
| `bus_status` | chi è sul bus e **quali sessioni sono vive adesso**, con i pendenti di ciascuna |
| `bus_inbox` | i messaggi di *questa* sessione (i suoi + i broadcast); `all_sessions` per tutti |
| `bus_read` | un messaggio per intero, con `_location` che dice dove sta e se è archiviato |
| `bus_send` | invia a `kai` (tutte le sessioni) o `kai/noi-calendar` (una sola) |
| `bus_archive` | sposta in `.done/` o `.read/` **accanto** al messaggio, non nella radice |
| `bus_thread` | rigioca una conversazione in ordine |

## Indirizzamento

Un agente può avere più sessioni, una per workspace:

```
kai                  → tutte le sessioni Kai (coda condivisa)
kai/noi-calendar     → solo quella
kai/microsoft-mcp    → solo l'altra
```

`bus_status` prima di inviare, quando non si sa quante sessioni esistono. Uno slug non vivo
**viene consegnato lo stesso** con un warning: il messaggio aspetta che quella sessione
parta, invece di sparire.

## Identità del chiamante

Dedotta dalla working directory via le `rules` di `AGENT_MAP.json`, quindi una sessione non
deve dichiarare chi è. Override con `AB_AGENT` e `AB_SESSION_SLUG` dove le regole non
arrivano. `AB_HOME` punta a un bus diverso (i test lo usano).

Nota: `detect_agent` qui prende il **prefisso più lungo** che matcha, mentre la lib bash
prende la prima regola in ordine di file. Identico quando le regole non si annidano, più
sicuro quando lo fanno.

## Installazione

```bash
claude mcp add agent-bus -- uv run --directory ~/Projects/07-Tooling/mcp/agent-bus-mcp \
  --with fastmcp --with pydantic python -m agent_bus_mcp.server
```

## Test

```bash
uv run --with pytest --with fastmcp --with pydantic pytest tests/ -q
```

19 test. Quelli che contano davvero:
`test_targeted_message_reaches_only_its_session`,
`test_two_sessions_do_not_overwrite_each_other`,
`test_archive_keeps_message_in_its_own_session_dir`.

## Relazione con il resto

- **Non sostituisce** la lib bash né gli slash command: `/inbox`, `/bus`, `/send`, `/read`
  restano e leggono gli stessi file. I due layer convivono sullo stesso filesystem.
- **Sorgente del protocollo**: `~/Projects/01-Building/agent-team-os` (repo), da cui
  `install.sh` copia hook e lib in `~/.claude`. Non editare `~/.claude` direttamente.
- **Piani**: `work-hub/plans/PLAN-BUS-V2-SESSION-IDENTITY.md` (il fix),
  `work-hub/plans/PLAN-AGENT-TEAM-OS.md` (la v2.0 hive-GOD, che questo abilita).

## Scelte di progetto

- **Scritture atomiche** (temp + rename): un lettore non vede mai un file a metà.
- **Il campo `to` resta il nome nudo** anche per i messaggi mirati: thread e lettori
  esistenti non cambiano, lo slug vive nel percorso.
- **`.read/` e `.done/` nascono con la directory di sessione**: senza, il primo messaggio
  mirato arrivava dove non si poteva archiviare (trovato da Kai usandolo).
- **`archive` è idempotente**: archiviare due volte non è un errore.
- **Una sessione senza heartbeat da 240 minuti** è considerata morta (`AB_SESSION_TTL_MIN`).
