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

# --- LLM ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

# --- Sync ---
INITIAL_BACKFILL_DAYS = int(os.getenv("INITIAL_BACKFILL_DAYS", "90"))
AUTO_SYNC_HOURS = [
    int(h) for h in os.getenv("AUTO_SYNC_HOURS", "7,19").split(",") if h.strip()
]

# --- App ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = _expand(os.getenv("DB_PATH", "garmincoach.db"))
DB_URL = f"sqlite:///{DB_PATH}"
