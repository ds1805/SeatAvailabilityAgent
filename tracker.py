"""Train seat tracker: emails you when available seats drop to 50, 40, 30, 20, 10 (and sold out).

All settings come from environment variables (set in the GitHub workflow / secrets).
"""
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.message import EmailMessage

THRESHOLDS = [50, 40, 30, 20, 10, 0]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def env(name, default=None, required=True):
    value = os.environ.get(name, default)
    if required and (value is None or str(value).strip() == "" or str(value).startswith("CHANGE_ME")):
        sys.exit(f"Missing or unfilled setting: {name}")
    return value


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerted": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_availability():
    host = env("RAPIDAPI_HOST", "irctc1.p.rapidapi.com", required=False)
    path = env("API_PATH", "/api/v1/checkSeatAvailability", required=False)
    params = {
        "classType": env("TRAVEL_CLASS", "3A", required=False),
        "fromStationCode": env("FROM_STATION"),
        "quota": env("QUOTA", "GN", required=False),
        "toStationCode": env("TO_STATION"),
        "trainNo": env("TRAIN_NO"),
        "date": env("JOURNEY_DATE"),
    }
    url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "x-rapidapi-key": env("RAPIDAPI_KEY"),
        "x-rapidapi-host": host,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"API error {e.code}: {e.read().decode()[:300]}")


def parse_seats(data):
    """Return (seat_count or None, status_text) for the journey date."""
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return None, json.dumps(data)[:300]

    want = datetime.strptime(env("JOURNEY_DATE"), "%Y-%m-%d").date()
    chosen = items[0]
    for item in items:
        text = json.dumps(item)
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"):
            for m in re.findall(r"\d{1,4}-[\dA-Za-z]{1,3}-\d{2,4}", text):
                try:
                    if datetime.strptime(m, fmt).date() == want:
                        chosen = item
                except ValueError:
                    pass

    text = json.dumps(chosen)
    m = re.search(r"AVAILABLE[-\s]*0*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), m.group(0)
    if re.search(r"WL|RAC|REGRET|NOT AVAILABLE|TRAIN DEPARTED|CURR_AVBL.?0\b", text, re.IGNORECASE):
        return 0, text[:200]
    return None, text[:300]


def send_email(subject, body):
    user = env("GMAIL_USER")
    password = env("GMAIL_APP_PASSWORD")
    to = env("ALERT_TO", user, required=False) or user
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def decide_alert(seats, alerted):
    """Return (threshold_to_report or None, new_alerted_list)."""
    # If seats went back up (cancellations), re-arm the thresholds below the new count.
    alerted = [t for t in alerted if t >= seats]
    crossed = [t for t in THRESHOLDS if seats <= t and t not in alerted]
    if not crossed:
        return None, alerted
    new_alerted = sorted(set(alerted + crossed), reverse=True)
    return min(crossed), new_alerted


def main():
    if os.environ.get("SEND_TEST") == "true":
        send_email("Train tracker: test email", "Email setup works. Tracking is running.")
        print("Test email sent.")

    if datetime.strptime(env("JOURNEY_DATE"), "%Y-%m-%d").date() < date.today():
        print("Journey date has passed. Disable the workflow.")
        return

    data = fetch_availability()
    seats, status = parse_seats(data)
    if seats is None:
        sys.exit(f"Could not read seat count. Raw status: {status}")

    train = env("TRAIN_NO")
    print(f"Train {train} {env('TRAVEL_CLASS', '3A', required=False)} on "
          f"{env('JOURNEY_DATE')}: {seats} seats ({status})")

    state = load_state()
    threshold, new_alerted = decide_alert(seats, state.get("alerted", []))
    state["alerted"] = new_alerted
    state["last_seats"] = seats
    save_state(state)

    if threshold is None:
        print("No new threshold crossed.")
        return

    if seats == 0:
        subject = f"Train {train}: {env('TRAVEL_CLASS', '3A', required=False)} SOLD OUT"
    else:
        subject = f"Train {train}: only {seats} seats left in {env('TRAVEL_CLASS', '3A', required=False)}"
    body = (
        f"Train {train} ({env('FROM_STATION')} to {env('TO_STATION')})\n"
        f"Date: {env('JOURNEY_DATE')}  Class: {env('TRAVEL_CLASS', '3A', required=False)}  "
        f"Quota: {env('QUOTA', 'GN', required=False)}\n"
        f"Status: {status}\n"
        f"Seats left: {seats} (dropped to the {threshold} alert level)\n\n"
        f"Book on IRCTC if you still want it."
    )
    send_email(subject, body)
    print(f"Email sent for threshold {threshold}.")


if __name__ == "__main__":
    main()
