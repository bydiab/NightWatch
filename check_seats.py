"""
FCC Course Seat Alert
----------------------
Fetches the FULL course catalog for the newest regular term (Spring / Summer /
Fall) in ONE request, compares seat availability against the previous run, and
emails you when:
  - a seat opens up in a section that was full, or
  - a brand-new section appears in the catalog.

The term is detected automatically from FCC's own Term dropdown, so when FCC
adds the next semester the bot follows it. When the term changes, the first
run silently builds a fresh baseline instead of emailing the whole catalog.

Set TERM_CODE to a specific code (e.g. 2026FA) to pin one term, or leave it
as "auto" (default) to follow the newest.
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

TERM_SETTING = os.environ.get("TERM_CODE", "auto").strip() or "auto"

WATCHLIST = [c.strip().upper() for c in os.environ.get("WATCHLIST", "").split(",") if c.strip()]

SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
TERM_LABEL_RE = re.compile(r"^(\d{4})\s+(Winter|Spring|Summer|Fall)$", re.I)

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
    return s, token, soup


def detect_latest_term(soup):
    """Return (term_code, label) of the newest plain 'YYYY Season' term, or None."""
    best = None
    for opt in soup.find_all("option"):
        label = opt.get_text(strip=True)
        m = TERM_LABEL_RE.match(label)
        value = (opt.get("value") or "").strip()
        if not m or not value:
            continue
        rank = (int(m.group(1)), SEASON_ORDER[m.group(2).lower()])
        if best is None or rank > best[0]:
            best = (rank, value, label)
    return (best[1], best[2]) if best else None


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
    m = re.search(r"-?\d+", value or "")
    return int(m.group()) if m else None


# ================= STATE =================
def load_state():
    """Return (term_in_file, seats_dict, file_existed)."""
    if not os.path.exists(STATE_FILE):
        return None, {}, False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, {}, True
    if isinstance(data, dict) and isinstance(data.get("seats"), dict):
        return data.get("term"), data["seats"], True
    # Old flat format (before term tracking): treat as unknown term
    return None, {}, True


def save_state(term, seats):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"term": term, "seats": seats}, f, indent=2)


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

    old_term, old_seats, state_existed = load_state()

    session, token, soup = create_session()

    if TERM_SETTING.lower() == "auto":
        detected = detect_latest_term(soup)
        if detected:
            term, term_label = detected
        elif old_term:
            term, term_label = old_term, old_term
            print(f"! Could not detect term from the page, staying on {old_term}")
        else:
            raise RuntimeError("Could not detect the current term and none is saved")
    else:
        term, term_label = TERM_SETTING, TERM_SETTING

    html = fetch_courses(session, token, term)
    courses = parse_courses_from_html(html)
    print(f"✓ Parsed {len(courses)} course sections for {term}")

    if not courses:
        # Empty/failed response: don't touch saved state, otherwise the next
        # good run would treat every section as brand new.
        print("! No sections returned, skipping this run")
        return

    if WATCHLIST:
        watched = [c for c in courses if c["course_code"].upper() in WATCHLIST]
    else:
        watched = courses

    term_changed = (old_term != term)
    is_baseline_run = (not state_existed) or term_changed
    if not is_baseline_run:
        old_state = old_seats
    else:
        old_state = {}

    new_state = {}
    alerts = []

    for c in watched:
        key = c["unique"]
        avail = available_count(c["available"])
        new_state[key] = avail

        if is_baseline_run:
            continue

        is_new_section = key not in old_state
        prev = old_state.get(key)
        when = c["schedule"] or "schedule TBD"
        where = c["classroom"] or "room TBD"
        avail_display = avail if avail is not None else "unknown"

        if is_new_section:
            alerts.append(
                f"🆕 NEW SECTION — {c['course_code']} ({c['section']}) — {c['course_name']}\n"
                f"  Instructor: {c['instructor']}\n"
                f"  When: {when}\n"
                f"  Where: {where}\n"
                f"  Seats available: {avail_display} / {c['capacity']}"
            )
        elif avail is not None and avail > 0 and (prev is None or prev <= 0):
            alerts.append(
                f"✅ SEAT OPENED — {c['course_code']} ({c['section']}) — {c['course_name']}\n"
                f"  Instructor: {c['instructor']}\n"
                f"  When: {when}\n"
                f"  Where: {where}\n"
                f"  Seats now available: {avail} / {c['capacity']}"
            )

    if is_baseline_run:
        print(f"✓ Baseline saved for {term} ({len(new_state)} sections), no seat alerts sent")
        if state_existed and term_changed:
            try:
                send_email(
                    f"🎓 Now watching {term_label}",
                    f"The seat alert bot switched to {term_label} ({term}).\n"
                    f"It is tracking {len(new_state)} sections and will email you "
                    f"from here on when a seat opens or a new section appears.",
                )
            except Exception as e:  # a failed notice must not block saving state
                print(f"! Could not send term-change notice: {e}")
    elif alerts:
        body = (
            f"Update for {term_label} as of {datetime.now(timezone.utc).isoformat()}:\n\n"
            + "\n\n".join(alerts)
        )
        send_email(f"🎓 {len(alerts)} course update(s)!", body)
    else:
        print("✓ No newly opened seats or new sections this run")

    save_state(term, new_state)


if __name__ == "__main__":
    main()
