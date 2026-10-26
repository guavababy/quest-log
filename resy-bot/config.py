"""Load and check the settings file (config.toml)."""
from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ConfigError(Exception):
    pass


@dataclass
class Restaurant:
    name: str
    venue: str                     # venue id, url slug, or resy.com URL
    city: str
    timezone: ZoneInfo
    earliest: dt.time
    latest: dt.time
    preferred: dt.time | None
    seating: list[str] = field(default_factory=list)   # allowed table types, best first
    release_days_ahead: int | None = None
    release_time: dt.time | None = None
    venue_id: int | None = None    # looked up at startup


@dataclass
class Config:
    email: str
    party_size: int
    dates: list[dt.date]           # in priority order
    restaurants: list[Restaurant]  # in priority order
    stop_after_first: bool
    dry_run: bool
    check_every_seconds: int
    release_lead_seconds: int
    release_window_minutes: int
    fast_check_seconds: float
    alerts: dict


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config.example.toml to config.toml and edit it.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} isn't valid TOML: {e}") from None

    account = raw.get("account", {})
    when = raw.get("when", {})
    bot = raw.get("bot", {})

    email = account.get("email")
    if not email:
        raise ConfigError("Set your Resy login email under [account].")

    party_size = _int(raw.get("party_size"), "party_size", minimum=1)
    tz_default = _tz(raw.get("timezone", "America/New_York"))
    earliest = _time(when.get("earliest", "17:00"), "when.earliest")
    latest = _time(when.get("latest", "22:00"), "when.latest")
    preferred = _time(when["preferred"], "when.preferred") if "preferred" in when else None

    restaurants = []
    for i, r in enumerate(raw.get("restaurants", []), 1):
        where = f"restaurant #{i}"
        if not r.get("venue"):
            raise ConfigError(f"{where} needs a venue (its resy.com URL, slug, or id).")
        days_ahead = r.get("release_days_ahead")
        release_time = r.get("release_time")
        if (days_ahead is None) != (release_time is None):
            raise ConfigError(f"{where}: set both release_days_ahead and release_time, or neither.")
        restaurants.append(Restaurant(
            name=r.get("name") or str(r["venue"]),
            venue=str(r["venue"]),
            city=r.get("city", "ny"),
            timezone=_tz(r["timezone"]) if "timezone" in r else tz_default,
            earliest=_time(r["earliest"], f"{where} earliest") if "earliest" in r else earliest,
            latest=_time(r["latest"], f"{where} latest") if "latest" in r else latest,
            preferred=_time(r["preferred"], f"{where} preferred") if "preferred" in r else preferred,
            seating=[s.lower() for s in r.get("seating", [])],
            release_days_ahead=_int(days_ahead, f"{where} release_days_ahead", minimum=0)
            if days_ahead is not None else None,
            release_time=_time(release_time, f"{where} release_time")
            if release_time is not None else None,
        ))
    if not restaurants:
        raise ConfigError("Add at least one [[restaurants]] entry.")
    for r in restaurants:
        if r.earliest > r.latest:
            raise ConfigError(f"{r.name}: earliest time is after latest time.")

    dates = expand_dates(when)
    if not dates:
        raise ConfigError("No dates to search. Set when.dates and/or when.start/when.end.")

    return Config(
        email=email,
        party_size=party_size,
        dates=dates,
        restaurants=restaurants,
        stop_after_first=bool(bot.get("stop_after_first", True)),
        dry_run=bool(bot.get("dry_run", True)),
        check_every_seconds=_int(bot.get("check_every_seconds", 120), "check_every_seconds", minimum=30),
        release_lead_seconds=_int(bot.get("release_lead_seconds", 30), "release_lead_seconds", minimum=0),
        release_window_minutes=_int(bot.get("release_window_minutes", 3), "release_window_minutes", minimum=1),
        fast_check_seconds=float(bot.get("fast_check_seconds", 2)),
        alerts=raw.get("alerts", {"method": "none"}),
    )


def expand_dates(when: dict) -> list[dt.date]:
    """Specific dates first (in the order listed), then every date in start..end that
    falls on one of the chosen weekdays."""
    dates = [_date(d, "when.dates") for d in when.get("dates", [])]
    if "start" in when or "end" in when:
        if not ("start" in when and "end" in when):
            raise ConfigError("Set both when.start and when.end, or neither.")
        start, end = _date(when["start"], "when.start"), _date(when["end"], "when.end")
        days = [d.lower()[:3] for d in when.get("weekdays", WEEKDAYS)]
        bad = [d for d in days if d not in WEEKDAYS]
        if bad:
            raise ConfigError(f"Unknown weekday(s) {bad}; use {WEEKDAYS}.")
        d = start
        while d <= end:
            if WEEKDAYS[d.weekday()] in days:
                dates.append(d)
            d += dt.timedelta(days=1)
    return list(dict.fromkeys(dates))


def _int(value, name, minimum):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{name} must be a whole number of at least {minimum}.")
    return value


def _time(value, name) -> dt.time:
    try:
        return dt.time.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{name} must be a 24-hour time like \"19:30\", got {value!r}.") from None


def _date(value, name) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{name} must be a date like \"2026-11-14\", got {value!r}.") from None


def _tz(name) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"Unknown timezone {name!r}; use e.g. \"America/New_York\".") from None
