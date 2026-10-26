# Resy reservation bot

Watches Resy for tables at the restaurants you list, books the best one using the
card on your Resy account, and texts you when it's done.

- **Ranked list:** restaurants are tried in the order you list them, then your dates in
  the order you list them, then the time closest to your preferred time.
- **One booking, then stop:** after it books one table it stops for good (it won't
  start again until you delete `booked.json`). Set `stop_after_first = false` to keep
  looking. It still books at most one table per date.
- **Release times:** if you enter a restaurant's release rule (e.g. 30 days ahead at
  10:00), the bot leaves Resy alone until 30 seconds before that moment. Then it
  checks every 2 seconds for 3 minutes. The rest of the time it checks about every
  2 minutes, which also catches cancellations.
- **Safety:**
  - It won't book a date where you already have a reservation at one of your listed
    restaurants.
  - If a booking request times out, it checks whether the booking went through before
    trying another slot.
  - If Resy starts refusing requests, it slows down. If the refusals continue, it stops
    and alerts you.

> **Read first.** Resy has no public API. This bot uses the same unofficial calls the
> resy.com website makes, so it can break whenever Resy changes them. Resy says it
> watches for misuse, cancels reservations that break its rules, and deactivates
> accounts that violate its Terms of Service. Using this puts your Resy account at
> risk. See the Resy blog post
> [What Restaurant Operators Wish You Knew About Reservation Fraud](https://blog.resy.com/resources/restaurant-reservation-fraud/).

## Setup (Windows)

1. Install Python 3.11 or newer from <https://www.python.org/downloads/>. Tick
   "Add python.exe to PATH" during install.
2. Open **Command Prompt** in this `resy-bot` folder and run:
   ```
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy config.example.toml config.toml
   ```
3. Edit `config.toml` in Notepad: your Resy email, party size, dates, times,
   restaurants and alerts. Every setting is explained in the file.
4. Find each restaurant's `venue` value. Either paste its resy.com URL, or run:
   ```
   py resybot.py search "restaurant name"
   ```

On macOS or Linux, use `python3` instead of `py` and `source .venv/bin/activate`.

## Text alerts

Free email-to-text addresses no longer work reliably. AT&T reportedly shut its gateway
down in June 2025, and Verizon's is reportedly ending in 2027 (see
[Sociocs](https://www.sociocs.com/post/how-to-send-a-text-message-from-email-why-the-manual-method-fails-business/)).
So the bot sends real texts through **Twilio** instead:

1. Sign up at <https://www.twilio.com> and get a phone number. Twilio charges per text.
   Check its current US sending rules, since some numbers need registering before they
   can send.
2. Put your Account SID, your Twilio number and your own mobile number in
   `[alerts]`. Put the Auth Token in the file as well, or set it as the
   `TWILIO_AUTH_TOKEN` environment variable.
3. Run `py resybot.py test-alert` to make sure the text arrives.

There are two free alternatives: `method = "email"` (works with a Gmail app password),
or `method = "ntfy"` (a push notification through the free [ntfy](https://ntfy.sh) app).

## Running it

```
py resybot.py check     # one round: shows what it WOULD book, never books
py resybot.py run       # keeps watching
```

`config.toml` starts with `dry_run = true`. In that mode `run` only reports what it
would book and sends a "[Test]" alert. Once that looks right, set `dry_run = false`.

It asks for your Resy password when it starts and never saves it. To skip the prompt,
set the `RESY_PASSWORD` environment variable.

Leave the window open while it runs, and stop it with Ctrl+C. Your PC has to stay on
and awake, so turn off sleep under Settings → System → Power. Everything it does is
logged to `resybot.log`, and bookings are recorded in `booked.json`.

## Tests

```
pip install pytest
py -m pytest
```

## Where the API details come from

These endpoints and fields were taken from open-source Resy bots. They are not
documented by Resy:

- [Alkaar/resy-booking-bot](https://github.com/Alkaar/resy-booking-bot): finding open
  times, getting booking details, booking.
- [andreb999999/resy-bot](https://github.com/andreb999999/resy-bot): login, venue
  lookup, availability calendar, upcoming reservations.
