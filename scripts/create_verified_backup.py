from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verified_backup import BackupError, create_verified_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an explicit verified GarminCoach SQLite backup")
    parser.add_argument("--output-root", type=Path)
    try: args = parser.parse_args(argv)
    except SystemExit as exc: return 64 if exc.code else 0
    try:
        directory = create_verified_backup(args.output_root)
    except (BackupError, Exception) as exc:
        print("ERROR: verified backup failed", file=sys.stderr)
        return 1
    print(f"Verified backup created: {directory.name}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
