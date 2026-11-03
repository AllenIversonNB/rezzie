"""Runtime configuration, read from environment variables (Fly.io secrets in prod).

Nothing secret is ever committed: the repo is public. See .env.example.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- App access -------------------------------------------------------
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "./data"))
    # Secure cookies need HTTPS. Fly serves HTTPS; set COOKIE_SECURE=0 for local http dev.
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))

    # --- Resy -------------------------------------------------------------
    resy_email: str = field(default_factory=lambda: os.getenv("RESY_EMAIL", ""))
    resy_password: str = field(default_factory=lambda: os.getenv("RESY_PASSWORD", ""))
    # Public web-app key baked into resy.com's JS bundle. Override if Resy rotates it.
    resy_api_key: str = field(
        default_factory=lambda: os.getenv("RESY_API_KEY", "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5")
    )

    # --- Network ----------------------------------------------------------
    # Optional outbound proxy for platform traffic, e.g. a Tailscale exit node at
    # home or a residential proxy: http://user:pass@host:port. Datacenter IPs are
    # treated with more suspicion by Resy's WAF than a home connection.
    outbound_proxy: str = field(default_factory=lambda: os.getenv("OUTBOUND_PROXY", ""))
    # curl_cffi browser fingerprint to present. Keep it stable — a logged-in
    # account whose browser changes every request looks far more bot-like than
    # one that never changes.
    impersonate: str = field(default_factory=lambda: os.getenv("IMPERSONATE", "chrome"))

    # --- Notifications ----------------------------------------------------
    ntfy_topic: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC", ""))
    ntfy_server: str = field(default_factory=lambda: os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    ntfy_token: str = field(default_factory=lambda: os.getenv("NTFY_TOKEN", ""))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", ""))

    # --- Safety / pacing (conservative defaults) ---------------------------
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))
    # Minimum seconds between ANY two calls to one platform (across all monitors).
    min_gap_seconds: int = field(default_factory=lambda: _int("MIN_GAP_SECONDS", 6))
    # Hard daily ceiling on calls per platform. A human browsing heavily might
    # make a few hundred; we stay well inside "heavy user" territory.
    daily_call_budget: int = field(default_factory=lambda: _int("DAILY_CALL_BUDGET", 900))
    # Floor for any monitor's polling interval.
    min_poll_seconds: int = field(default_factory=lambda: _int("MIN_POLL_SECONDS", 60))
    max_active_monitors: int = field(default_factory=lambda: _int("MAX_ACTIVE_MONITORS", 8))
    # Overnight (in APP_TIMEZONE) monitors slow down 4x — people sleep.
    quiet_hours: str = field(default_factory=lambda: os.getenv("QUIET_HOURS", "01-07"))
    app_timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "America/New_York"))
    # Snipe burst: how long to keep retrying after release and how fast.
    snipe_window_seconds: int = field(default_factory=lambda: _int("SNIPE_WINDOW_SECONDS", 45))
    snipe_min_interval_ms: int = field(default_factory=lambda: _int("SNIPE_MIN_INTERVAL_MS", 1200))
    snipe_max_interval_ms: int = field(default_factory=lambda: _int("SNIPE_MAX_INTERVAL_MS", 2200))
    max_password_logins_per_day: int = field(default_factory=lambda: _int("MAX_LOGINS_PER_DAY", 3))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "rezzie.db")

    def quiet_range(self) -> tuple[int, int] | None:
        try:
            a, b = self.quiet_hours.split("-")
            return int(a), int(b)
        except Exception:
            return None


settings = Settings()
