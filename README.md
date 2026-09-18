# BookMyShow Watcher

Watches 4 BookMyShow cinema pages and sends a Telegram message when one:

- **appears** — the requested date is currently outside BookMyShow's bookable
  window, so the URL silently falls back to today's listing. Once the site
  actually resolves the URL to the requested date, you get notified.
- **changes** — once a date is live, any change to the showtime listing
  (new film added, showtimes updated, etc.) triggers a notification.

Watched pages:

| Cinema | Date |
|---|---|
| Allu Cinemas: Kokapet (ALUC) | 26 Sep 2026 |
| Prasads Multiplex Hyderabad (PRHN) | 26 Sep 2026 |
| Allu Cinemas: Kokapet (ALUC) | 24 Sep 2026 |
| Prasads Multiplex Hyderabad (PRHN) | 24 Sep 2026 |

Runs on a GitHub Actions schedule (free) — no server to maintain. State is
tracked in `state.json`, committed back to the repo after each run.

## Why a headless browser instead of a simple HTTP request

BookMyShow sits behind Cloudflare's bot-challenge and renders pages
client-side, so a plain `requests.get()` gets a challenge page, not the
real content. `watch.py` uses Playwright (headless Chromium) to load each
page like a real browser would, then reads `window.location.href` after
the page settles to tell whether the requested date actually resolved (vs.
silently falling back to today).

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. You'll get a token like `123456789:AAExampleTokenXYZ`.
2. Message your new bot anything (e.g. "hi") so it can message you back.
3. Get your chat ID: visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   look for `"chat":{"id":123456789,...}` — or message
   [@userinfobot](https://t.me/userinfobot) and it'll reply with your ID
   directly.

### 2. Create the GitHub repo

This is set up as a **public** repo, checking every 10 minutes — public
repos get unlimited free GitHub Actions minutes, so the frequent schedule
doesn't burn through a quota. Nothing sensitive lives in the code itself;
your bot token/chat ID are stored as encrypted repo secrets, never
committed to a file.

```bash
cd bms-watcher
git init
git add .
git commit -m "Initial commit"
gh repo create bms-watcher --public --source=. --remote=origin --push
```

(No `gh` CLI? Create an empty repo on github.com, then
`git remote add origin <url> && git push -u origin main`.)

### 3. Add secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**, add:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. Done

The workflow runs every 10 minutes automatically
(`.github/workflows/watch.yml`). To test it immediately: **Actions** tab →
"BookMyShow Watcher" → **Run workflow**.

## Avoiding getting blocked

- Checks run every 10 minutes, not continuously — light enough to not look
  like abuse.
- Each run happens on a fresh GitHub-hosted runner with its own IP, so it
  doesn't look like one machine hammering the site repeatedly.
- A random 0–45s jitter is added before each run so requests aren't on a
  perfectly predictable clock tick.
- The browser context sets a realistic desktop Chrome user agent, locale,
  and timezone, and suppresses the `navigator.webdriver` flag that marks a
  browser as automated.
- If checks fail for about an hour straight (~6 consecutive runs), the bot
  sends you one warning message that it might be blocked, instead of
  failing silently forever.

If BookMyShow starts hard-blocking this despite the above, don't try to
fight it harder (e.g. rotating proxies, spoofing more aggressively) —
that crosses from "checking a page occasionally" into scraping evasion.
At that point, just check the page yourself, or increase the interval.

## Adjusting the schedule / dates

- Change `cron: "*/10 * * * *"` in `.github/workflows/watch.yml` to poll
  more/less often. This only stays free at any frequency while the repo is
  **public** — GitHub Actions minutes are metered (2,000/month free) on a
  private repo, and 10-minute checks would burn through that fast.
- Edit the `TARGETS` list in `watch.py` to watch different cinemas/dates.
