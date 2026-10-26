"""Small client for Resy's unofficial web API (the same calls resy.com makes).

Resy publishes no official API. The endpoints and fields below were taken from
open-source Resy bots and may change without notice:
  - https://github.com/Alkaar/resy-booking-bot  (find / details / book)
  - https://github.com/andreb999999/resy-bot     (login, venue, calendar, reservations)
"""
from __future__ import annotations

import json
import logging
import re

import requests

API = "https://api.resy.com"
# Public key embedded in resy.com's own web app (the same for every user).
API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

log = logging.getLogger("resybot")


class ResyError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body

    @property
    def blocked(self) -> bool:
        """Resy is rate-limiting or refusing us, as opposed to e.g. a slot being gone."""
        return self.status in (403, 429)


class ResyClient:
    def __init__(self, timeout: float = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f'ResyAPI api_key="{API_KEY}"',
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "X-Origin": "https://resy.com",
        })
        self.payment_method_id: int | None = None
        self._email: str | None = None
        self._password: str | None = None

    # -- plumbing
    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs):
        r = self.session.request(method, API + path, timeout=self.timeout, **kwargs)
        log.debug("%s %s %s -> %s", method, path, kwargs.get("params", ""), r.status_code)
        # 401/419: the auth token expired. Log in again once and retry.
        if r.status_code in (401, 419) and retry_auth and self._password:
            self.login(self._email, self._password)
            return self._request(method, path, retry_auth=False, **kwargs)
        if r.status_code >= 400:
            raise ResyError(r.status_code, r.text[:300])
        return r.json() if r.content else {}

    # -- auth
    def login(self, email: str, password: str) -> dict:
        data = self._request("POST", "/3/auth/password", retry_auth=False,
                             data={"email": email, "password": password})
        token = data["token"]
        self.session.headers["X-Resy-Auth-Token"] = token
        self.session.headers["X-Resy-Universal-Auth"] = token
        self.payment_method_id = pick_payment_method(data.get("payment_methods"))
        self._email, self._password = email, password
        return data

    # -- venues
    def search(self, query: str, lat: float = 40.7128, lng: float = -74.0060) -> list[dict]:
        body = {"query": query, "geo": {"latitude": lat, "longitude": lng},
                "types": ["venue"], "per_page": 10, "page": 1}
        data = self._request("POST", "/3/venuesearch/search", json=body)
        hits = []
        for h in data.get("search", {}).get("hits", []):
            vid = h.get("id")
            hits.append({
                "id": vid.get("resy") if isinstance(vid, dict) else vid,
                "name": h.get("name"),
                "neighborhood": h.get("neighborhood"),
                "city": (h.get("location") or {}).get("name"),
                "slug": h.get("url_slug"),
            })
        return hits

    def venue(self, venue: str, city: str) -> dict:
        """Look up a venue by its id, url slug, or resy.com URL. Returns {"id", "name"}."""
        if str(venue).isdigit():
            return {"id": int(venue), "name": None}
        m = re.search(r"cities/([^/]+)/(?:venues/)?([^/?#]+)", venue)
        slug, city = (m.group(2), m.group(1)) if m else (venue, city)
        data = self._request("GET", "/3/venue", params={"url_slug": slug, "location": city})
        return {"id": int(data["id"]["resy"]), "name": data.get("name")}

    # -- availability
    def calendar(self, venue_id: int, party_size: int, start: str, end: str) -> set[str]:
        """Dates (YYYY-MM-DD) between start and end that have open tables."""
        data = self._request("GET", "/4/venue/calendar",
                             params={"venue_id": venue_id, "num_seats": party_size,
                                     "start_date": start, "end_date": end})
        return {d["date"] for d in data.get("scheduled", [])
                if (d.get("inventory") or {}).get("reservation") == "available"}

    def find(self, venue_id: int, day: str, party_size: int) -> list[dict]:
        """Open slots for one venue and day: [{"start", "type", "token"}]."""
        data = self._request("GET", "/4/find",
                             params={"lat": 0, "long": 0, "day": day,
                                     "party_size": party_size, "venue_id": venue_id})
        slots = []
        for v in data.get("results", {}).get("venues", []):
            for s in v.get("slots", []):
                config, date = s.get("config") or {}, s.get("date") or {}
                if config.get("token") and date.get("start"):
                    slots.append({"start": date["start"],      # "2026-11-14 19:30:00"
                                  "type": config.get("type") or "",
                                  "token": config["token"]})
        return slots

    # -- booking
    def details(self, config_token: str, day: str, party_size: int) -> dict:
        """What the website calls when you click a time; returns the book_token."""
        params = {"config_id": config_token, "day": day, "party_size": party_size}
        try:
            return self._request("GET", "/3/details", params=params)
        except ResyError as e:
            if e.blocked:
                raise
            return self._request("POST", "/3/details", json={"commit": 1, **params})

    def book(self, config_token: str, day: str, party_size: int) -> dict:
        """Book a slot using the card on file. Returns {"resy_token", "reservation_id", ...}."""
        details = self.details(config_token, day, party_size)
        payment_id = self.payment_method_id or pick_payment_method(
            (details.get("user") or {}).get("payment_methods"))
        form = {"book_token": details["book_token"]["value"],
                "source_id": "resy.com-venue-details"}
        if payment_id:
            form["struct_payment_method"] = json.dumps({"id": payment_id}, separators=(",", ":"))
        return self._request("POST", "/3/book", data=form)

    def upcoming_reservations(self) -> list[dict]:
        data = self._request("GET", "/3/user/reservations",
                             params={"limit": 25, "offset": 1, "type": "upcoming"})
        out = []
        for r in data.get("reservations", []):
            vid = str((r.get("venue") or {}).get("id", ""))
            out.append({"day": r.get("day"), "venue_id": int(vid) if vid.isdigit() else None})
        return out


def pick_payment_method(methods) -> int | None:
    """The default card on the account, else the first one."""
    methods = methods or []
    for m in methods:
        if m.get("is_default"):
            return m.get("id")
    return methods[0].get("id") if methods else None
