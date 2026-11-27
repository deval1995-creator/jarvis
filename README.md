# Jarvis

Personal assistant: a local Python Jarvis Core, Claude Code (Agent SDK) as the brain,
SQLite memory, local voice. Architecture: see the "Jarvis — Production Architecture" doc.

## Layout
- `core/` — Jarvis Core: API, sessions, database, memory (`memory.py`, `memory_mcp.py`,
  `memory_review.py`), connector policy (`connectors.py`)
- `voice/` — voice service: Pipecat pipeline (`pipeline.py`, `bot.py`), speech models
  (`models.py`), test page (`static/`)
- `jarvis_home/` — Jarvis persona (`CLAUDE.md`), copied to `%LOCALAPPDATA%\Jarvis\home` on start
- `scripts/` — one-off tools; `sdk_smoke.py` is the Week 1 go/no-go test, `voice_e2e.py`
  talks to the voice service with generated speech (no microphone)
- `tests/` — pytest

## Run
Always on: a supervisor keeps the Core and voice service running. It restarts a service that
stops, and one that stops answering (checked every 30 s). A Windows task checks every 30 s that
the supervisor itself is running and starts it if not. Logs: `%LOCALAPPDATA%\Jarvis\logs`.
Install once (no admin needed):

    powershell -File scripts\install_watchdog.ps1   # every 30 s; undo: Unregister-ScheduledTask "Jarvis watchdog"
    scripts\install_autostart.cmd                   # also at login; undo: delete it from shell:startup

Then just open http://127.0.0.1:8765. To run the parts by hand instead:

    uv run python -m core                         # then open http://127.0.0.1:8765
    uv run python -m voice                        # voice; use the mic controls on the chat page

The voice service needs the Core running. First start downloads Whisper large-v3-turbo
(~1.6 GB) and Kokoro (~350 MB). Both run on the GPU (CUDA 12 and cuDNN 9 come from pip
wheels; no CUDA install needed). Settings (environment variables): `JARVIS_VOICE`
(Kokoro voice or a blend like `bm_george:0.6+bm_fable:0.4`, default `bm_fable`),
`JARVIS_VOICE_SPEED` (default 1.05), `JARVIS_WHISPER_MODEL`, `JARVIS_WHISPER_DEVICE` and
`JARVIS_TTS_DEVICE` (`cuda` or `cpu`), `JARVIS_VAD_START_SECS` (hands-free: how much
speech counts as talking over Jarvis, default 0.2).

Modes on the test page: **push-to-talk** (hold the button or Space; pressing while Jarvis
speaks cuts it off) and **hands-free** (Silero VAD + Smart Turn v3 decide when you're done).

## Internet tools (keys live in %LOCALAPPDATA%\Jarvis, never in the repo)
- `search`, `read_page`: Tavily (`tavily_api_key`)
- `places`: Google Places API (New) (`google_places_api_key`)
- `weather`: Open-Meteo (no key)
- `browser_*`: Playwright, Jarvis's own headless Chromium with no logins; clicks and typing
  need an approval card. One-time setup: `uv run python -m playwright install chromium`
- Fallback: Claude Code's WebSearch / WebFetch

## Commands
    uv run pytest                                 # tests
    uv run python scripts/sdk_smoke.py --runs 5   # SDK on your Pro plan + timings
    uv run python scripts/voice_e2e.py --interrupt [--mode hands-free]   # voice timings

The live database is `%LOCALAPPDATA%\Jarvis\jarvis.db` (override with `JARVIS_DATA_DIR`).
