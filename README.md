# Jarvis

Personal assistant: a local Python Jarvis Core, Claude Code (Agent SDK) as the brain,
SQLite memory, local voice. Architecture: see the "Jarvis — Production Architecture" doc.

## Layout
- `core/` — Jarvis Core: API, sessions, database (`core/db.py`)
- `mcp_memory/` — local memory MCP server (Week 2)
- `voice/` — Pipecat voice pipeline (Week 3)
- `app/` — Tauri desktop app (Week 4)
- `scripts/` — one-off tools; `sdk_smoke.py` is the Week 1 go/no-go test
- `tests/` — pytest

## Commands
    uv run pytest                                 # tests
    uv run python scripts/sdk_smoke.py --runs 5   # SDK on your Pro plan + timings

The live database is `%LOCALAPPDATA%\Jarvis\jarvis.db` (override with `JARVIS_DATA_DIR`).
