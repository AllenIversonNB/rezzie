# Rezzie

A personal restaurant-reservation sniper you use from your phone's browser.

Rezzie updates the **Reservation Sniper** plan, which was a SwiftUI iOS app with a cloud helper. It keeps the same features but changes the shape:

- **Cloud back end**: a small always-on Python service on Fly.io. It holds your logins, keeps exact time and does the sniping, even with your phone off.
- **Mobile website front end**: open it in Safari, then use **Share → Add to Home Screen**. It runs like an app, with no App Store and no Xcode.
- **Alerts**: push notifications through the free **ntfy** app.

> ⚠️ **Personal, experimental use only.** Resy, OpenTable and SevenRooms terms of service all prohibit automated booking. Rezzie is built to behave like one careful human (see [Account safety](#account-safety)), but there is always some risk to the accounts you connect. Don't resell reservations. Cancel the ones you won't use; no-shows hurt restaurants and can get you banned.

---

## What it does

| | Resy | SevenRooms | OpenTable |
|---|---|---|---|
| **Snipe the drop** (fire the instant reservations open) | ✅ auto-books in about a second | ✅ detects the drop and pushes you a one-tap link | ⏰ 60-second warning, then a one-tap link at release |
| **Watch for cancellations** | ✅ auto-book or notify | ✅ notify with link | ➖ use OpenTable's built-in *Notify me* instead |
| **Login needed** | Your Resy account | None (public widget) | None (we never contact OpenTable) |

Every request has:
- One or more dates
- An earliest / ideal / latest time window
- Party size
- Seating types to skip (for example "bar" or "patio")
- Auto-book on or off
- An optional "if the drop is missed, keep watching for cancellations" fallback

**Screens:**
- **Requests**: live countdowns and watch status.
- **New**: search a venue (or paste its link), configure, schedule.
- **Request detail**: a full activity log, **Check now**, **Stop**, **Edit**, **Cancel reservation** on Resy, and **Open booking page**.
- **History**
- **Settings**: Resy account, notifications, safety status and the activity log.

## What changed since the original plan (refreshed Nov 2026)

| Area | Original plan | Now |
|---|---|---|
| Front end | SwiftUI iOS app | Mobile web app you add to your Home Screen (`/web`, no build step) |
| Timing | iOS foreground timers and background tasks (unreliable) | Server-side asyncio timer that fires at T+50 ms; your phone can be asleep |
| Resy API key | `VbWk…Xnav` (wrong) | `VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5`, overridable with `RESY_API_KEY` |
| Resy login | Password every time | Password once, then Resy's ~90-day `production_refresh_token` via `/3/auth/refresh`. JWT expiry is tracked, and password logins are capped at 3 per day. |
| Resy search | `GET /3/venuesearch/search?query=` | `POST /3/venuesearch/search` with a `struct_data` form field. Pasting a resy.com link also works. |
| `/4/find`, `/3/details` | GET only | GET (what the web app does), falling back to POST JSON if Resy changes |
| Cancellation polling | `/4/find` for every date, every 10–30 s | `/4/venue/calendar` first: one call checks every date, and `/4/find` runs only on dates that show availability. 60 s minimum. |
| User-Agent | "Rotate User-Agent strings" | **Never rotate.** Rotating the UA on one logged-in account is a classic bot tell. One stable, real Chrome TLS fingerprint (curl_cffi). |
| OpenTable | GraphQL `/dapi/fe/gql` scraping | **Never contacted.** Akamai Bot Manager now blocks every non-browser client, even TLS-impersonating ones and headless Chrome, and the persisted-query hashes rotate. You get reminders and deep links instead. |
| SevenRooms | Not in the plan | Public widget availability endpoint (no login) with deep-link alerts |
| Notifications | iOS local notifications | ntfy push with "Open" action buttons |

## Account safety

These are the main ways an account gets flagged, and what Rezzie does about each:

1. **Too many requests.**
   - At least 6–10 s (randomized) between any two background calls to a platform.
   - A hard **daily call budget** per platform (900 by default). Monitors can use only 75% of it, so a scheduled snipe always has headroom.
   - Monitors poll every **2 min by default** (60 s minimum), with ±25% jitter.
   - Overnight (1–7 am) polling slows 4×. It speeds up in the last 48 hours, when cancellations actually happen.
   - At most 8 active monitors at once.
2. **Bursts.**
   - A snipe retries every 1.2–2.2 s (random) for a short window (45 s by default), about 20 checks, then stops or falls back to the gentle monitor.
   - Monitors are held off the platform during a snipe.
   - Only **one booking is in flight at a time**, ever.
3. **Ignoring pushback.**
   - **429** → stop all calls to that platform for 5 min, doubling on each repeat, up to 3 h.
   - **403, a WAF/challenge page, or repeated bare 500s** (Resy's firewall often answers with a 500) → stop for 30 min, doubling, up to 12 h.
   - Each time this happens you get one push notification. Nothing retries through it. You can override it in Settings, but don't.
4. **Looking like a script.**
   - The TLS fingerprint and headers are a real Chrome build's, and the same on every request.
   - One long-lived session, like one browser tab.
   - A short, human-like pause before clicking Book.
   - No parallel booking attempts.
5. **Repeated logins.**
   - Resy logs in with your password at most 3 times a day.
   - Otherwise it refreshes the token silently, and does so a few minutes *before* a snipe, never during one.
6. **Datacenter IP (optional fix).**
   - Resy's WAF trusts residential IPs more than cloud ones.
   - If you start seeing pauses, set `OUTBOUND_PROXY`: for example, a Tailscale exit node on a computer at home, or a residential proxy. All platform traffic then comes from there.

Other things that help:
- Turn on **Resy Notify** and **OpenTable Notify me** in their own apps for the restaurants you care about. They're sanctioned, and they stack with Rezzie.
- Use `DRY_RUN=1` while you're trying things out. It does everything except the final `/3/book`.

---

## Deploy (about 15 minutes, one time)

You need a Fly.io account (it asks for a card; this app costs about $2–5/month) and a computer with a terminal for setup. After that, everything happens from your phone.

1. **Install flyctl and log in.**
   ```bash
   brew install flyctl
   ```
   ```bash
   fly auth login
   ```
2. **Create the app and its storage**, from this repo's folder. Pick a unique name and keep the existing `fly.toml` when asked.
   ```bash
   fly launch --no-deploy --copy-config --name rezzie-yourname --region ewr
   ```
   ```bash
   fly volumes create rezzie_data --region ewr --size 1
   ```
3. **Set secrets.** Pick your own password. `NTFY_TOPIC` should be long and random, because anyone who knows it can read your alerts.
   ```bash
   fly secrets set APP_PASSWORD='a-long-password' SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" NTFY_TOPIC="rezzie-$(python3 -c 'import secrets;print(secrets.token_hex(8))')" PUBLIC_URL=https://rezzie-yourname.fly.dev
   ```
   You can also put your Resy login here (`RESY_EMAIL` / `RESY_PASSWORD`) instead of entering it in the app.
4. **Deploy as exactly one machine.** The scheduler and database live in one process.
   ```bash
   fly deploy --ha=false
   ```
5. **On your iPhone:**
   1. Open `https://rezzie-yourname.fly.dev` in Safari, then **Share → Add to Home Screen**.
   2. Open it and enter your `APP_PASSWORD`.
   3. **Settings → Resy account**: connect. You'll see "Card on file ✓" if Resy has a payment method.
   4. Install **ntfy** from the App Store, tap **+** and subscribe to your `NTFY_TOPIC`. Then tap **Send test** in Settings.

**Optional auto-deploy:** run `fly tokens create deploy` and add the result as a GitHub repo secret named `FLY_API_TOKEN`. Every push to `main` then runs the tests and deploys.

## Using it

- **Snipe a drop:**
  1. Go to New → Resy and search for the restaurant (or paste its resy.com link).
  2. Add the date(s), the time window and the party size.
  3. Tap **Suggest from Resy's booking window**. This reads the venue's booking window in days; set the release *time of day* yourself (often 9:00, 10:00 or midnight; check the venue's page).
  4. Tap **Schedule snipe**.

  Five minutes before the release it checks your login and card. At T+50 ms it starts.
- **Watch for cancellations:** New → choose *Watch cancellations*. You can add up to 21 dates.
- **SevenRooms:** paste the venue's `sevenrooms.com/reservations/<venue>` (or `/explore/<venue>/…`) link.
- **OpenTable:** paste the `opentable.com/r/<restaurant>` link and set the release time. You'll get a push 60 s before, and a "GO" push at release that opens the right page and party size.

## Configuration

All settings are environment variables (Fly secrets); see [`.env.example`](.env.example). Key ones:

| Var | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | — (required) | Unlocks the website |
| `SECRET_KEY` | auto-generated | Signs sessions and encrypts stored credentials |
| `NTFY_TOPIC` / `NTFY_SERVER` / `NTFY_TOKEN` | — / ntfy.sh / — | Push notifications |
| `RESY_EMAIL` / `RESY_PASSWORD` | — | Alternative to entering them in Settings |
| `OUTBOUND_PROXY` | — | Route platform traffic through a home or residential IP |
| `DRY_RUN` | 0 | Simulate bookings |
| `DAILY_CALL_BUDGET` | 900 | Per-platform daily cap |
| `MIN_POLL_SECONDS` | 60 | Floor for monitor intervals |
| `MIN_GAP_SECONDS` | 6 | Minimum spacing between background calls |
| `SNIPE_WINDOW_SECONDS` | 45 | Default retry window after release |
| `QUIET_HOURS` / `APP_TIMEZONE` | 01-07 / America/New_York | Overnight slow-down |

## Local development

```bash
python3 -m venv .venv
```
```bash
. .venv/bin/activate
```
```bash
pip install -r requirements-dev.txt
```
```bash
python -m pytest -q
```
```bash
APP_PASSWORD=dev COOKIE_SECURE=0 DRY_RUN=1 uvicorn app.main:app --reload --port 8000
```

Then open http://localhost:8000.

## Layout

```
app/
  main.py            FastAPI routes + serves /web
  engine.py          snipe / monitor / reminder scheduler (asyncio, in-process)
  safety.py          Governor: pacing, daily budgets, backoff, pauses
  matching.py        slot filtering and ranking
  notify.py          ntfy push
  security.py        app login, encrypted credential storage
  db.py              SQLite (on the Fly volume)
  platforms/
    resy.py          Resy client (auth, refresh, search, calendar, find, details, book, cancel)
    sevenrooms.py    public widget availability
    opentable.py     deep links only, no network calls
    http.py          curl_cffi Chrome-fingerprint transport
web/                 mobile web app (vanilla JS PWA)
tests/               pytest suite with a fake transport (no real network)
```

## Known limits

- These are unofficial, undocumented APIs. Resy can change endpoint shapes, rotate its key or tighten its WAF without notice. Failures show up in the activity log and as push alerts. `RESY_API_KEY` lets you swap the key without a code change.
- Venues that require a deposit or card confirmation may stop at "Found — finish in Resy", and you get a link.
- Rezzie can't know a venue's release *time of day* automatically; you set it per request.
- It runs as a single machine by design. Don't scale it to more than one.
