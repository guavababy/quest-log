import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_mod  # noqa: E402
import planner  # noqa: E402
from bot import AlreadyBooked, Bot, describe  # noqa: E402
from resy_api import ResyError  # noqa: E402

NY = ZoneInfo("America/New_York")
DAY1, DAY2 = dt.date(2026, 11, 14), dt.date(2026, 11, 21)


def restaurant(name, venue_id, **kw):
    defaults = dict(name=name, venue=str(venue_id), city="ny", timezone=NY,
                    earliest=dt.time(18), latest=dt.time(21, 30), preferred=dt.time(19, 30))
    defaults.update(kw)
    return config_mod.Restaurant(**defaults)


def make_cfg(restaurants, **kw):
    defaults = dict(email="me@example.com", party_size=2, dates=[DAY1, DAY2],
                    restaurants=restaurants, stop_after_first=True, dry_run=False,
                    check_every_seconds=120, release_lead_seconds=30,
                    release_window_minutes=3, fast_check_seconds=2,
                    alerts={"method": "none"})
    defaults.update(kw)
    return config_mod.Config(**defaults)


def slot(day, hhmm, kind="Dining Room", token=None):
    return {"start": f"{day} {hhmm}:00", "type": kind, "token": token or f"{day}-{hhmm}-{kind}"}


class FakeClient:
    def __init__(self, slots=None, open_days=None, reservations=(), book_errors=None):
        self.slots = slots or {}            # (venue_id, day) -> [slot]
        self.open_days = open_days          # venue_id -> set of days, None = use slots
        self.reservations = list(reservations)
        self.book_errors = book_errors or {}  # token -> exception
        self.booked, self.find_calls, self.calendar_calls = [], [], []

    def venue(self, venue, city):
        return {"id": int(venue), "name": None}

    def upcoming_reservations(self):
        return self.reservations

    def calendar(self, venue_id, party, start, end):
        self.calendar_calls.append((venue_id, start, end))
        if self.open_days is not None:
            return self.open_days.get(venue_id, set())
        return {d for (v, d), s in self.slots.items() if v == venue_id and s}

    def find(self, venue_id, day, party):
        self.find_calls.append((venue_id, day))
        return self.slots.get((venue_id, day), [])

    def book(self, token, day, party):
        if token in self.book_errors:
            raise self.book_errors[token]
        self.booked.append(token)
        return {"resy_token": "rt-" + token, "reservation_id": 1}


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def make_bot(tmp_path, cfg, client, now=dt.datetime(2026, 11, 1, 12, tzinfo=NY)):
    alerts = FakeAlerts()
    bot = Bot(cfg, client, alerts, state_dir=tmp_path, now=lambda: now, sleep=lambda s: None)
    bot.start()
    return bot, alerts


# ---------------------------------------------------------------- ranking
def test_books_highest_ranked_restaurant_first(tmp_path):
    a, b = restaurant("A", 1), restaurant("B", 2)
    client = FakeClient(slots={
        (2, "2026-11-14"): [slot("2026-11-14", "19:30")],
        (1, "2026-11-21"): [slot("2026-11-21", "20:00")],
    })
    bot, alerts = make_bot(tmp_path, make_cfg([a, b]), client)
    assert bot.check_once() is None                    # booked one, so it stops
    assert client.booked == ["2026-11-21-20:00-Dining Room"]
    assert "Booked A, Sat Nov 21 at 8:00 PM for 2 (Dining Room)" in alerts.sent[0]


def test_picks_time_closest_to_preferred_and_respects_window():
    r = restaurant("A", 1, seating=["bar", "dining room"])
    slots = [slot("2026-11-14", "17:30", "Bar"),          # too early
             slot("2026-11-14", "18:00", "Dining Room"),
             slot("2026-11-14", "19:45", "Dining Room"),
             slot("2026-11-14", "21:00", "Bar"),
             slot("2026-11-14", "19:30", "Patio")]        # seating not allowed
    found = sorted(planner.candidates(r, 0, DAY1, 0, slots), key=lambda c: c.rank)
    assert [(c.start.strftime("%H:%M"), c.seating) for c in found] == [
        ("21:00", "Bar"), ("19:45", "Dining Room"), ("18:00", "Dining Room")]


def test_falls_back_to_next_slot_when_booking_fails(tmp_path):
    a = restaurant("A", 1)
    first, second = slot("2026-11-14", "19:30"), slot("2026-11-14", "20:30")
    client = FakeClient(slots={(1, "2026-11-14"): [first, second]},
                        book_errors={first["token"]: ResyError(412, "gone")})
    bot, _ = make_bot(tmp_path, make_cfg([a]), client)
    bot.check_once()
    assert client.booked == [second["token"]]


# ---------------------------------------------------------------- stopping / one booking
def test_dry_run_never_books(tmp_path):
    client = FakeClient(slots={(1, "2026-11-14"): [slot("2026-11-14", "19:30")]})
    bot, alerts = make_bot(tmp_path, make_cfg([restaurant("A", 1)], dry_run=True), client)
    assert bot.check_once() is None
    assert client.booked == []
    assert alerts.sent[0].startswith("[Test]")
    assert not (tmp_path / "booked.json").exists()


def test_refuses_to_start_after_a_booking(tmp_path):
    client = FakeClient(slots={(1, "2026-11-14"): [slot("2026-11-14", "19:30")]})
    cfg = make_cfg([restaurant("A", 1)])
    bot, _ = make_bot(tmp_path, cfg, client)
    bot.check_once()
    with pytest.raises(AlreadyBooked):
        make_bot(tmp_path, cfg, FakeClient())


def test_keep_looking_books_one_table_per_date(tmp_path):
    client = FakeClient(slots={
        (1, "2026-11-14"): [slot("2026-11-14", "19:30"), slot("2026-11-14", "20:00")],
        (1, "2026-11-21"): [slot("2026-11-21", "19:30")],
    })
    bot, _ = make_bot(tmp_path, make_cfg([restaurant("A", 1)], stop_after_first=False), client)
    bot.check_once()
    assert client.booked == ["2026-11-14-19:30-Dining Room", "2026-11-21-19:30-Dining Room"]
    assert bot.check_once() is None      # every date booked, nothing left to look for


def test_skips_dates_with_existing_reservation(tmp_path):
    client = FakeClient(slots={(1, "2026-11-14"): [slot("2026-11-14", "19:30")],
                               (1, "2026-11-21"): [slot("2026-11-21", "19:30")]},
                        reservations=[{"day": "2026-11-14", "venue_id": 1}])
    bot, _ = make_bot(tmp_path, make_cfg([restaurant("A", 1)]), client)
    bot.check_once()
    assert client.booked == ["2026-11-21-19:30-Dining Room"]


def test_timeout_that_actually_booked_is_not_booked_twice(tmp_path):
    first, second = slot("2026-11-14", "19:30"), slot("2026-11-14", "20:30")
    client = FakeClient(slots={(1, "2026-11-14"): [first, second]},
                        book_errors={first["token"]: requests.Timeout("slow")})
    client.reservations = [{"day": "2026-11-14", "venue_id": 1}]  # it went through
    r = restaurant("A", 1)
    r.venue_id = 1    # set by hand: start() would see the reservation up front
    bot = Bot(make_cfg([r]), client, FakeAlerts(), tmp_path,
              now=lambda: dt.datetime(2026, 11, 1, 12, tzinfo=NY))
    bot.check_once()
    assert client.booked == []           # didn't try the 20:30 slot
    assert bot.done


# ---------------------------------------------------------------- release timing
def test_waits_for_release_then_checks_fast(tmp_path):
    r = restaurant("A", 1, release_days_ahead=14, release_time=dt.time(10))
    cfg = make_cfg([r], dates=[DAY1])               # releases Oct 31 at 10:00 NY
    client = FakeClient()

    bot, _ = make_bot(tmp_path, cfg, client, now=dt.datetime(2026, 10, 31, 9, 0, tzinfo=NY))
    pause = bot.check_once()
    assert client.find_calls == [] and client.calendar_calls == []   # nothing asked yet
    assert pause <= 59 * 60 + 30                                     # wakes ~30s before 10:00

    bot.now = lambda: dt.datetime(2026, 10, 31, 9, 59, 45, tzinfo=NY)
    assert bot.check_once() == 2                                     # fast mode
    assert client.find_calls == [(1, "2026-11-14")]

    bot.now = lambda: dt.datetime(2026, 10, 31, 10, 5, tzinfo=NY)
    assert bot.check_once() >= 90                                    # back to normal pace


def test_calendar_limits_find_calls(tmp_path):
    client = FakeClient(open_days={1: {"2026-11-21"}})
    bot, _ = make_bot(tmp_path, make_cfg([restaurant("A", 1)]), client)
    bot.check_once()
    assert client.calendar_calls == [(1, "2026-11-14", "2026-11-21")]
    assert client.find_calls == [(1, "2026-11-21")]


def test_backs_off_then_gives_up_when_blocked(tmp_path):
    class Blocked(FakeClient):
        def calendar(self, *a):
            raise ResyError(429, "slow down")

        def find(self, *a):
            raise ResyError(429, "slow down")

    bot, alerts = make_bot(tmp_path, make_cfg([restaurant("A", 1)]), Blocked())
    pauses = [bot.check_once() for _ in range(6)]
    assert pauses[:5] == [60, 120, 240, 480, 960]
    assert pauses[5] is None
    assert len(alerts.sent) == 2         # "slowing down" once, then "stopped"


def test_describe_formats_times():
    c = planner.Candidate(rank=(), restaurant=restaurant("Lilia", 1), day=DAY1,
                          start=dt.datetime(2026, 11, 14, 9, 5), seating="", token="t")
    assert describe(c, 4) == "Lilia, Sat Nov 14 at 9:05 AM for 4"
