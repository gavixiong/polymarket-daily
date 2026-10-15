#!/usr/bin/env python3
"""Render the day's Xiaohongshu cards (cards.html) to PNG files.

Usage:  python3 scripts/render_cards.py [YYYY-MM-DD]

Output: cards/<date>/01-cover.png ... and cards/<date>/index.json

Engine: Playwright (Chromium) if importable, otherwise a locally installed Chrome/Chromium
binary in headless mode. Both capture exactly the 1242x1660 card.
Which floating cards appear is decided here with the same thresholds cards.html uses.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
W, H = 1242, 1660
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]
NOISE = re.compile(r"price of \w+ be above|be above \$[\d,]+ on|Up or Down|temperature", re.I)
FIXED = ["cover", "headline", "breaking", "watch"]
FLOATING = ["whales", "resolved", "new", "feed"]


def eligible(key: str, day: dict) -> bool:
    cats = day["categories"]
    if key == "whales":
        w = day.get("whales") or []
        return len(w) >= 4 and w[0]["usd"] >= 50_000
    if key == "resolved":
        seen = set()
        rows = [r for c in ("politics", "finance", "tech") for r in cats[c]["resolved"]
                if not NOISE.search(r["question"]) and not (r["question"] in seen or seen.add(r["question"]))]
        return len(rows) >= 3
    if key == "new":
        posts = [p for p in day.get("feed", []) if p["kind"] == "new"]
        evs = [e for c in ("politics", "finance", "tech") for e in cats[c]["new"] if not NOISE.search(e["title"])]
        return len(posts) + len(evs) >= 3
    if key == "feed":
        return len([p for p in day.get("feed", []) if p["kind"] != "new" and p["lane"] != "other"]) >= 6
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = lambda *a, **k: Quiet(*a, directory=str(ROOT), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def render_playwright(base: str, date: str, keys: list[str], out: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for i, key in enumerate(keys, 1):
            pg.goto(f"{base}/cards.html?date={date}&card={key}", wait_until="networkidle")
            pg.wait_for_selector(".card .foot", timeout=15000)
            time.sleep(0.4)  # let fonts settle
            pg.locator(".card").first.screenshot(path=str(out / f"{i:02d}-{key}.png"))
        b.close()


def render_chrome(base: str, date: str, keys: list[str], out: Path, chrome: str) -> None:
    for i, key in enumerate(keys, 1):
        cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
               f"--window-size={W},{H}", f"--screenshot={out / f'{i:02d}-{key}.png'}", "--virtual-time-budget=5000",
               f"{base}/cards.html?date={date}&card={key}"]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else json.loads((ROOT / "data" / "index.json").read_text())["latest"]
    day = json.loads((ROOT / "data" / f"{date}.json").read_text())
    keys = FIXED + [k for k in FLOATING if eligible(k, day)]

    out = ROOT / "cards" / date
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    port = free_port()
    srv = serve(port)
    base = f"http://127.0.0.1:{port}"
    try:
        try:
            import playwright  # noqa: F401
            engine = "playwright"
            render_playwright(base, date, keys, out)
        except ImportError:
            chrome = next((c for c in CHROME_CANDIDATES if shutil.which(c) or os.path.exists(c)), None)
            if not chrome:
                print("[error] neither Playwright nor a Chrome binary is available", file=sys.stderr)
                return 1
            engine = "chrome"
            render_chrome(base, date, keys, out, chrome)
    finally:
        srv.shutdown()

    made = [{"key": k, "file": f"{i:02d}-{k}.png"} for i, k in enumerate(keys, 1) if (out / f"{i:02d}-{k}.png").exists()]
    (out / "index.json").write_text(json.dumps({"date": date, "engine": engine, "cards": made}, ensure_ascii=False, indent=1))
    print(f"[info] rendered {len(made)}/{len(keys)} cards with {engine} -> cards/{date}/", file=sys.stderr)
    return 0 if len(made) == len(keys) else 1


if __name__ == "__main__":
    sys.exit(main())
