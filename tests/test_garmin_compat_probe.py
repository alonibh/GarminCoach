from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts import garmin_compat_probe as probe


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "garmin_compat_probe.py"
EXPECTED_KEYS = [
    "python_version",
    "python_executable",
    "operating_system",
    "garminconnect_version",
    "garminconnect_typed_import",
    "pydantic_version",
    "repository_sha",
]


def test_probe_cli_prints_only_the_approved_inventory_fields():
    marker = "must-not-appear-in-probe-output"
    env = os.environ.copy()
    env["GARMIN_PASSWORD"] = marker
    env["TELEGRAM_BOT_TOKEN"] = marker

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert marker not in result.stdout
    lines = result.stdout.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == EXPECTED_KEYS
    assert all("=" in line and line.split("=", 1)[1] for line in lines)


def test_missing_optional_packages_are_inventory_results(monkeypatch, capsys):
    def missing(_distribution):
        raise probe.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(probe.importlib.metadata, "version", missing)
    monkeypatch.setattr(
        probe.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ModuleNotFoundError()),
    )

    assert probe.main() == 0
    output = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert output["garminconnect_version"] == probe.NOT_INSTALLED
    assert output["garminconnect_typed_import"] == "false"
    assert output["pydantic_version"] == probe.NOT_INSTALLED


def test_actual_execution_failure_is_nonzero_and_silent(monkeypatch, capsys):
    monkeypatch.setattr(
        probe,
        "collect_probe",
        lambda: (_ for _ in ()).throw(RuntimeError("execution failed")),
    )

    assert probe.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_repository_sha_failure_is_reported_as_unknown(monkeypatch):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, "", "not a repo"),
    )

    assert probe._repository_sha() == probe.UNKNOWN
