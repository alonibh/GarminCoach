# GarminCoach Roadmap 🗺️

Feature backlog for the GarminCoach app. Priorities may shift as we go.

---

## 🔴 Phase 1 — Security & Cloud Essentials

- [x] **Garmin login on the cloud server**
  - The cloud instance needs its own authenticated Garmin session so it can auto-sync fresh data every day without relying on the local copy.

- [x] **HTTPS (SSL certificate)**
  - Free Let's Encrypt certificate installed. Auto-renews. Available at `https://garmincoach.duckdns.org`.

---

## 🟡 Phase 2 — Polish & Convenience

- [x] **Custom domain name**
  - Live at `https://garmincoach.duckdns.org` via DuckDNS (free).

- [x] **Auto-deploy from GitHub**
  - Push code to GitHub → server automatically pulls and restarts using GitHub Actions.

- [x] **Install as phone app (PWA)**
  - App is now installable via 'Add to Home Screen' with custom icons and standalone mode.

---

## 🟢 Phase 3 — Smart Features


- [ ] **Nutrition recommendations**
  - Post-workout meal suggestions based on workout type, intensity, and goals.
  - Daily macro targets (protein/carbs/fat) adjusted to training load and rest days.
  - Integration with Garmin's calorie burn data to suggest caloric intake.


- [x] **Training plan calendar view**
  - View past workouts in a calendar grid.
  - Visual week/month calendar showing past workouts and planned sessions.

- [x] **Push notifications (Telegram)**
  - AI proactive push notifications, recovery alerts, and interactive workout scheduling delivered via Telegram bot integration.

---

## 💡 Ideas Parking Lot
_Drop any future ideas here:_

- **Smart Activity Recommendations:** Recommend specific activities based on user preferences and goals.
- **Dynamic Workout Type Suggestions:** Suggest different types of workouts (Yoga, Swim, Cardio, etc.) based on fatigue status, recovery needs, and overall goals, instead of just strength training.
- Strava integration
- Export reports as PDF
- Multi-user support
- Heart rate zone training plans
- Sleep optimization tips based on HRV trends
