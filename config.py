"""Central configuration. OS-agnostic (uses pathlib). Reads from .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (the directory this file lives in).
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _expand(path: str) -> Path:
    """Expand ~ and make relative paths project-root-relative."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


# --- Garmin ---
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "")
GARMIN_TOKEN_STORE = _expand(os.getenv("GARMIN_TOKEN_STORE", "~/.garminconnect"))
ICS_CALENDAR_URL = os.getenv("ICS_CALENDAR_URL", "")

# --- LLM ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# gemini-1.5-pro is retired on the free tier (new keys can't call it).
# gemini-2.5-flash is the current free-tier workhorse: high RPM/RPD, big
# context, strong instruction-following. Override per deployment in .env.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Output controls for the Gemini REST call (mirrors Claude's max_tokens cap).
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.3"))
GEMINI_TOP_P = float(os.getenv("GEMINI_TOP_P", "0.9"))
GEMINI_TOP_K = int(os.getenv("GEMINI_TOP_K", "40"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))

# --- Sync ---
INITIAL_BACKFILL_DAYS = int(os.getenv("INITIAL_BACKFILL_DAYS", "90"))
AUTO_SYNC_HOURS = [
    int(h) for h in os.getenv("AUTO_SYNC_HOURS", "7,19").split(",") if h.strip()
]

# --- App ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = _expand(os.getenv("DB_PATH", "garmincoach.db"))
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
_DEFAULT_SESSION_SECRET = "change-me-to-a-random-string"
SESSION_SECRET = os.getenv("SESSION_SECRET", _DEFAULT_SESSION_SECRET)
SESSION_MAX_AGE_DAYS = int(os.getenv("SESSION_MAX_AGE_DAYS", "30"))
DB_URL = f"sqlite:///{DB_PATH}"

# Security guard: a default signing secret makes session cookies forgeable, so
# refuse to run with auth enabled and the placeholder secret still in place.
if APP_USERNAME.strip() and SESSION_SECRET.strip() == _DEFAULT_SESSION_SECRET:
    raise RuntimeError(
        "SESSION_SECRET is still the default placeholder while app auth is "
        "enabled (APP_USERNAME is set). Generate a real secret with:\n"
        '  python -c "import secrets; print(secrets.token_hex(32))"\n'
        "and set it in .env, or leave APP_USERNAME blank to disable auth."
    )
