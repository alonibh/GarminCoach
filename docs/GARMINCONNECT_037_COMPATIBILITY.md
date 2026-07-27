# GarminConnect 0.3.7 Compatibility

**Phase:** 2A.2

**Date:** 2026-07-27

**Policy:** [`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md)

**Conclusion:** The known response-contract incompatibilities are fixed, and
the code is ready for a controlled runtime/dependency migration. A production
upgrade is not yet proven safe because the production interpreter and token
format have not been observed, and a real account may require
reauthentication.

This work does not change the production Python runtime, `requirements.txt`,
Garmin authentication behavior, sync orchestration, database schema, coaching
authority, or Telegram behavior. No Garmin account, endpoint, credential, or
real token was used.

## Current runtime assumptions

| Area | Current assumption | Migration consequence |
| --- | --- | --- |
| Declared application runtime | `README.md` and `run.bat` say Python 3.11+ | This does not prove that production is running Python 3.12. |
| Normal CI | `.github/workflows/deploy.yml` pins Python 3.11 | Normal CI cannot install `garminconnect==0.3.7`, which requires Python 3.12+. |
| Compatibility CI | A separate job pins Python 3.12 and `garminconnect[typed]==0.3.7` | It tests the complete offline suite without changing production dependencies. |
| Production executable | systemd starts `/home/ubuntu/garmincoach/.venv/bin/uvicorn`; deploy and reset workflows activate that `.venv` | The virtual environment's original interpreter determines production Python. |
| GitHub deployment | Reuses `.venv` and installs `requirements.txt` into it | It does not recreate or upgrade the environment's interpreter. |
| `deploy.ps1` / `setup.sh` path | Creates `.venv` with unversioned `python3` when needed | `python3` must not be assumed to mean Python 3.12. |
| Reset/recovery | Reuses `.venv` and its `python` | Recovery does not migrate the runtime. |

Moving production to Python 3.12 still requires a new environment created with
an explicitly verified Python 3.12 executable. The existing production
environment must not be recreated until the read-only probe below has been run
and its output recorded.

## Compatibility-test results

The test-only dependency file
[`requirements-compat-garminconnect-037.txt`](../requirements-compat-garminconnect-037.txt)
installs the exact requirement `garminconnect[typed]==0.3.7` without changing
`requirements.txt`.

Local isolated result:

- Python: 3.12.13
- `garminconnect`: exactly 0.3.7
- Pydantic: 2.13.4
- Complete suite: 414 passed
- Expected failures: 0
- Unexpected failures: 0

The unchanged normal environment also completed the full suite: 386 passed
and 28 exact-0.3.7-only checks skipped. There were no failures.

The three Phase 2A.1 expected incompatibilities now pass:

1. `avgRespirationValue` is accepted alongside
   `averageRespirationValue`.
2. Training Readiness snapshot lists normalize into the existing internal
   dictionary shape.
3. Multiple same-day snapshots select the latest valid timestamp.

All tests are offline. Method-signature and synthetic-token checks are not
evidence that Garmin accepts a production account's token or payload.

## Fixed response contracts

One pure Garmin-boundary normalizer now handles legacy Training Readiness
dictionaries and 0.3.7 snapshot lists. It receives the target local decision
date, rejects malformed and off-date entries, returns missing for an empty
valid response, and selects the latest valid same-day snapshot by timestamp.
The normalized dictionary retains the score as `trainingReadiness` and
preserves available source fields such as `recoveryTime` and `level`.

Both normal daily-health sync and priority/morning sync call this same
normalizer. There is no second response-shape assumption that can select a
different value. The normalizer validates transport data only; it does not
interpret a score or make a coaching decision.

Sleep ingestion now maps both known respiration aliases into the existing
`respiration_avg` field. Sleep calculation, freshness, cadence, and UI behavior
are unchanged.

## Token-contract findings

Sanitized synthetic fixtures and offline tests establish these distinct
contracts:

| Library contract | Serialized structure | Result |
| --- | --- | --- |
| Pre-0.3 (`garth`) | Base64-encoded JSON array containing an OAuth1 object followed by an OAuth2 object | The expected field structure and encoding are covered. |
| GarminConnect 0.3.7 | JSON object containing `di_token`, `di_refresh_token`, and `di_client_id` | Synthetic serialization and loading succeed under exact 0.3.7. |

The encrypted `UserSecretVault` round-trips synthetic 0.3.7 token data, and the
encrypted file does not contain the synthetic access token in plaintext.
Existing MFA continuation remains process-local, is reused to complete the
login, and is not written to the token directory before MFA completes.

The exact 0.3.7 loader gracefully rejects a synthetic pre-0.3 serialized token
as structurally unsupported and remains unauthenticated. Therefore an existing
production token may require a fresh login and possibly MFA after migration.
This phase deliberately does not translate, discard, or automatically replace
an old token, and it does not change production authentication behavior.

## Remaining uncertainty and risk

The following are still unproven:

- the Python executable and version behind the production `.venv`;
- the installed production `garminconnect` version;
- the actual production token serialization generation;
- whether the real token can be refreshed or must be replaced;
- whether fresh login and MFA complete successfully for the production account;
- real-account response variations beyond the sanitized supported fixtures.

No production compatibility claim should be made until the probe output is
captured. Even with Python 3.12 confirmed, the owner must plan for a controlled
one-time reauthentication and retain a rollback path that does not destroy the
existing environment or encrypted token.

## Upgrade decision

The response adapters and offline contracts are ready for a controlled
runtime/dependency migration. An immediate in-place production upgrade is
**not yet safe** because production-version evidence and a real-token
reauthentication outcome are still missing.

## Exact next step

**Phase 2A.3:** capture the production probe output, then build a separate
explicit Python 3.12 virtual environment with exact
`garminconnect[typed]==0.3.7`; run the full tests and offline smoke checks
there; perform an owner-supervised token restore or fresh-login/MFA validation;
and switch systemd only after those checks pass with the current environment
retained for rollback.

## Required production evidence

The owner must run exactly:

```bash
cd /home/ubuntu/garmincoach && /home/ubuntu/garmincoach/.venv/bin/python /home/ubuntu/garmincoach/scripts/garmin_compat_probe.py
```

The command is read-only. It makes no Garmin request, reads no credentials or
tokens, and modifies no file or database. Record its complete seven-line
output before planning the Python 3.12 environment replacement.
