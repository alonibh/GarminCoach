# Garmin Capability Registry

Registry version: `2026-07-29-v1`.

This is an offline, versioned evidence registry. It never fetches Garmin web
pages at startup or during sync. Each non-unknown mapping names an official
Garmin source and the date it was manually verified. An omitted model or
capability is unknown, not unsupported. A successful current-device response
can promote any registry state to supported; empty responses and ordinary,
authentication, or rate-limit failures cannot establish unsupported support.

Official source snapshots recorded in the module:

- `training_status_faq` — Training Status FAQ `VxKazDQ2mkAmDoQbJriEBA`
- `recovery_time_faq` — Recovery Time FAQ `8ImmxVkZMh4EYYq5Zp2bR8`
- `hrv_status_faq` — HRV Status FAQ `HnFAR4oFRF4kHeqYme3bU6`
- `body_battery_faq` — Body Battery FAQ `VOFJAsiXut9K19k1qEn5W5`
- `fitness_age_faq` — Fitness Age FAQ `CM1YJmMrrNAbEpM9PapJ07`
- `unified_training_status_faq` — Unified Training Status FAQ `EjPECQK58qA0xzJ5X74vm7`
- `vo2max_faq` — VO2 max FAQ `lWqSVlq3w76z5WoihLy5f8`
- `vivoactive_5_manual` and `vivoactive_5_specs` — official owner manual and product specifications.

The current verified model coverage is deliberately narrow: **vívoactive 5**.
Its Training Readiness and Training Status are unsupported; on-device Recovery
Time, HRV Status, Body Battery, and VO2 max are supported; Connect Recovery
Time is unsupported. Fitness Age remains unknown until an official model rule
or a valid account observation exists. All other watches retain stable
unknown-model identities and unknown capability rows.

`recovery_time_device` and `recovery_time_connect` are deliberately separate:
the former describes watch support, while the latter describes a Garmin Connect
payload expectation. This phase does not store a Recovery Time value.
