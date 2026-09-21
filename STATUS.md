# agent-bus-mcp — STATUS

**Stato**: 🟢 funzionante, 19 test verdi, e2e verificato via protocollo MCP (21 set 2026).
**Owner**: Alita. **Nato da**: bug sessioni parallele Kai, 21 set 2026.

## Fatto
- Layer filesystem (`bus.py`) con semantica allineata a bus v1.4 (`agent/slug`).
- 6 tool MCP: status, inbox, read, send, archive, thread.
- 19 test su AB_HOME isolato, incluso il caso che riproduce il bug.

## Da fare
- Registrare l'MCP in `~/.claude.json` e provarlo da una sessione vera.
- Valutare se `/inbox` e `/send` debbano diventare wrapper dei tool MCP (oggi
  duplicano la logica in bash). Non urgente: i due layer convivono.
- `delegate_inline` (previsto dal piano v2.0) non implementato.
