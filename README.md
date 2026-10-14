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
| `bus_status` | chi è sul bus e **quali sessioni sono vive adesso**, su tutte e due le macchine, con i pendenti di ciascuna |
| `bus_inbox` | i messaggi di *questa* sessione (i suoi + i broadcast); `all_sessions` per tutti. Ogni riga ha `reply_to` |
| `bus_read` | un messaggio per intero, con `_location` che dice dove sta e se è archiviato, e `_reply_to` |
| `bus_send` | invia a `kai` (tutte le sessioni), `kai/noi-calendar` (una sola), `kai@mini/crewmoor` (sull'altra macchina) |
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

## Due macchine (v2.5)

Laptop (hub, dove sta Alita) e Mac mini, stessi percorsi e stessa `AGENT_MAP.json`. Solo il
laptop apre SSH verso il mini, mai il contrario. Stessa semantica della lib bash, stessi file:

```
kai@mini             → tutte le sessioni Kai sul mini
kai@mini/crewmoor    → una sessione sul mini
alita                → dal mini va al laptop (l'hub vive sull'host hub)
```

- `hosts` in `AGENT_MAP.json`, nome della macchina in `~/.agent-team-os/HOST` (override
  `AB_HOST`; senza file = l'host con `hub: true`).
- **laptop → mini**: scrittura atomica via SSH nella inbox del mini. Mini giù: il messaggio
  resta in `outbox/mini/` e `bus_send` torna `queued: true` con un warning, non un errore.
- **mini → laptop**: il messaggio aspetta in `outbox/laptop/` del mini (`queued: true`).
- **Raccolta**: `bus_inbox` e `bus_status` sul laptop spingono la coda e tirano l'outbox del
  mini se l'ultima raccolta ha più di 30 s (copia, verifica, poi cancella sul mini). Lock e
  timbro condivisi con la lib bash e con il launchd di raccolta.
- **Stato**: le sessioni del mini lette via SSH dal suo `registry/`, cache 20 s; se il mini
  è giù resta l'ultimo elenco con `reachable: false`.
- Ogni messaggio porta `context.host` (macchina del mittente); `reply_to` lo usa.

SSH: `AB_SSH_BIN` (default `ssh`), `BatchMode=yes`, `ConnectTimeout=AB_SSH_TIMEOUT` (5),
`ControlMaster auto` con socket in `~/.ssh/cm-%r@%h:%p` per 10 minuti.

## Identità del chiamante

Dedotta dalla working directory via le `rules` di `AGENT_MAP.json`, quindi una sessione non
deve dichiarare chi è. Override con `AB_AGENT` e `AB_SESSION_SLUG` dove le regole non
arrivano. `AB_HOME` punta a un bus diverso (i test lo usano).

**Identità sbagliata?** Un server MCP eredita la cwd del client, che con alcuni launcher è
la cartella del server invece del workspace della sessione: in quel caso ogni chiamata
risulterebbe fatta dall'agente che possiede questa cartella. Il server se ne accorge e
rifiuta invece di indovinare. Rimedio: `AB_AGENT` e `AB_SESSION_SLUG` nell'entry MCP.
**Non** usare `${CLAUDE_PROJECT_DIR}` in `env`: se il client non lo espande, la stringa
letterale è peggio di niente — il server la scarta apposta.

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

45 test. Quelli che contano davvero:
`test_targeted_message_reaches_only_its_session`,
`test_two_sessions_do_not_overwrite_each_other`,
`test_archive_keeps_message_in_its_own_session_dir`, e per le due macchine
(`tests/test_hosts.py`, con un `ssh` finto nel `PATH` che esegue in locale su un secondo
`AB_HOME`): `test_mini_down_queues_and_does_not_fail`,
`test_collect_pushes_the_queue_and_pulls_the_outbox`,
`test_pull_is_idempotent_and_keeps_what_it_cannot_read`.

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
