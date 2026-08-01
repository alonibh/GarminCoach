from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verified_backup import BackupError, restore_plan, verify_verified_backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Phase 6A backup without restoring it")
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--against-current-config", action="store_true")
    parser.add_argument("--restore-plan-json", action="store_true")
    try: args = parser.parse_args(argv)
    except SystemExit as exc: return 64 if exc.code else 0
    try:
        result = restore_plan(args.backup_directory, against_current_config=args.against_current_config) if args.restore_plan_json else verify_verified_backup(args.backup_directory, against_current_config=args.against_current_config)
    except BackupError:
        print("ERROR: backup verification failed", file=sys.stderr); return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__": raise SystemExit(main())
