"""Pure decision logic: when a date's tables are released, and which slot is best."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from config import Restaurant

WAIT, FAST, OPEN = "wait", "fast", "open"


def release_moment(r: Restaurant, day: dt.date) -> dt.datetime | None:
    """When tables for `day` go on sale, or None if we don't know the restaurant's rule."""
    if r.release_days_ahead is None or r.release_time is None:
        return None
    return dt.datetime.combine(day - dt.timedelta(days=r.release_days_ahead),
                               r.release_time, tzinfo=r.timezone)


def phase(r: Restaurant, day: dt.date, now: dt.datetime,
          lead: dt.timedelta, window: dt.timedelta) -> str:
    """WAIT: not released yet, don't ask Resy. FAST: around the release moment, check
    every few seconds. OPEN: released (or rule unknown), check at the normal pace."""
    release = release_moment(r, day)
    if release is None:
        return OPEN
    if now < release - lead:
        return WAIT
    if now <= release + window:
        return FAST
    return OPEN


@dataclass(frozen=True)
class Candidate:
    rank: tuple
    restaurant: Restaurant
    day: dt.date
    start: dt.datetime      # local to the restaurant
    seating: str
    token: str


def candidates(r: Restaurant, r_index: int, day: dt.date, d_index: int,
               slots: list[dict]) -> list[Candidate]:
    """Slots that fit the restaurant's time window and seating, with a sort key:
    restaurant priority, then date priority, then seating preference, then how close
    the time is to `preferred` (or how early, if no preferred time)."""
    out = []
    for s in slots:
        try:
            start = dt.datetime.fromisoformat(s["start"])
        except (KeyError, ValueError):
            continue
        if start.date() != day or not (r.earliest <= start.time() <= r.latest):
            continue
        seating = (s.get("type") or "").lower()
        if r.seating and seating not in r.seating:
            continue
        seat_rank = r.seating.index(seating) if r.seating else 0
        minutes = start.hour * 60 + start.minute
        anchor = r.preferred or r.earliest
        time_rank = abs(minutes - (anchor.hour * 60 + anchor.minute))
        out.append(Candidate(rank=(r_index, d_index, seat_rank, time_rank, minutes),
                             restaurant=r, day=day, start=start,
                             seating=s.get("type") or "", token=s["token"]))
    return out
