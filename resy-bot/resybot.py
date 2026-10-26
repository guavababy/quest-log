#!/usr/bin/env python3
"""Resy reservation bot.

  python resybot.py search "lilia"     find a restaurant's slug / venue id
  python resybot.py check              one round of checks, never books
  python resybot.py test-alert         send yourself a test alert
  python resybot.py run                keep watching and book (see config.toml)

Your Resy password is read from the RESY_PASSWORD environment variable, or asked for
when the bot starts. It is never saved.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

import requests

import config as config_mod
from alerts import Alerts
from bot import AlreadyBooked, Bot
from resy_api import ResyClient, ResyError

HERE = Path(__file__).resolve().parent
log = logging.getLogger("resybot")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Watch Resy and book a table.")
    p.add_argument("--config", type=Path, default=HERE / "config.toml")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search", help="find a restaurant's slug and venue id")
    s.add_argument("query")
    sub.add_parser("check", help="one round of checks; never books")
    sub.add_parser("test-alert", help="send yourself a test alert")
    sub.add_parser("run", help="keep watching and book (dry_run in config.toml decides)")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    try:
        cfg = config_mod.load(args.config)
        alerts = Alerts(cfg.alerts)
    except (config_mod.ConfigError, ValueError) as e:
        log.error("%s", e)
        return 2

    if args.command == "test-alert":
        return 0 if alerts.send("Resy bot test alert. Alerts are working.") else 1

    client = ResyClient()
    try:
        password = os.environ.get("RESY_PASSWORD") or getpass.getpass(f"Resy password for {cfg.email}: ")
        client.login(cfg.email, password)
    except ResyError as e:
        log.error("Resy login failed (%s). Check your email and password.", e)
        return 1
    except requests.RequestException as e:
        log.error("Couldn't reach Resy: %s", e)
        return 1
    if not client.payment_method_id:
        log.warning("No card found on your Resy account; restaurants that need one will fail.")

    if args.command == "search":
        for h in client.search(args.query):
            print(f"{h['name']:<35} {h['neighborhood'] or '':<20} {h['city'] or '':<15} "
                  f"slug={h['slug']}  id={h['id']}")
        return 0

    if args.command == "check":
        cfg.dry_run = True
        cfg.stop_after_first = False
        alerts = Alerts({"method": "none"})

    bot = Bot(cfg, client, alerts, state_dir=HERE)
    try:
        bot.start()
        if cfg.dry_run and args.command == "run":
            log.info("dry_run is on: the bot will only report what it would book.")
        bot.run(once=args.command == "check")
    except AlreadyBooked as e:
        log.info("%s", e)
    except KeyboardInterrupt:
        log.info("Stopped.")
    except (ResyError, requests.RequestException) as e:
        log.error("Stopping: %s", e)
        alerts.send(f"Resy bot stopped with an error: {e}")
        return 1
    except Exception as e:
        log.exception("Unexpected error")
        alerts.send(f"Resy bot crashed: {e}")
        return 1
    return 0


def setup_logging(verbose: bool):
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    if log.handlers:
        return
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(HERE / "resybot.log", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


if __name__ == "__main__":
    sys.exit(main())
