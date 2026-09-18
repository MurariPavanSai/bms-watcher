import hashlib
import json
import os
import random
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_FILE = Path("state.json")
FAIL_THRESHOLD = 4  # ~2hrs of consecutive failures at a 30min schedule before we warn once

TARGETS = [
    {
        "key": "ALUC_0926",
        "label": "Allu Cinemas Kokapet - 26 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/allu-cinemas-kokapet/buytickets/ALUC/20260926",
        "date": "20260926",
    },
    {
        "key": "PRHN_0926",
        "label": "Prasads Multiplex - 26 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/prasads-multiplex-hyderabad/buytickets/PRHN/20260926",
        "date": "20260926",
    },
    {
        "key": "ALUC_0924",
        "label": "Allu Cinemas Kokapet - 24 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/allu-cinemas-kokapet/buytickets/ALUC/20260924",
        "date": "20260924",
    },
    {
        "key": "PRHN_0924",
        "label": "Prasads Multiplex - 24 Sep",
        "url": "https://in.bookmyshow.com/cinemas/HYD/prasads-multiplex-hyderabad/buytickets/PRHN/20260924",
        "date": "20260924",
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


def check(browser, target):
    # A fresh context per target matters: reusing one page across sequential
    # navigations leaves client-side state primed so the SPA stops redirecting
    # a not-yet-bookable date back to today, producing false "live" results.
    context, page = new_page(browser)
    try:
        page.goto(target["url"], wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1500)

        title = page.title()
        if "moment" in title.lower() or "checking" in title.lower():
            raise RuntimeError(f"blocked by challenge page (title={title!r})")

        final_url = page.url
        live = target["date"] in final_url

        text = ""
        if live:
            try:
                text = page.inner_text("main")
            except Exception:
                text = page.inner_text("body")

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

            if live and not prev.get("live"):
                notifications.append(f"Booking page is now LIVE for {target['label']}:\n{target['url']}")
            elif live and prev.get("live") and digest != prev.get("hash"):
                notifications.append(f"Booking page changed for {target['label']}:\n{target['url']}")

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
