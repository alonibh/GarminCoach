# 🏃 GarminCoach

A personal, **local** web app that pulls everything off your Garmin watch
(workouts incl. per-set drills/reps/weights, sleep, HRV, Body Battery, stress,
resting HR, steps), turns it into a clean dashboard and type-aware workout
views, and (Phase 3) acts as an adaptive coach. Runs free on your own Windows
or Linux machine — **self-hosted; your data stays on hardware you control.**
(You can optionally expose it over the internet — see Phase 4 — but there's no
third-party cloud in the loop.)

Built with **FastAPI · SQLAlchemy 2.0 · Jinja2 · Chart.js · garminconnect**, on
Python 3.11+.

## Features

**Dashboard**
- Past-week trend charts for **Sleep, HRV, Resting HR, and Steps** — the value
  is labelled on every point (no hover needed), arranged 2×2, each chart
  collapsible.
- **Fitness Age** and **VO₂ max** summary tiles with the current value, an
  up/down trend arrow vs. the last recorded change, and how long ago it was
  measured. Computed during sync and cached, so the page loads instantly and
  works even when Garmin is unreachable.

**Type-aware workout pages**
- **Strength** sessions: per-exercise breakdown with weight × reps for every
  set and total volume — straight from the watch. Optional manual correction
  for the rare misdetected set.
- **Cardio** sessions (soccer, running, …): distance, speed, elevation, laps,
  intensity minutes, training effect — showing only the metrics that are
  meaningful for that activity type (e.g. running cadence/pace are hidden for
  stop-start sports).
- **HR-zone breakdown** (time in each zone) on every workout, plus ⓘ hover
  tooltips explaining the non-obvious metrics.
- Shows the watch's own values — only unit conversions, never invented metrics.

## Status

- **Phase 1 ✅ — Foundation + Dashboard:** Garmin sync (token cache + MFA),
  SQLite cache, trend dashboard, fitness-age/VO₂ tiles, and type-aware
  workout-detail views (strength sets, cardio stats, HR zones).
- **Phase 2 ✅ — Metrics engine:** training load (Banister/Edwards TRIMP), EWMA
  ACWR, readiness (HRV/RHR/sleep z-scores vs a personal baseline), sleep debt,
  and strength progression (volume load + Epley estimated-1RM). Every formula is
  science-based and cited — see [`docs/METRICS.md`](docs/METRICS.md).
- **Phase 3 ✅ — Coach (LLM):** daily suggestions + chat, swappable Ollama/Claude.
- **Phase 4 ✅ — Cloud Security:** HTTP Basic Authentication locking down all routes and data, preparing the app for safe deployment to the public internet (e.g. Oracle Cloud Free Tier).
- **Phase 5 🚧 — Lifestyle Integration (Planned):** sync to Google/Apple Calendar via `.ics` to suggest workout times, plus dynamic nutrition/food/vitamin suggestions based on goals and current recovery status.

## Setup

```bash
cd garmincoach
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                          # then edit GARMIN_EMAIL
```

Edit `.env` and set `GARMIN_EMAIL`. Leave `LLM_PROVIDER=ollama` (default, free).

```bash
.venv/bin/python app.py          # Windows: .venv\Scripts\python app.py
```

Open http://localhost:8000 (or http://<this-machine-ip>:8000 from your iPhone on
the same wifi).

## Access Away From Home (Tailscale)
Install [Tailscale](https://tailscale.com/) on the machine running this app, and on your phone.
The app automatically binds to `0.0.0.0`, meaning you can open `http://<your-tailscale-ip>:8000` from your phone anywhere in the world to chat with the Coach or check your readiness!

## First login (one time)

1. Click **Connect your Garmin account**.
2. Enter your Garmin password. If your account uses 2FA, also paste the code.
   - Your password is used once to obtain an auth token, then **discarded**.
   - The token is cached at `~/.garminconnect` and reused on every later run, so
     you won't log in again (this also avoids Garmin's login rate-limits).
3. An initial backfill (last `INITIAL_BACKFILL_DAYS`, default 90) runs in the
   background. Refresh the dashboard as data lands.

Thereafter, data auto-syncs at the hours in `AUTO_SYNC_HOURS` (default 7am/7pm),
plus a **Sync now** button on the dashboard.

## Switching the coach to Claude (Phase 3, optional, paid)

In `.env`: set `LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY=...`. That's the only
change — same app, sharper advice, ~$0.002 per coaching call. Default stays free
(local Ollama).

## Notes & limitations

- The unofficial Garmin login flow breaks ~1–3×/year when Garmin changes their
  SSO. If sync starts failing on auth, `pip install -U garminconnect`.
- Per-set strength data only appears if the watch logged it (you tracked reps and
  entered weight on-device during a Strength activity).
- This is personal single-user use of your own data.
