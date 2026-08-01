from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operator_health import render_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GarminCoach operator health")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-paths", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0
    output, code = render_health(deep=args.deep, as_json=args.json, show_paths=args.show_paths)
    print(output)
    return code


if __name__ == "__main__": raise SystemExit(main())
