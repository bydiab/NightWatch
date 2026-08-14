"""
FCC Course Seat Alert
----------------------
Fetches the FULL course catalog for a given term in ONE request (using
FCC's Empower SIS internal catalog API), compares seat availability
against the previous run, and emails you when a seat opens up on a
course you're watching (or on ANY course, if WATCHLIST is empty).

State (previous seat counts) is stored in course_data/latest_seats.json
so each run only alerts on a CHANGE, not on already-open seats every time.
"""

import json
import os
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ================= CONFIG =================
BASE_URL = "https://mysis-fccollege.empower-xl.com"
CATALOG_URL = f"{BASE_URL}/fusebox.cfm?fuseaction=CourseCatalog&rpt=1"
API_URL = f"{BASE_URL}/cfcs/courseCatalog.cfc?method=GetList"

DATA_DIR = "course_data"
STATE_FILE = os.path.join(DATA_DIR, "latest_seats.json")

TERM_CODE = os.environ.get("TERM_CODE", "2026FA")  # e.g. 2026FA for 2026 Fall

# Comma-separated list of course codes to watch, e.g. "COMP 111,COMP 206,DLD 101"
# Leave EMPTY ("") to watch the ENTIRE catalog.
WATCHLIST = [c.strip().upper() for c in os.environ.get("WATCHLIST", "").split(",") if c.strip()]

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": CATALOG_URL,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.6367.91 Safari/537.36",
}


# ================= FETCH =================
def create_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(CATALOG_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "TOKEN"}) or soup.find("input", {"name": "token"})
    token = token_input["value"] if token_input else ""
    return s, token


def fetch_courses(session, token, term):
    payload = {
        "method": "GetList",
        "fuseaction": "CourseCatalog",
        "token": token,
        "empower_global_term_id": term,
        "status": "1",
        "page": "1",
        "pageSize": "5000",
        "uiGridPageSize": "5000",
        "rows": "5000",
        "limit": "5000",
    }
    r = session.post(API_URL, data=payload, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data.get("html", "")


# ================= PARSE =================
def parse_courses_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.ui-grid-row")

    courses = []

    def safe(cols, i):
        return cols[i].get_text(strip=True) if i < len(cols) else ""

    skipped_header = 0
    for row in rows:
        cols = row.find_all("div", class_=lambda x: x and "ui-grid-col-" in x)
        if skipped_header < 2:
            skipped_header += 1
            continue
        if row.find("hr"):
            continue

        course_col = cols[1] if len(cols) > 1 else None
        if course_col is None:
            continue
        course_text = course_col.get_text("\n", strip=True)
        parts = [p.strip() for p in course_text.split("\n") if p.strip()]
        if not parts:
            continue

        first_line = parts[0].replace("\xa0", " ")
        tokens = first_line.split()
        if len(tokens) < 2:
            continue
        section = tokens[-1]
        course_code = " ".join(tokens[:-1])
        course_name = parts[-1] if len(parts) > 1 else ""
        unique = f"{course_code}/{section}"

        classroom = safe(cols, 3)

        schedule_col = cols[4] if len(cols) > 4 else None
        schedule_raw = ""
        if schedule_col is not None:
            schedule_text = schedule_col.get_text("\n", strip=True)
            schedule_parts = [p.strip() for p in schedule_text.split("\n") if p.strip()]
            days = ""
            time = ""
            for part in schedule_parts:
                if part.lower().startswith("start:"):
                    continue
                if "-" in part:
                    time = part
                elif re.match(r"^[A-Z\s]+$", part):
                    days = part
            schedule_raw = " | ".join(p for p in [days, time] if p)

        capacity = safe(cols, 6)
        available = safe(cols, 7)
        instructor = safe(cols, 5)

        courses.append({
            "course_code": course_code,
            "section": section,
            "unique": unique,
            "course_name": course_name,
            "instructor": instructor,
            "classroom": classroom,
            "schedule": schedule_raw,
            "capacity": capacity,
            "available": available,
        })

    return courses


def available_count(value):
    """Pull the first integer out of the 'available' cell."""
    m = re.search(r"-?\d+", value or "")
    return int(m.group()) if m else None


# ================= EMAIL =================
def send_email(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
    print(f"✓ Email sent: {subject}")


# ================= MAIN =================
def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    session, token = create_session()
    html = fetch_courses(session, token, TERM_CODE)
    courses = parse_courses_from_html(html)
    print(f"✓ Parsed {len(courses)} course sections for {TERM_CODE}")

    if WATCHLIST:
        watched = [c for c in courses if c["course_code"].upper() in WATCHLIST]
    else:
        watched = courses

    is_first_run = not os.path.exists(STATE_FILE)
    old_state = {}
    if not is_first_run:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old_state = json.load(f)

    new_state = {}
    alerts = []

    for c in watched:
        key = c["unique"]
        avail = available_count(c["available"])
        new_state[key] = avail

        if is_first_run:
            continue

        prev = old_state.get(key)
        if avail is not None and avail > 0 and (prev is None or prev <= 0):
            when = c["schedule"] or "schedule TBD"
            where = c["classroom"] or "room TBD"
            alerts.append(
                f"{c['course_code']} ({c['section']}) — {c['course_name']}\n"
                f"  Instructor: {c['instructor']}\n"
                f"  When: {when}\n"
                f"  Where: {where}\n"
                f"  Seats now available: {avail} / {c['capacity']}"
            )

    if is_first_run:
        print(f"✓ First run — saved baseline for {len(new_state)} sections, no alerts sent")
    elif alerts:
        body = (
            f"Seat(s) opened up as of {datetime.now(timezone.utc).isoformat()}Z:\n\n"
            + "\n\n".join(alerts)
        )
        send_email(f"🎓 {len(alerts)} course seat(s) opened up!", body)
    else:
        print("✓ No newly opened seats this run")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2)


if __name__ == "__main__":
    main()
