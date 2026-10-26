"""The monitoring loop: check availability, book the best slot, alert, repeat."""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time
from pathlib import Path

import requests

import planner
from alerts import Alerts
from config import Config
from resy_api import ResyClient, ResyError

log = logging.getLogger("resybot")

MAX_BACKOFF = 30 * 60      # seconds
GIVE_UP_AFTER_BLOCKS = 6   # checks in a row where Resy refused every request


class AlreadyBooked(Exception):
    pass


class Bot:
    def __init__(self, cfg: Config, client: ResyClient, alerts: Alerts, state_dir: Path,
                 now=lambda: dt.datetime.now(dt.timezone.utc), sleep=time.sleep):
        self.cfg = cfg
        self.client = client
        self.alerts = alerts
        self.bookings_file = state_dir / "booked.json"
        self.now = now
        self.sleep = sleep
        self.booked_days: set[dt.date] = set()
        self.done = False
        self.backoff = 0
        self.blocked_checks = 0

    # ------------------------------------------------------------------ setup
    def start(self):
        prior = self._read_bookings()
        if prior and self.cfg.stop_after_first and not self.cfg.dry_run:
            b = prior[-1]
            raise AlreadyBooked(
                f"Already booked {b['restaurant']} on {b['day']} at {b['time']}. "
                f"Delete {self.bookings_file.name} to start looking again.")
        self.booked_days |= {dt.date.fromisoformat(b["day"]) for b in prior}

        for r in self.cfg.restaurants:
            v = self.client.venue(r.venue, r.city)
            r.venue_id = v["id"]
            if v["name"] and r.name == r.venue:
                r.name = v["name"]
            log.info("Watching %s (venue %s)", r.name, r.venue_id)

        # Don't book a night you already have a table at one of these restaurants.
        ours = {r.venue_id for r in self.cfg.restaurants}
        try:
            for res in self.client.upcoming_reservations():
                if res["venue_id"] in ours and res["day"]:
                    day = dt.date.fromisoformat(res["day"])
                    if day in self.cfg.dates:
                        log.info("You already have a reservation on %s; skipping that date.", day)
                        self.booked_days.add(day)
        except (ResyError, requests.RequestException, ValueError) as e:
            log.warning("Couldn't read your upcoming reservations (%s); continuing.", e)

    # ------------------------------------------------------------------ loop
    def run(self, once: bool = False):
        while True:
            pause = self.check_once()
            if pause is None or once:
                return
            log.debug("Sleeping %.0fs", pause)
            self.sleep(pause)

    def check_once(self) -> float | None:
        """One round of checks. Returns seconds to wait before the next round, or None
        when the bot should stop."""
        now = self.now()
        fast, normal, next_release = self._plan(now)
        if not fast and not normal and next_release is None:
            log.info("Nothing left to look for (all dates passed or booked).")
            return None

        self._calls = self._failures = self._blocks = 0
        found = self._check_fast(fast) if fast else self._check_normal(normal)

        if self._calls and self._failures == self._calls and self._blocks:
            return self._back_off()
        self.backoff = self.blocked_checks = 0

        if found:
            self._book_best(found)
            if self.done:
                return None

        if fast:
            return self.cfg.fast_check_seconds
        pause = self.cfg.check_every_seconds * random.uniform(0.75, 1.25)
        if next_release is not None:
            pause = min(pause, max(1.0, (next_release - self.now()).total_seconds()))
        return pause

    def _plan(self, now):
        """Split (restaurant, date) pairs into: checking fast (release moment), checking
        at the normal pace, and the next time a release window opens."""
        lead = dt.timedelta(seconds=self.cfg.release_lead_seconds)
        window = dt.timedelta(minutes=self.cfg.release_window_minutes)
        fast, normal, next_release = [], [], None
        for ri, r in enumerate(self.cfg.restaurants):
            today = now.astimezone(r.timezone).date()
            for di, day in enumerate(self.cfg.dates):
                if day < today or day in self.booked_days:
                    continue
                p = planner.phase(r, day, now, lead, window)
                if p == planner.WAIT:
                    opens = planner.release_moment(r, day) - lead
                    next_release = opens if next_release is None else min(next_release, opens)
                elif p == planner.FAST:
                    fast.append((ri, r, di, day))
                else:
                    normal.append((ri, r, di, day))
        return fast, normal, next_release

    def _check_fast(self, targets) -> list[planner.Candidate]:
        found = []
        for ri, r, di, day in targets:
            slots = self._call(self.client.find, r.venue_id, day.isoformat(), self.cfg.party_size)
            if slots:
                found += planner.candidates(r, ri, day, di, slots)
        return found

    def _check_normal(self, targets) -> list[planner.Candidate]:
        """One calendar call per restaurant to see which dates have tables, then look at
        the times only on those dates."""
        by_restaurant: dict[int, list] = {}
        for t in targets:
            by_restaurant.setdefault(t[0], []).append(t)
        found = []
        for group in by_restaurant.values():
            r = group[0][1]
            days = [t[3] for t in group]
            open_days = self._call(self.client.calendar, r.venue_id, self.cfg.party_size,
                                   min(days).isoformat(), max(days).isoformat())
            for ri, _, di, day in group:
                if open_days is not None and day.isoformat() not in open_days:
                    continue
                slots = self._call(self.client.find, r.venue_id, day.isoformat(),
                                   self.cfg.party_size)
                if slots:
                    found += planner.candidates(r, ri, day, di, slots)
        if not found:
            log.info("No matching tables yet.")
        return found

    def _call(self, fn, *args):
        """Call the API, counting failures instead of raising."""
        self._calls += 1
        try:
            return fn(*args)
        except ResyError as e:
            self._failures += 1
            self._blocks += e.blocked
            log.warning("%s failed: %s", fn.__name__, e)
        except requests.RequestException as e:
            self._failures += 1
            log.warning("%s failed: %s", fn.__name__, e)
        return None

    def _back_off(self) -> float | None:
        self.blocked_checks += 1
        if self.blocked_checks >= GIVE_UP_AFTER_BLOCKS:
            msg = ("Resy bot stopped: Resy has refused every request for a while "
                   "(rate limit or block). Try again later.")
            log.error(msg)
            self.alerts.send(msg)
            return None
        self.backoff = min(max(60, self.backoff * 2), MAX_BACKOFF)
        if self.blocked_checks == 1:
            self.alerts.send("Resy bot: Resy is refusing requests (rate limit?). "
                             "Slowing down and retrying.")
        log.warning("Resy is refusing requests; waiting %ss.", self.backoff)
        return self.backoff

    # ------------------------------------------------------------------ booking
    def _book_best(self, found: list[planner.Candidate]):
        for c in sorted(found, key=lambda c: c.rank):
            if c.day in self.booked_days:
                continue
            if self.cfg.dry_run:
                log.info("DRY RUN: would book %s", describe(c, self.cfg.party_size))
                self.alerts.send("[Test] Resy bot would book " + describe(c, self.cfg.party_size))
                self._booked(c, None)
            elif self.cfg.stop_after_first and self._read_bookings():
                log.info("Another run already booked a table; stopping.")
                self.done = True
            else:
                self._try_book(c)
            if self.done:
                return

    def _try_book(self, c: planner.Candidate) -> bool:
        log.info("Booking %s", describe(c, self.cfg.party_size))
        try:
            result = self.client.book(c.token, c.day.isoformat(), self.cfg.party_size)
        except ResyError as e:
            log.info("Couldn't book that one (%s); trying the next.", e)
            return False
        except (requests.RequestException, KeyError, ValueError) as e:
            # The request may still have gone through. Check before trying another slot,
            # so we never end up with two tables.
            log.warning("Booking request failed (%s); checking whether it went through.", e)
            if not self._confirm_booked(c):
                return False
            result = {}
        self._booked(c, result)
        msg = "Booked " + describe(c, self.cfg.party_size) + ". Details are in your Resy app."
        log.info(msg)
        self.alerts.send(msg)
        return True

    def _confirm_booked(self, c: planner.Candidate) -> bool | None:
        try:
            for res in self.client.upcoming_reservations():
                if res["venue_id"] == c.restaurant.venue_id and res["day"] == c.day.isoformat():
                    return True
            return False
        except (ResyError, requests.RequestException) as e:
            msg = (f"Resy bot stopped: a booking at {c.restaurant.name} on {c.day} may or may "
                   f"not have gone through ({e}). Check the Resy app.")
            log.error(msg)
            self.alerts.send(msg)
            self.done = True
            return None

    def _booked(self, c: planner.Candidate, result: dict | None):
        self.booked_days.add(c.day)
        if self.cfg.stop_after_first:
            self.done = True
        if result is None:      # dry run: nothing to record
            return
        bookings = self._read_bookings()
        bookings.append({
            "restaurant": c.restaurant.name,
            "day": c.day.isoformat(),
            "time": c.start.strftime("%H:%M"),
            "seating": c.seating,
            "party_size": self.cfg.party_size,
            "resy_token": result.get("resy_token"),
            "reservation_id": result.get("reservation_id"),
            "booked_at": self.now().isoformat(timespec="seconds"),
        })
        self.bookings_file.write_text(json.dumps(bookings, indent=2), encoding="utf-8")

    def _read_bookings(self) -> list[dict]:
        if not self.bookings_file.exists():
            return []
        return json.loads(self.bookings_file.read_text(encoding="utf-8"))


def describe(c: planner.Candidate, party_size: int) -> str:
    """e.g. 'Lilia, Sat Nov 14 at 7:30 PM for 2 (Dining Room)'"""
    when = f"{c.start:%a %b} {c.start.day} at {c.start:%I:%M %p}".replace(" 0", " ")
    seating = f" ({c.seating})" if c.seating else ""
    return f"{c.restaurant.name}, {when} for {party_size}{seating}"
