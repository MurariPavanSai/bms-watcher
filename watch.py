import hashlib
import json
import os
import random
import re
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_FILE = Path("state.json")
FAIL_THRESHOLD = 6  # ~1hr of consecutive failures at a 10min schedule before we warn once
SHOWTIME_RE = re.compile(r"\d{1,2}:\d{2}\s?(AM|PM)", re.I)

# type "cinema_date": a single cinema's day page. BookMyShow silently redirects
# an out-of-window date back to today, so "live" means the URL held on the
# requested date after the redirect logic had a chance to fire.
#
# type "movie_listing": a movie-wide listing across cinemas (BookMyShow or
# District). No redirect happens here; a not-yet-published date just renders
# with no showtimes at all, so "live" means at least one showtime is present.
TARGETS = [
    {
        "key": "ALUC_0926",
        "label": "Allu Cinemas Kokapet - 26 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/allu-cinemas-kokapet/buytickets/ALUC/20260926",
        "type": "cinema_date",
        "date": "20260926",
    },
    {
        "key": "PRHN_0926",
        "label": "Prasads Multiplex - 26 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/prasads-multiplex-hyderabad/buytickets/PRHN/20260926",
        "type": "cinema_date",
        "date": "20260926",
    },
    {
        "key": "ALUC_0924",
        "label": "Allu Cinemas Kokapet - 24 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/allu-cinemas-kokapet/buytickets/ALUC/20260924",
        "type": "cinema_date",
        "date": "20260924",
    },
    {
        "key": "PRHN_0924",
        "label": "Prasads Multiplex - 24 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/prasads-multiplex-hyderabad/buytickets/PRHN/20260924",
        "type": "cinema_date",
        "date": "20260924",
    },
    {
        "key": "PARADISE_BMS_0923",
        "label": "The Paradise - BookMyShow (Hyderabad, Telugu) - 23 Sep",
        "url": "https://in.bookmyshow.com/movies/hyderabad/the-paradise/buytickets/ET00518514/20260923?etCodes=*&language=telugu&refEventCode=ET00518514",
        "type": "movie_listing",
    },
    {
        "key": "PARADISE_DISTRICT_0923",
        "label": "The Paradise - District.in (Hyderabad) - 23 Sep",
        "url": "https://www.district.in/movies/the-paradise-movie-tickets-in-hyderabad-MV185027?frmtid=sfuykkkg9p&fromdate=2026-09-23",
        "type": "movie_listing",
    },
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def new_page(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1366, "height": 900},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context, context.new_page()


REDIRECT_POLL_SECONDS = 10  # how long to wait for the SPA's date-fallback redirect to fire


def page_text(page):
    try:
        return page.inner_text("main")
    except Exception:
        return page.inner_text("body")


def check_cinema_date(page, target):
    # The redirect-to-today logic runs asynchronously after load, and its
    # timing varies with network latency (it fires slower from GitHub's
    # runners than from a nearby machine). Poll instead of a fixed sleep,
    # and only conclude "live" once the URL has held steady for the full
    # window -- concluding early risks a false "live" positive.
    deadline = time.monotonic() + REDIRECT_POLL_SECONDS
    while time.monotonic() < deadline:
        if target["date"] not in page.url:
            break
        page.wait_for_timeout(400)

    live = target["date"] in page.url
    text = page_text(page) if live else ""
    return live, text


def check_movie_listing(page, target):
    # No redirect on these pages: a not-yet-published date just renders with
    # no showtimes. "Live" = at least one showtime is actually on the page.
    # A fixed render buffer (rather than networkidle) is used for these --
    # District.in in particular has background network chatter that never
    # goes fully idle, so waiting on networkidle here just times out.
    page.wait_for_timeout(4000)
    text = page_text(page)
    live = bool(SHOWTIME_RE.search(text))
    return live, (text if live else "")


def check(browser, target):
    # A fresh context per target matters: reusing one page across sequential
    # navigations leaves client-side state primed so a cinema_date page stops
    # redirecting a not-yet-bookable date back to today, producing false
    # "live" results.
    context, page = new_page(browser)
    try:
        wait_until = "networkidle" if target["type"] == "cinema_date" else "load"
        page.goto(target["url"], wait_until=wait_until, timeout=45000)

        title = page.title()
        if "moment" in title.lower() or "checking" in title.lower():
            raise RuntimeError(f"blocked by challenge page (title={title!r})")

        if target["type"] == "cinema_date":
            live, text = check_cinema_date(page, target)
        else:
            live, text = check_movie_listing(page, target)

        digest = hashlib.sha256(text.encode()).hexdigest() if text else None
        return live, digest
    finally:
        context.close()


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    state = load_state()
    meta = state.setdefault("_meta", {"consecutive_failures": 0, "warned": False})
    notifications = []
    any_success = False
    any_failure = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        for target in TARGETS:
            key = target["key"]
            prev = state.get(key, {"live": False, "hash": None})
            try:
                live, digest = check(browser, target)
                any_success = True
            except Exception as e:
                print(f"[warn] {key} check failed: {e}")
                any_failure = True
                time.sleep(random.uniform(3, 6))
                continue

            is_movie = target["type"] == "movie_listing"
            if live and not prev.get("live"):
                verb = "Shows just opened" if is_movie else "Booking page is now LIVE"
                notifications.append(f"{verb} for {target['label']}:\n{target['url']}")
            elif live and prev.get("live") and digest != prev.get("hash"):
                verb = "Showtimes changed" if is_movie else "Booking page changed"
                notifications.append(f"{verb} for {target['label']}:\n{target['url']}")

            state[key] = {"live": live, "hash": digest}
            time.sleep(random.uniform(2, 4))

        browser.close()

    if any_success:
        meta["consecutive_failures"] = 0
        meta["warned"] = False
    elif any_failure:
        meta["consecutive_failures"] = meta.get("consecutive_failures", 0) + 1
        if meta["consecutive_failures"] >= FAIL_THRESHOLD and not meta.get("warned"):
            notifications.append(
                "Watcher has failed to load BookMyShow pages for a while "
                "(likely blocked or the site changed). Check it manually."
            )
            meta["warned"] = True

    state["_meta"] = meta
    save_state(state)

    for msg in notifications:
        try:
            send_telegram(token, chat_id, msg)
        except Exception as e:
            print(f"[error] failed to send telegram message: {e}")
        time.sleep(1)

    print(f"Sent {len(notifications)} notification(s)." if notifications else "No changes detected.")


if __name__ == "__main__":
    main()
