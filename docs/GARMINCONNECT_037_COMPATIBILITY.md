# GarminConnect 0.3.7 Compatibility

**Phase:** 2A.3

**Date:** 2026-07-27

**Policy:** [`METRIC_SYNC_POLICY.md`](METRIC_SYNC_POLICY.md)

**Conclusion:** The known response-contract incompatibilities are fixed. The
production runtime is already Python 3.12, so this is a controlled package
upgrade to `garminconnect[typed]==0.3.7`, with the current environment and Git
SHA retained for rollback. A production token may still require reauthentication.

This package upgrade does not change Garmin authentication behavior, sync
orchestration, database schema, coaching authority, or Telegram behavior. No
Garmin account, endpoint, credential, or real token is used by its checks.

## Controlled package-upgrade contract

| Area | Current assumption | Migration consequence |
| --- | --- | --- |
| Production preflight | Python 3.12.3; `.venv/bin/python` resolves to `/usr/bin/python3.12`; `garminconnect` is 0.3.6 | No interpreter installation or runtime migration is needed. |
| Declared application runtime | `README.md`, `run.bat`, setup, and CI require Python 3.12 | Python 3.12 is explicit rather than inferred from `python3`. |
| Application dependency | `requirements.txt` pins `garminconnect[typed]==0.3.7` | Every normal test and deployment environment validates the exact distribution. |
| CI | The complete offline suite runs under Python 3.12 | The former compatibility-only job is unnecessary; its contract tests run in the normal suite. |
| Candidate environment | Deployment clones `.venv` to `.venv-garminconnect-037-<SHA>` | The working `.venv` and its package state are never modified. |
| Runtime selection | A temporary systemd override starts the checked candidate | It is installed only after exact-package/import, compile/import, and compatibility-test checks pass. |
| Rollback | The deployment trap restores the previous Git SHA and systemd override | The original `.venv` remains the rollback environment. |

The read-only production preflight captured this runtime evidence before the
package switch. It made no Garmin request and did not read tokens or databases.

## Compatibility-test results

The main dependency file installs the exact requirement
`garminconnect[typed]==0.3.7`; all compatibility contracts run in the standard
Python 3.12 suite.

Local isolated result:

- Python: 3.12.13
- `garminconnect`: exactly 0.3.7
- Pydantic: 2.13.4
- Complete suite: 414 passed
- Expected failures: 0
- Unexpected failures: 0

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

- the actual production token serialization generation;
- whether the real token can be refreshed or must be replaced;
- whether fresh login and MFA complete successfully for the production account;
- real-account response variations beyond the sanitized supported fixtures.

Even with Python 3.12 confirmed, the owner must plan for a controlled one-time
reauthentication and retain a rollback path that does not destroy the existing
environment or encrypted token.

## Upgrade decision

The response adapters and offline contracts are ready for a controlled,
rollback-capable package upgrade. Token reauthentication remains an expected,
owner-supervised possibility after a healthy deployment.

## Exact next step

Deployment clones the existing Python 3.12 environment, installs the pinned
package in that candidate, runs compatibility tests and smoke checks, then
switches systemd only after those checks pass. Do not enter credentials or MFA
during deployment. If the healthy application reports an expired Garmin
session, reauthenticate through its existing Connect Garmin account UI.

## Required production evidence

The completed preflight ran:

```bash
cd /home/ubuntu/garmincoach && /home/ubuntu/garmincoach/.venv/bin/python /home/ubuntu/garmincoach/scripts/garmin_compat_probe.py
```

The command is read-only. It makes no Garmin request, reads no credentials or
tokens, and modifies no file or database.
