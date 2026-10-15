#!/usr/bin/env python3
"""Render the day's Xiaohongshu cards (cards.html) to PNG.

    python3 scripts/render_cards.py [YYYY-MM-DD]

Output: cards/<date>/01-cover.png ... and cards/<date>/index.json
Engine: Playwright Chromium if importable, else a local Chrome/Chromium binary in headless mode.
Which floating cards appear is decided here with the same thresholds cards.html uses.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
W, H = 1242, 1660
FIXED = ["cover", "headline", "breaking", "hot"]
FLOATING = ["resolved", "new"]   # the feed card stays dashboard-only: English headlines, already digested in the post
CHROME = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
          "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def eligible(key: str, day: dict) -> bool:
    domains = ("politics", "finance", "tech")
    if key == "resolved":
        return len({r["question"] for d in domains for r in day["resolved"][d]}) >= 3
    if key == "new":
        return sum(1 for p in day["feed"] if p["kind"] == "new") + sum(len(day["new"][d]) for d in domains) >= 3
    if key == "feed":
        return sum(1 for p in day["feed"] if p["kind"] != "new" and p["lane"] != "other") >= 6
    return True


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), lambda *a, **k: Quiet(*a, directory=str(ROOT), **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def render_playwright(base: str, date: str, keys: list[str], out: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        for i, key in enumerate(keys, 1):
            pg.goto(f"{base}/cards.html?date={date}&card={key}", wait_until="networkidle")
            pg.wait_for_selector(".card .foot", timeout=15000)
            time.sleep(0.4)
            pg.locator(".card").first.screenshot(path=str(out / f"{i:02d}-{key}.png"))
        b.close()


def render_chrome(base: str, date: str, keys: list[str], out: Path, chrome: str) -> None:
    for i, key in enumerate(keys, 1):
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                        f"--window-size={W},{H}", f"--screenshot={out / f'{i:02d}-{key}.png'}", "--virtual-time-budget=5000",
                        f"{base}/cards.html?date={date}&card={key}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else json.loads((ROOT / "data" / "index.json").read_text())["latest"]
    day = json.loads((ROOT / "data" / f"{date}.json").read_text())
    keys = FIXED + [k for k in FLOATING if eligible(k, day)]
    out = ROOT / "cards" / date
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    srv, base = serve()
    try:
        try:
            import playwright  # noqa: F401
            engine = "playwright"
            render_playwright(base, date, keys, out)
        except ImportError:
            chrome = next((c for c in CHROME if shutil.which(c) or os.path.exists(c)), None)
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
