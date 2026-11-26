Source of Jarvis's persona. `CLAUDE.md` here is copied on every Core start to
Jarvis's Claude Code home, `%LOCALAPPDATA%\Jarvis\home`, which is the working
directory of Jarvis's Claude sessions. That home is outside this repo on purpose:
- dev sessions in C:\Jarvis don't load Jarvis's persona;
- Claude Code saves per-project settings (switched-off connectors) by repo root,
  so Jarvis's connector choices must not live inside this repo.
