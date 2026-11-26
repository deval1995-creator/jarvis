# Jarvis

Personal assistant: a local Python Jarvis Core, Claude Code (Agent SDK) as the brain,
SQLite memory, local voice. Architecture: see the "Jarvis — Production Architecture" doc.

## Layout
- `core/` — Jarvis Core: API, sessions, database, memory (`memory.py`, `memory_mcp.py`,
  `memory_review.py`), connector policy (`connectors.py`)
- `voice/` — Pipecat voice pipeline (Week 3)
- `app/` — Tauri desktop app (Week 4)
- `jarvis_home/` — Jarvis persona (`CLAUDE.md`), copied to `%LOCALAPPDATA%\Jarvis\home` on start
- `scripts/` — one-off tools; `sdk_smoke.py` is the Week 1 go/no-go test
- `tests/` — pytest

## Run
    uv run python -m core                         # then open http://127.0.0.1:8765

## Commands
    uv run pytest                                 # tests
    uv run python scripts/sdk_smoke.py --runs 5   # SDK on your Pro plan + timings

The live database is `%LOCALAPPDATA%\Jarvis\jarvis.db` (override with `JARVIS_DATA_DIR`).
