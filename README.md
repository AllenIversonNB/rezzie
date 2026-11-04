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
| **Snipe the drop** (fire the instant reservations open) | ✅ auto-books in about a second | ✅ auto-books no-card tables; card-required ones get a one-tap link | ⏰ 60-second warning, then a one-tap link at release |
| **Watch for cancellations** | ✅ auto-book or notify | ✅ auto-book (no-card) or notify | ➖ use OpenTable's built-in *Notify me* instead |
| **Login needed** | Your Resy account | None: guest checkout with your saved name, email and phone | None (we never contact OpenTable) |

**SevenRooms and DoorDash:** DoorDash bought SevenRooms in 2025, and the DoorDash app's Reservations tab sells the same SevenRooms tables. Rezzie books those tables directly through SevenRooms' own reservation widget instead of the DoorDash app. That way your DoorDash account is never involved, and only DashPass-exclusive tables are out of reach (use DoorDash's in-app Notify for those).

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
| SevenRooms | Not in the plan | Public widget (no login): `ng/widget/range` availability, then auto-booking via a 5-minute hold (`/api-yoa/dining/hold/add`) and the guest booking form. Only `type: book` times count as tables, and tables flagged `require_credit_card` are never automated. |
| Notifications | iOS local notifications | ntfy push with "Open" action buttons |

## Account safety

These are the main ways an account gets flagged, and what Rezzie does about each:

1. **Too many requests.**
   - At least 6–10 s (randomized) between any two background calls to a platform.
   - A hard **daily call budget** per platform (900 by default; change it in **Settings → Account safety**). 100 calls a day are held back so snipes always have headroom; monitors share the rest.
   - **Budget pacing:** monitors space their checks so the day's budget lasts until midnight (New York time), so they never go dark in the evening. More monitors means each one checks less often. Monitors watching several dates cost about double.
   - **Hot windows:** the budget is spent where cancellations actually happen. Each monitor checks fastest in the ~2.5 h before the restaurant's cancellation-fee deadline (Resy's reported cutoff when known, else 24 h, plus a smaller 48 h bump), on the day itself 9 am–noon and 3–6 pm, and somewhat faster within 48 h. Dates more than a week away and overnight hours get far fewer checks (see `app/hotness.py`).
   - **Learning:** every table that appears between two checks is logged, and **Settings → When tables opened up** charts them by time of day and by how long before the reservation, so the windows can be tuned from real data.
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

## Deploy from a Windows PC (about 20 minutes, one time)

Your PC is only used to **send setup commands** to Fly.io. Rezzie itself runs on a small Linux server that Fly rents you (about $2–5/month), and you use it from your phone's browser. After setup, the PC can be off.

All commands below go in **Windows PowerShell**: press the Windows key, type `PowerShell`, and press Enter. Paste one command at a time and press Enter. (On a Mac, the same `fly` commands work in Terminal.)

1. **Create a Fly account** at https://fly.io/app/sign-up and add a credit card under Billing.
2. **Install Git and the Fly tool.**
   ```powershell
   winget install --id Git.Git -e
   ```
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   ```
   Then **close PowerShell and open a new one** so Windows picks up both tools.
3. **Log in to Fly.** A browser window opens; confirm there.
   ```powershell
   fly auth login
   ```
4. **Download the code** and go into its folder.
   ```powershell
   git clone -b claude/affectionate-ramanujan-514j2r https://github.com/AllenIversonNB/rezzie.git
   ```
   ```powershell
   cd rezzie
   ```
5. **Create the app.** Use the name `rezzie-jf`; the auto-update workflow deploys to the app named in `fly.toml`. If Fly says the name is taken, pick another and change the `app = "…"` line in `fly.toml` on GitHub to match. If it asks to tweak settings, answer `N`.
   ```powershell
   fly launch --no-deploy --copy-config --name rezzie-jf --region ewr
   ```
6. **Create the 1 GB database disk.** If it warns about having a single volume, answer `y`.
   ```powershell
   fly volumes create rezzie_data --region ewr --size 1
   ```
7. **Generate a random notification topic and secret key.** Write down the topic it prints.
   ```powershell
   $r=[Security.Cryptography.RandomNumberGenerator]::Create(); $b=New-Object byte[] 8; $r.GetBytes($b); $TOPIC="rezzie-"+(($b|%{$_.ToString("x2")}) -join ""); $k=New-Object byte[] 48; $r.GetBytes($k); $SECRET=[Convert]::ToBase64String($k); $TOPIC
   ```
8. **Store your private settings on Fly.** Change the password (and `rezzie-jf` if you used a different name). `DRY_RUN=1` means nothing is really booked until you turn it off in step 12.
   ```powershell
   fly secrets set APP_PASSWORD="MyLongPassword123" SECRET_KEY="$SECRET" NTFY_TOPIC="$TOPIC" PUBLIC_URL=https://rezzie-jf.fly.dev DRY_RUN=1
   ```
9. **Build and start it on Fly's servers.** No Docker is needed on your PC. Takes about 3 minutes.
   ```powershell
   fly deploy --ha=false --remote-only
   ```
10. **On your iPhone:**
    1. Open `https://rezzie-jf.fly.dev` in Safari, then **Share → Add to Home Screen → Add**.
    2. Open Rezzie from the Home Screen and enter your password.
    3. **Settings → Resy account**: enter your Resy login and tap **Connect Resy**.
    4. Install **ntfy** from the App Store, tap **+**, subscribe to your topic, and allow notifications. Then tap **Send test** in Rezzie's Settings.
11. **Test** with a "Watch cancellations" request and check its activity log.
12. **Go live** (real bookings) from PowerShell in the `rezzie` folder:
    ```powershell
    fly secrets unset DRY_RUN
    ```

13. **Turn on automatic updates (one time).**
    1. In PowerShell, in the `rezzie` folder, run the command below and copy the long token it prints (it starts with `FlyV1`).
       ```powershell
       fly tokens create deploy
       ```
    2. On GitHub, open the rezzie repo, then **Settings → Secrets and variables → Actions → New repository secret**.
    3. Name: `FLY_API_TOKEN`. Secret: paste the token. Click **Add secret**.

### How automatic updates work

You never run update commands. Whenever a change is pushed to this repo (on `main` or a `claude/…` branch), GitHub Actions:

1. runs the test suite, and stops if anything fails, so the running app is left untouched;
2. waits until no snipe is running or due in the next 15 minutes, so a restart never interrupts a drop;
3. deploys the new version to Fly as a single machine.

Restarts take under a minute. Your data, logins and secrets live on Fly and are kept. You can watch each update under the repo's **Actions** tab, and you can re-run the latest deploy by hand there (**Deploy to Fly.io → Run workflow**).

## Using it

- **Snipe a drop:**
  1. Go to New → Resy and search for the restaurant (or paste its resy.com link).
  2. Add the date(s), the time window and the party size.
  3. Tap **Suggest from Resy's booking window**. This reads the venue's booking window in days; set the release *time of day* yourself (often 9:00, 10:00 or midnight; check the venue's page).
  4. Tap **Schedule snipe**.

  Five minutes before the release it checks your login and card. At T+50 ms it starts.
- **Watch for cancellations:** New → choose *Watch cancellations*. You can add up to 21 dates.
- **SevenRooms:** paste the venue's `sevenrooms.com/reservations/<venue>` (or `/explore/<venue>/…`) link. To auto-book, first fill in **Settings → Your booking details** (name, email, mobile). Confirmations come from the restaurant by email, and you cancel using the link in that email.
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
  hotness.py         when cancellations are likely; drives monitor pacing
  safety.py          Governor: pacing, daily budgets, backoff, pauses
  matching.py        slot filtering and ranking
  notify.py          ntfy push
  security.py        app login, encrypted credential storage
  db.py              SQLite (on the Fly volume)
  platforms/
    resy.py          Resy client (auth, refresh, search, calendar, find, details, book, cancel)
    sevenrooms.py    public widget availability + guest hold/book
    opentable.py     deep links only, no network calls
    http.py          curl_cffi Chrome-fingerprint transport
web/                 mobile web app (vanilla JS PWA)
tests/               pytest suite with a fake transport (no real network)
```

## Known limits

- These are unofficial, undocumented APIs. Resy can change endpoint shapes, rotate its key or tighten its WAF without notice. Failures show up in the activity log and as push alerts. `RESY_API_KEY` lets you swap the key without a code change.
- Venues that require a deposit or card confirmation stop at "Found — finish in Resy/SevenRooms", and you get a link. If SevenRooms ever shows a captcha at booking, Rezzie stops and sends the link too; it never tries to solve one.
- SevenRooms' booking requests come from public captures of the widget, not from live testing by me. The first real SevenRooms auto-booking is the live test, so check the activity log and your email.
- Rezzie can't know a venue's release *time of day* automatically; you set it per request.
- It runs as a single machine by design. Don't scale it to more than one.
