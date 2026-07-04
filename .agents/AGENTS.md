# Rules for Agents

## Project Context
- GarminCoach is a personal Garmin Connect health/fitness dashboard with AI coaching.
- **Backend**: Python 3, FastAPI, Uvicorn. **Frontend**: Jinja2 templates + vanilla HTML/CSS/JS in `static/`.
- **Packages**: `coach/` (LLM, snapshot, calendar, actions), `sync/` (Garmin client, scheduler, sync service), `metrics/` (engine, freshness), `notify/` (Telegram, reminders, weekly).
- **Database**: local SQLite (`garmincoach.db`); schema/migrations in `db.py` → `init_db()` + `_migrate_add_columns()`. No external DB server.
- **AI**: `coach/llm.py` supports `gemini` (default in prod: `gemini-2.5-flash`), `claude` (`claude-haiku-4-5`), and `ollama`. Provider set via `LLM_PROVIDER` in `.env`.
- **Config**: `.env` (from `.env.example`). Holds Garmin creds, LLM keys, sync schedule, session secret, optional Telegram settings.
- **Local root**: `C:\Projects\garmincoach`. Deployment script: `deploy.ps1`.
- Local-only details in `.codex/local-context.md` — **do not commit**.

## Infrastructure
- Prod: Ubuntu VM, systemd service `garmincoach.service`, Uvicorn on port `8000`, iptables maps `80 → 8000`.
- Deploy: `deploy.ps1` → tarballs app (excludes venvs, `__pycache__`, DB) → `scp` to VM → remote `setup.sh` (installs deps, restarts service).

## Workflow Rules
1. **Test first**: Run `python -m pytest tests/ -x -q` before committing. Fix all failures.
2. **Commit & push**: Commit all relevant changes and push to GitHub at the end of every session. Revert/discard abandoned changes.
3. **Verify deployment**: After restarting the remote service, always check:
   - Logs: `sudo journalctl -u garmincoach -n 50`
   - Smoke test: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/`
4. **No regressions**: If a change could break something, add a test for it.
