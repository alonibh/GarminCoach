# GarminConnect 0.3.7 Compatibility Preflight

**Phase:** 2A.1

**Date:** 2026-07-27

**Policy:** [`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md)

**Conclusion:** Do not upgrade production yet.

This report is a read-only preflight. It does not change the production Python
runtime, `requirements.txt`, Garmin authentication, synchronization, stored
data, or coaching behavior. No Garmin account or endpoint was used.

## Current runtime assumptions

| Area | Current assumption | Consequence for Python 3.12 |
| --- | --- | --- |
| Declared application runtime | `README.md` and `run.bat` say Python 3.11+ | This does not guarantee 3.12. |
| Normal CI | `.github/workflows/deploy.yml` pins Python 3.11 | Normal CI cannot install `garminconnect==0.3.7`, whose package metadata requires Python 3.12+. |
| Production executable | systemd starts `/home/ubuntu/garmincoach/.venv/bin/uvicorn`; deploy and reset workflows activate `.venv` and invoke its `python` | The virtual environment's original interpreter, not the `python3` command currently on `PATH`, determines production Python. |
| GitHub deployment | Reuses the existing `.venv` and runs `python -m pip install -r requirements.txt` | It never recreates or upgrades the virtual environment interpreter. |
| `deploy.ps1` / `setup.sh` path | Excludes `.venv` from the archive, then runs unversioned `python3 -m venv .venv` on the server | It does not prove `python3` is 3.12 and does not explicitly create a clean replacement environment. |
| Production reset/recovery | Reuses `.venv`, runs reset code with its `python`, and reinstalls `requirements.txt` into it | A reset does not migrate the runtime. |
| Test configuration | There is no `pyproject.toml`, `tox.ini`, `.python-version`, or pytest Python-version constraint | The workflow or selected executable is the only runtime pin. |
| Local Windows launcher | Reuses `.venv` after first creation and accepts any discovered Python advertised as 3.11+ | It does not migrate an existing environment to 3.12. |

Moving production to Python 3.12 requires a newly created, explicitly
versioned environment, for example one built with `python3.12 -m venv`, followed
by dependency installation and smoke checks before systemd is switched. The
current GitHub deployment does not perform that operation.

The current deployment cannot be considered able to install Python-3.12-only
packages until the production probe reports the actual `.venv` interpreter.
If it reports Python 3.11 or lower, pip cannot install `garminconnect==0.3.7`.
Even if it reports 3.12+, the response and authentication blockers below still
prevent an upgrade.

## Isolated compatibility test

The test-only dependency file
[`requirements-compat-garminconnect-037.txt`](../requirements-compat-garminconnect-037.txt)
installs the normal requirements together with the exact requirement
`garminconnect[typed]==0.3.7`. `requirements.txt` is unchanged.

The non-production CI job uses Python 3.12, asserts the installed distribution
is exactly `0.3.7`, and runs the complete suite. It is intentionally not a
dependency of the production deploy job in this preflight phase.

Local isolated result:

- Python: 3.12.13
- `garminconnect`: exactly 0.3.7
- Pydantic: 2.13.4
- Tests collected: 403
- Passed: 400
- Expected incompatibilities (`xfail`): 3
- Unexpected failures: 0

The unchanged default local environment also completed the full `tests/` tree:
374 passed, 26 exact-0.3.7 checks skipped, and the same 3 known contract gaps
were recorded as expected incompatibilities.

All methods currently called by `sync/garmin_client.py` still exist and accept
the current call shapes in 0.3.7. The tested surface includes authentication
entry points, token serialization members, activities, activity detail,
strength sets, HR zones, sleep, HRV, Body Battery, stress, resting HR, steps,
daily stats, device metadata, Training Readiness, Training Status, and workout
reads. This is an offline signature check, not evidence that Garmin accepts a
production token or returns an account-specific payload.

## Response-shape findings

Sanitized synthetic fixtures cover daily stats, sleep, HRV, Body Battery,
Training Readiness list snapshots, the legacy Training Readiness dictionary,
an empty response, multiple same-day snapshots, and activities.

| Contract | Result |
| --- | --- |
| Daily stats | Current raw-dictionary fields used by GarminCoach parse successfully; the 0.3.7 `DailyStats` typed model validates the fixture. |
| Sleep core fields | Duration, stages, timestamps, and score parse successfully. |
| Sleep respiration | Incompatible: the 0.3.7 typed model uses `avgRespirationValue`; the current adapter reads `averageRespirationValue`. |
| HRV | Current overnight and baseline fields parse successfully; the 0.3.7 `HrvData` model validates the fixture. |
| Body Battery | Current value-array parsing succeeds; the 0.3.7 `BodyBatteryEntry` model validates the fixture. |
| Activities | Current summary parsing succeeds; the 0.3.7 `Activity` model validates the fixture. |
| Legacy Training Readiness dictionary | Current `trainingReadiness` parsing succeeds. |
| Empty Training Readiness list | Safely remains missing. |
| 0.3.7 Training Readiness list | Incompatible: 0.3.7 declares `list[dict]` with snapshot field `score`; GarminCoach declares and parses a dictionary containing `trainingReadiness` or `value`. |
| Multiple same-day readiness snapshots | Incompatible: there is no adapter that filters to the decision date and selects the latest valid snapshot. |

The incompatibilities are explicit strict expected failures. They do not
silently pass as compatible behavior.

## Adapters that must change

The next implementation must remain at the Garmin boundary:

1. Change `GarminClient.training_readiness` to expose the real response
   contract rather than declaring `dict`.
2. Add one pure Training Readiness normalization function that accepts the
   legacy dictionary and 0.3.7 snapshot list, rejects invalid/off-date entries,
   and selects the latest valid same-day snapshot.
3. Route both `_sync_daily_health` and `run_priority_sync` through that
   normalizer so they cannot disagree.
4. Accept both known sleep respiration aliases without changing other sleep
   semantics.
5. If `TypedGarmin` is adopted later, convert typed models to the existing
   internal raw contract at one boundary; current sync parsers assume
   dictionaries and must not receive Pydantic models directly.

No decision-engine or sync-orchestration change is needed for these contract
fixes.

## Authentication and token-format risks

GarminCoach currently encrypts the string returned by `api.client.dumps()` and
restores it with `api.client.loads()`. Version 0.3.7 serializes native DI OAuth
access/refresh data with a different token structure and uses
`garmin_tokens.json` for path-based storage. Its loader requires recognized
token fields.

Therefore:

- a token blob created by a pre-0.3 production installation may not load under
  0.3.7;
- a one-time password/MFA reauthentication may be required;
- successful offline existence/signature checks do not prove cached-token
  restore, refresh, MFA resume, or production account login;
- the production token format and installed package version remain unknown
  because this task did not read credentials or token storage.

Before an upgrade, sanitized token-format fixtures must cover current encrypted
blob restore and 0.3.7 serialization, and the owner must explicitly accept the
possible one-time reauthentication.

## Upgrade decision

Upgrading is **not currently safe**.

Python 3.12 and the exact dependency can install and run the complete offline
suite, and the methods GarminCoach calls are present. However, production
runtime evidence is missing, current Training Readiness parsing would discard
the 0.3.7 snapshot-list response, the latest same-day snapshot is not selected,
the sleep respiration alias differs, and pre-0.3 token restoration is
unverified.

## Exact next implementation task

**Phase 2A.2:** implement and test a pure Garmin response-normalization adapter
for the 0.3.7 Training Readiness snapshot list and sleep respiration alias,
route both existing readiness ingestion paths through it, and add sanitized
old/new token-format compatibility tests. Do not change sync orchestration,
coaching authority, or production dependency pins in that task.

## Required production evidence

After this commit is present on the production checkout, the owner must run
exactly:

```bash
cd /home/ubuntu/garmincoach && /home/ubuntu/garmincoach/.venv/bin/python /home/ubuntu/garmincoach/scripts/garmin_compat_probe.py
```

The command is read-only. It makes no Garmin request, reads no credentials or
tokens, and modifies no file or database. Record its complete seven-line output
before planning the Python 3.12 environment replacement. Until that output is
available, this report makes no claim about the production Python,
`garminconnect`, Pydantic, typed-import, executable, or repository versions.
