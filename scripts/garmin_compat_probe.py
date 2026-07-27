"""Print a read-only GarminCoach runtime compatibility inventory."""
from __future__ import annotations

import importlib
import importlib.metadata
import platform
import subprocess
import sys
from pathlib import Path


NOT_INSTALLED = "not_installed"
UNKNOWN = "unknown"


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return NOT_INSTALLED
    except Exception:
        return UNKNOWN


def _typed_imports() -> bool:
    try:
        importlib.import_module("garminconnect.typed")
    except Exception:
        return False
    return True


def _repository_sha() -> str:
    repository = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    sha = result.stdout.strip()
    if result.returncode != 0 or len(sha) != 40:
        return UNKNOWN
    return sha


def collect_probe() -> list[tuple[str, str]]:
    return [
        ("python_version", platform.python_version()),
        ("python_executable", sys.executable),
        ("operating_system", platform.platform()),
        ("garminconnect_version", _package_version("garminconnect")),
        ("garminconnect_typed_import", str(_typed_imports()).lower()),
        ("pydantic_version", _package_version("pydantic")),
        ("repository_sha", _repository_sha()),
    ]


def main() -> int:
    try:
        values = collect_probe()
        for key, value in values:
            print(f"{key}={value}")
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
