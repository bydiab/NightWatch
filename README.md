# Course Seat Alert

Emails you when a seat opens up. Runs on GitHub Actions
every 5 minutes for free — no server needed.

## How it works
- Makes ONE request to FCC's course catalog API per run (the same
  internal endpoint the catalog page itself uses) and gets the whole
  term's seat data back at once — not thousands of requests.
- Compares against the previous run's seat counts (stored in
  `course_data/latest_seats.json`, committed back to the repo each time).
- Emails you only when a course goes from 0 (or unknown) available
  seats to 1+.

## Setup (5 minutes)

1. **Create a new GitHub repo** and push these files to it (see commands below).

2. **Get an app password for email.**
   If you use Gmail: go to your Google Account → Security → 2-Step
   Verification → App passwords → create one for "Mail". Copy the
   16-character password.

3. **Add repo secrets** — in your new repo: Settings → Secrets and
   variables → Actions → New repository secret. Add:
   | Name | Value (Gmail example) |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASS` | the 16-character app password |
   | `ALERT_TO` | the email address you want alerts sent to |

4. **Pick which course(s) to watch.** Open
   `.github/workflows/check_seats.yml` and edit the `WATCHLIST` line:
   ```yaml
   WATCHLIST: "COMP 111,COMP 206"   # comma-separated course codes
   ```
   Leave it as `""` to get alerted about ANY course in the catalog
   opening up (noisier — you'll get emails for courses you don't care
   about too).

5. **Check the term code.** `TERM_CODE: "2026FA"` in the workflow
   matches "2026 Fall" on FCC's site. Update it each semester.

6. Push to GitHub — the workflow will start running automatically on
   its schedule. You can also trigger it manually anytime from the
   **Actions** tab → "Check FCC seat availability" → **Run workflow**,
   which is the fastest way to confirm it's working.

## Push to your own repo

```bash
cd NightWatch
git init
git add .
git commit -m "Initial commit: seat alert bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

## Notes
- GitHub's free `cron` schedule can lag a few minutes under load —
  treat "every 5 min" as "every 5-10 min" in practice.
- The state file (`latest_seats.json`) gets committed by the workflow
  itself after each run, so don't edit it manually.
