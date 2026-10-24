# agent-bus-mcp

MCP server per il bus Agent Team OS. Sostituisce heredoc bash e scrittura diretta di JSON
nelle inbox con sei tool tipizzati.

Nato il 21 set 2026 dal bug delle sessioni parallele: due Kai aperti insieme
(`microsoft-mcp` e `noi-calendar`) si rubavano i messaggi perché il bus li vedeva come un
unico destinatario. Il fix (bus v1.4, `agent/slug`) vive nella lib bash; questo server lo
espone come API di prima classe.

## Bus v3 (BR24-F5.5)

Il server è solo un client sottile: ogni tool è una chiamata al **centralino** (daemon di
`agent-team-os/centralino`, socket `$AB_HOME/centralino.sock`), che tiene tutto lo stato in SQLite.
Qui non c'è più il layer a file (`bus.py` è stato tolto): niente interruttore `AB_CENTRALINO`,
niente `centralino.json`. Con il centralino giù le tool falliscono con un errore chiaro, senza ripiego.
Le tool sono sei (`bus_send`, `bus_reply`, `bus_close`, `bus_inbox`, `bus_read`, `bus_status`);
`bus_gestisci`, `bus_archive`, `bus_waits`, `bus_expect` e `bus_thread` restano come alias fino
al 31 ottobre 2026. Un battito ogni 60 s dice al centralino che la sessione è viva. Le sezioni
qui sotto (file di casella, `.read/`, `.done/`, lock) descrivono il bus a file e valgono solo
per il mini, finché non passa anche lui al centralino.

## Tool

| Tool | A cosa serve |
|---|---|
| `bus_status` | chi è sul bus e **quali sessioni sono vive adesso**, su tutte e due le macchine, con i pendenti di ciascuna |
| `bus_inbox` | i messaggi di *questa* sessione (i suoi + i broadcast); `all_sessions` per tutti. Ogni riga ha `reply_to` |
| `bus_read` | un messaggio per intero, con `_location` che dice dove sta e se è archiviato, e `_reply_to` |
| `bus_send` | invia a `kai` (tutte le sessioni), `kai/noi-calendar` (una sola), `kai@mini/crewmoor` (sull'altra macchina) |
| `bus_archive` | sposta in `.done/` o `.read/` **accanto** al messaggio, non nella radice |
| `bus_thread` | rigioca una conversazione in ordine |
| `bus_expect` | (v2.6) registra un esito atteso su questa sessione quando deleghi senza `bus_send` (es. `onda_agent_prompt`); restituisce `task_id` e la riga da mettere in fondo al prompt |
| `bus_waits` | (v2.6) le deleghe che questa sessione aspetta, con età, scadenza e motivo di una sveglia rimandata; (v2.7) `waiting_on_you`, le domande dell'esecutore ancora senza risposta; (v2.8) `unhandled_returns`, i ritorni senza esito |
| `bus_gestisci` | (v2.8) consuma un messaggio con un esito: `chiuso` (attesa chiusa), `riarma` (aspetti ancora, non parte nulla), `risposto` (con `reply`: risposta con lo stesso `task_id`, sveglia l'esecutore), `letto`; `task_id` decide un messaggio da agganciare |

`bus_send` ha `kind` (v2.8, piano BS21): obbligatorio senza default per `request`, `brief` e `handoff`, `delega` (vuoi l'esito) o `comunicazione` (non torna niente); senza, l'invio è rifiutato con un errore che spiega la scelta. Con `kind='delega'` l'attesa si registra da sola prima della consegna (`attesa registrata t-…` in `note`), il messaggio porta il `task_id`, e ogni ritorno sveglia chi aspetta: lo fa `agent-team-os-wake.py` (repo agent-team-os), che aggancia anche i ritorni scritti come messaggi nuovi. `expect_result=true` resta come vecchio nome di `kind='delega'`. Una risposta con `in_reply_to` eredita il `task_id`. `wake_chain` resta timbrato ma non blocca più: le catene le fermano i tetti della sveglia.

Domande (v2.7): chi esegue chiede al delegante con `message_type='question'` e lo stesso `task_id` (con `in_reply_to` sulla delega lo eredita). Ogni domanda sveglia il delegante una volta, l'esito lo sveglia comunque dopo. Chi chiede ha `waiting_agent` (`agent`, `address`, `task_id`, `msg_id`, `since`, `summary`) nella sua voce `registry/<agent>.d/<slug>.json` finché non legge la risposta (`bus_read`) o non manda l'esito.

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
uv run --with pytest pytest -q
```

21 test (`tests/test_centralino.py`): un finto centralino su un socket Unix registra le richieste e
verifica la mappa tool -> operazione (v3 e alias), gli argomenti, i rifiuti, il centralino giù, il
battito, e come si decide la cartella del chiamante. La logica del bus ha i suoi test in
`agent-team-os/centralino/tests` e nel banco di prova `centralino sim`.

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
