# Rules for Agents

- Project context:
  - GarminCoach is a personal Garmin Connect health/fitness dashboard with AI coaching.
  - Backend: Python 3, FastAPI, Uvicorn. Frontend: Jinja2 templates, HTML, static files.
  - Database: local SQLite file (`garmincoach.db`) with schema and migrations handled in `db.py` via `init_db()` and `_migrate_add_columns()`. There is no external database server.
  - AI integration: currently configured for Gemini 2.5 Flash, with support for Claude and local Ollama models.
  - Local project root on Windows: `C:\Projects\garmincoach`.
  - Main local deployment script: `C:\Projects\garmincoach\deploy.ps1`.
  - Local-only deployment details may be available in `.codex/local-context.md`; do not commit that file.
- Infrastructure and deployment:
  - Production runs on an Ubuntu VM; no DNS name is configured.
  - Deployment is lightweight/script-based. `deploy.ps1` bundles the app into `garmincoach.tar.gz`, excluding virtualenvs, `__pycache__`, and the database, uploads the archive plus `setup.sh` with `scp`, then runs `setup.sh` over SSH.
  - `setup.sh` runs remotely to update system packages, extract the archive, create/update `.venv`, install `requirements.txt`, and configure `iptables`.
  - The app runs as systemd service `garmincoach.service`.
  - Uvicorn listens on port `8000`; HTTP port `80` is mapped to `8000` with `iptables` PREROUTING so Uvicorn does not run as root.
  - Runtime configuration is in `.env`, with `.env.example` as the template. It includes Garmin credentials, LLM API keys, sync intervals, session secrets, and optional Telegram bot settings.
- Commit and push to GitHub after every chat/piece of work.
- Discard (revert) changes that are not relevant or were abandoned during testing.
- ALWAYS verify the deployment after pushing code and restarting the remote server. You must check the service logs (`sudo journalctl -u garmincoach -n 50`) and perform a quick smoke test (e.g. `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/`) to ensure the server didn't crash on startup.
- After each change you make, you must make sure you didn't break anything, that the new change actually works, and all tests pass. If new tests are needed, add them.
- Follow `.agents/rules/git-test-workflow.md` for the full Git, test, cleanup, commit, and push protocol.

# Communication Rules

- Minimize the thinking output and show only the final outcome. Keep responses concise and direct.
