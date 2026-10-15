#!/usr/bin/env python3
"""Pull one day's worth of Polymarket data for the daily digest.

Only the standard library is used so this runs anywhere with python3.

Outputs:
  data/YYYY-MM-DD.json   full structured data for that day
  data/index.json        list of available dates (newest first)
  stdout                 compact text summary for the digest writer
"""
from __future__ import annotations

import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG = ROOT / "config" / "watchlist.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
SITE = "https://polymarket.com"  # un-documented site endpoints: /api/biggest-movers, /breaking (X feed mirror)
NEWS_RSS = "https://news.polymarket.com/feed"

API_UA = "polymarket-daily-digest/1.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")

# Official "Breaking" board categories -> our digest group
OFFICIAL_CATEGORIES = {
    "politics": "politics", "world": "politics",
    "finance": "finance", "crypto": "finance",
    "tech": "tech", "culture": "culture",
}

# Lanes for the @Polymarket X feed (keyword first-match, order matters).
# This is a coarse pass for the dashboard filter; the digest writer re-curates by hand.
def _kw(*words: str) -> re.Pattern:
    """Whole-word, case-insensitive alternation ("NFL" must not match "coNFLict")."""
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


LANES = [
    ("other", _kw(
        "smuggl\\w*", "poison\\w*", "snakes?", "lizards?", "manure", "hazmat", "Animal of the Year", "cognitive test",
        "freaking genius", "toothbrush", "curry", "bizarre", "weird", "viral", "prank", "zoo", "sharks?", "alligators?",
        "bears?", "UFO", "aliens?", "salmon", "byproducts")),
    ("culture", _kw(
        "NFL", "NBA", "MLB", "NHL", "FIFA", "World Cup", "Super Bowl", "soccer", "football", "basketball", "tennis",
        "golf", "Olympics?", "UFC", "boxing", "coach", "quarterback", "Ballon d'Or", "Grammys?", "Oscars?", "Emmys?",
        "box office", "movie", "film", "trailer", "album", "Spotify", "concert", "MrBeast", "YouTube", "streamer",
        "celebrity", "Taylor Swift", "Rihanna", "Kardashian", "Netflix", "Rotten Tomatoes", "GTA", "video game",
        "Nintendo", "PlayStation", "Xbox", "Pokémon", "documentary", "49ers", "Cowboys", "Lakers", "Yankees")),
    ("tech", _kw(
        "AI", "OpenAI", "Anthropic", "Claude", "Gemini", "Google", "Apple", "iPhone", "Microsoft", "Nvidia", "Tesla",
        "SpaceX", "Starship", "xAI", "Grok", "Musk", "Amazon", "Meta", "TikTok", "robots?", "chips?", "semiconductors?",
        "smartphones?", "software", "apps?", "startups?", "data centers?", "GPUs?", "Walkman", "Sony", "Samsung",
        "Intel", "AMD", "crypto", "Bitcoin", "Ethereum", "tokens?", "blockchain", "quantum", "wearables?")),
    ("finance", _kw(
        "Fed", "Federal Reserve", "Powell", "Warsh", "rates?", "inflation", "CPI", "PPI", "GDP", "jobs report",
        "unemployment", "Treasury", "yields?", "bonds?", "stocks?", "S&P", "Nasdaq", "Dow", "market cap", "IPO",
        "earnings", "revenue", "banks?", "tariffs?", "oil", "OPEC", "gold", "dollar", "billion", "trillion",
        "recession", "economy", "economic", "trade deal", "shipments", "layoffs", "producer prices", "merger",
        "prices?", "gas prices")),
    ("politics", _kw(
        "Trump", "White House", "Congress", "Senate", "House", "Democrats?", "Democratic", "Republicans?", "GOP",
        "elections?", "votes?", "voters?", "polls?", "governor", "mayor", "president", "prime minister", "minister",
        "parliament", "government", "FBI", "DOJ", "Supreme Court", "court", "judge", "indict\\w*", "charged",
        "charges", "sanctions?", "ceasefire", "war", "military", "missiles?", "strikes?", "troops", "soldiers",
        "Iran", "Israel", "Gaza", "Hamas", "Houthis?", "Yemen", "Russia", "Ukraine", "Putin", "Zelensky", "China",
        "Xi", "Taiwan", "North Korea", "Kim Jong Un", "Venezuela", "Maduro", "Brazil", "Lula", "Bolsonaro",
        "Mexico", "Canada", "UK", "France", "Germany", "EU", "NATO", "UN", "immigration", "ICE", "border",
        "pardon", "impeach\\w*", "Pentagon", "Secretary", "Chief of Staff", "Press Secretary", "midterms?",
        "campaign", "Obama", "Harris", "Biden", "Navy", "police", "arrests?")),
]
LANE_LABELS = {"politics": "政经", "finance": "政经", "tech": "科技", "culture": "文体", "other": "猎奇与其他", "new": "新盘公告"}

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, no tzdata dependency

# Category -> Gamma tag ids. Verified 2026-10-15, see docs/api-research.md
CATEGORIES = {
    "politics": {"label": "时政", "tag_ids": [2, 100265]},  # Politics, Geopolitics
    "finance": {"label": "金融", "tag_ids": [100328, 120, 107, 235]},  # Economy, Finance, Business, Bitcoin
    "tech": {"label": "科技", "tag_ids": [1401, 439, 74]},  # Tech, AI, Science
}

# Markets that are noise for a politics / tech / finance digest.
NOISE = re.compile(
    r"Up or Down|temperature|\bvs\.?\b|Game \d|Map \d|shots on target|Spread:|O/U|"
    r"Over/Under|tweets|Truth Social posts|Will .* say \"|\bsay\b.*\btimes\b|What will .* say|"
    r"dozen eggs|Week of|first half|second half|halftime|\(BO\d\)|"
    # short-dated price ladders: "Bitcoin above ___ on October 15?", "price on October 15?",
    # "hit October 12-18?", "closes above ___ on October 15?"
    r"above ___ on|price on \w+ \d{1,2}\??$|hit on \w+ \d{1,2}\??$|hit \w+ \d{1,2}-\d{1,2}|closes above ___ on|"
    r"price of \w+ be above|be above \$[\d,]+ on",
    re.IGNORECASE,
)

# Thresholds
MOVER_MIN_CHANGE = 0.05       # 5 percentage points in 24h
MOVER_PRICE_RANGE = (0.03, 0.97)
MOVER_MIN_VOL24H = 5_000      # market-level 24h volume, USD
HOT_MIN_VOL24H = 20_000
RESOLVED_MIN_VOL = 2_000
WHALE_MIN_USD = 10_000
LOOKBACK_H = 24
RESOLVED_LOOKBACK_H = 36
PER_CATEGORY = {"movers": 12, "hot": 10, "new": 8, "resolved": 8}
SPARKLINE_TOP_MOVERS = 6
PAGES_PER_TAG = 2  # 100 events per page

_session_requests = 0


# ----------------------------------------------------------------------------- http
def get(url: str, params: dict | None = None, retries: int = 3, ua: str = API_UA, raw: bool = False):
    global _session_requests
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                _session_requests += 1
                time.sleep(0.2)
                body = r.read().decode("utf-8", errors="ignore")
                return body if raw else json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                print(f"[warn] giving up on {url}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace(" ", "T")
    if "." in s:  # trim sub-second precision Python may not like
        head, tail = s.split(".", 1)
        tz = ""
        for sep in ("+", "-", "Z"):
            if sep in tail:
                tz = tail[tail.index(sep):]
                break
        s = head + tz
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def prices(m: dict) -> list[float]:
    try:
        return [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return []


def is_noise(*texts: str) -> bool:
    return any(NOISE.search(t or "") for t in texts)


def fnum(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------------- shaping
def market_brief(m: dict) -> dict:
    p = prices(m)
    return {
        "id": m.get("id"),
        "question": m.get("question"),
        "label": m.get("groupItemTitle") or m.get("question"),
        "yes": p[0] if p else None,
        "change1d": m.get("oneDayPriceChange"),
        "change1w": m.get("oneWeekPriceChange"),
        "volume24h": fnum(m.get("volume24hr")),
        "volume": fnum(m.get("volume")),
        "liquidity": fnum(m.get("liquidity")),
        "endDate": m.get("endDate"),
        "conditionId": m.get("conditionId"),
        "yesTokenId": (json.loads(m.get("clobTokenIds") or "[]") or [None])[0],
        "slug": m.get("slug"),
    }


def event_brief(e: dict, category: str) -> dict:
    markets = [m for m in e.get("markets", []) if m.get("active") and not m.get("closed")]
    briefs = [market_brief(m) for m in markets]
    # Mutually exclusive outcomes sum to ~1: rank by probability. Threshold ladders
    # ("hit $90k", "by Dec 31") sum well above 1: the interesting rungs are near 50%.
    total = sum(b["yes"] or 0 for b in briefs)
    if len(briefs) > 1 and total > 1.5:
        near = sorted(briefs, key=lambda b: abs((b["yes"] or 0) - 0.5))[:4]
        leaders = sorted(near, key=lambda b: b["yes"] or 0, reverse=True)  # easiest rung first
    else:
        leaders = sorted(briefs, key=lambda b: b["yes"] or 0, reverse=True)
    for b in briefs:
        if b["label"] == b["question"]:
            b["label"] = "Yes"
    return {
        "id": e.get("id"),
        "slug": e.get("slug"),
        "title": e.get("title"),
        "url": f"https://polymarket.com/event/{e.get('slug')}",
        "category": category,
        "tags": [t.get("label") for t in e.get("tags", [])][:8],
        "volume24h": fnum(e.get("volume24hr")),
        "volume": fnum(e.get("volume")),
        "liquidity": fnum(e.get("liquidity")),
        "openInterest": fnum(e.get("openInterest")),
        "commentCount": e.get("commentCount", 0),
        "competitive": e.get("competitive"),
        "createdAt": e.get("createdAt"),
        "endDate": e.get("endDate"),
        "marketCount": len(markets),
        "leaders": leaders[:4],
        "_markets": markets,  # stripped before writing
    }


# ----------------------------------------------------------------------------- fetchers
def fetch_category_events(now: datetime) -> dict[str, dict]:
    """Return {event_id: event_brief} for all categories; an event gets its first matching category."""
    seen: dict[str, dict] = {}
    for cat, cfg in CATEGORIES.items():
        for tag in cfg["tag_ids"]:
            for page in range(PAGES_PER_TAG):
                rows = get(f"{GAMMA}/events", {
                    "tag_id": tag, "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false", "limit": 100, "offset": page * 100,
                }) or []
                for e in rows:
                    if e["id"] in seen or is_noise(e.get("title")):
                        continue
                    seen[e["id"]] = event_brief(e, cat)
                if len(rows) < 100:
                    break
            # newest events for this tag (may be beyond volume ranking)
            rows = get(f"{GAMMA}/events", {
                "tag_id": tag, "active": "true", "closed": "false",
                "order": "createdAt", "ascending": "false", "limit": 50,
            }) or []
            for e in rows:
                if e["id"] in seen or is_noise(e.get("title")):
                    continue
                seen[e["id"]] = event_brief(e, cat)
    return seen


def pick_movers(events: dict[str, dict], cat: str) -> list[dict]:
    out = []
    for ev in events.values():
        if ev["category"] != cat:
            continue
        best = None
        for m in ev["_markets"]:
            ch = m.get("oneDayPriceChange")
            if ch is None or abs(ch) < MOVER_MIN_CHANGE:
                continue
            p = prices(m)
            if not p or not (MOVER_PRICE_RANGE[0] <= p[0] <= MOVER_PRICE_RANGE[1]):
                continue
            if fnum(m.get("volume24hr")) < MOVER_MIN_VOL24H or is_noise(m.get("question")):
                continue
            if best is None or abs(ch) > abs(best.get("oneDayPriceChange") or 0):
                best = m
        if best:
            b = market_brief(best)
            if b["label"] == b["question"]:
                b["label"] = "Yes"
            out.append({**b, "eventTitle": ev["title"], "eventSlug": ev["slug"], "url": ev["url"],
                        "eventVolume24h": ev["volume24h"], "sparkline": None})
    out.sort(key=lambda x: abs(x["change1d"]), reverse=True)
    return out[: PER_CATEGORY["movers"]]


def pick_hot(events: dict[str, dict], cat: str) -> list[dict]:
    rows = [e for e in events.values() if e["category"] == cat and e["volume24h"] >= HOT_MIN_VOL24H]
    rows.sort(key=lambda e: e["volume24h"], reverse=True)
    return [strip(e) for e in rows[: PER_CATEGORY["hot"]]]


def pick_new(events: dict[str, dict], cat: str, now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=LOOKBACK_H)
    rows = []
    for e in events.values():
        if e["category"] != cat:
            continue
        created = parse_iso(e.get("createdAt"))
        if created and created >= cutoff:
            rows.append(e)
    rows.sort(key=lambda e: e["volume24h"], reverse=True)
    return [strip(e) for e in rows[: PER_CATEGORY["new"]]]


def fetch_resolved(cat: str, now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=RESOLVED_LOOKBACK_H)
    out, seen = [], set()
    for tag in CATEGORIES[cat]["tag_ids"]:
        rows = get(f"{GAMMA}/markets", {
            "closed": "true", "tag_id": tag, "order": "closedTime", "ascending": "false", "limit": 100,
        }) or []
        for m in rows:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            closed = parse_iso(m.get("closedTime"))
            if not closed or closed < cutoff:
                continue
            if fnum(m.get("volume")) < RESOLVED_MIN_VOL or is_noise(m.get("question")):
                continue
            p = prices(m)
            outcomes = json.loads(m.get("outcomes") or '["Yes","No"]')
            result = None
            if p:
                idx = max(range(len(p)), key=lambda i: p[i])
                result = outcomes[idx] if idx < len(outcomes) else None
            ev = (m.get("events") or [{}])[0]
            out.append({
                "question": m.get("question"),
                "result": result,
                "closedTime": m.get("closedTime"),
                "volume": fnum(m.get("volume")),
                "eventTitle": ev.get("title"),
                "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
            })
    out.sort(key=lambda x: x["volume"], reverse=True)
    return out[: PER_CATEGORY["resolved"]]


def fetch_whales(events: dict[str, dict], now: datetime) -> list[dict]:
    slugs = {e["slug"]: e for e in events.values()}
    cutoff_ts = (now - timedelta(hours=LOOKBACK_H)).timestamp()
    rows = get(f"{DATA_API}/trades", {
        "limit": 1000, "takerOnly": "true", "filterType": "CASH", "filterAmount": WHALE_MIN_USD,
    }) or []
    out = []
    for t in rows:
        if t.get("timestamp", 0) < cutoff_ts:
            continue
        ev = slugs.get(t.get("eventSlug"))
        if not ev or is_noise(t.get("title")):
            continue
        usd = fnum(t.get("size")) * fnum(t.get("price"))
        out.append({
            "usd": round(usd),
            "side": t.get("side"),
            "outcome": t.get("outcome"),
            "price": round(fnum(t.get("price")), 3),
            "trader": trader_name(t),
            "wallet": t.get("proxyWallet"),
            "title": t.get("title"),
            "eventTitle": ev["title"],
            "category": ev["category"],
            "url": ev["url"],
            "timestamp": t.get("timestamp"),
        })
    out.sort(key=lambda x: x["usd"], reverse=True)
    return out[:20]


def fetch_watchlist() -> list[dict]:
    if not CONFIG.exists():
        return []
    cfg = json.loads(CONFIG.read_text())
    out = []
    for item in cfg.get("events", []):
        rows = get(f"{GAMMA}/events", {"slug": item["slug"]}) or []
        if not rows:
            print(f"[warn] watchlist slug not found: {item['slug']}", file=sys.stderr)
            continue
        e = event_brief(rows[0], item.get("category", "politics"))
        e["note"] = item.get("note")
        e["leaders"] = e["leaders"][: item.get("show", 3)]
        out.append(strip(e))
    return out


def add_sparklines(movers: list[dict]) -> None:
    for mv in movers[:SPARKLINE_TOP_MOVERS]:
        if not mv.get("yesTokenId"):
            continue
        d = get(f"{CLOB}/prices-history", {"market": mv["yesTokenId"], "interval": "1w", "fidelity": 360})
        hist = (d or {}).get("history") or []
        mv["sparkline"] = [[h["t"], round(h["p"], 3)] for h in hist]


def trader_name(t: dict) -> str:
    name = t.get("name") or ""
    if not name or name.startswith("0x") or name.isdigit():
        name = t.get("pseudonym") or ""
    if not name or name.startswith("0x"):
        w = t.get("proxyWallet") or ""
        name = f"{w[:6]}…{w[-4:]}" if w else "匿名"
    return name


def strip(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------- official breaking board
def fetch_official_movers() -> dict[str, list[dict]]:
    """Polymarket's own 'Breaking' board (polymarket.com/breaking), per category."""
    out: dict[str, list[dict]] = {}
    for cat in OFFICIAL_CATEGORIES:
        d = get(f"{SITE}/api/biggest-movers", {"locale": "en", "category": cat}, ua=BROWSER_UA)
        rows = (d or {}).get("markets") or []
        items = []
        for m in rows:
            if is_noise(m.get("question")):
                continue
            ev = (m.get("events") or [{}])[0]
            hist = m.get("history") or []
            step = max(1, len(hist) // 24)
            raw_ids = m.get("clobTokenIds") or []
            try:
                ids = json.loads(raw_ids) if isinstance(raw_ids, str) else list(raw_ids)
            except (ValueError, TypeError):
                ids = []
            token = str(ids[0]) if ids else None
            items.append({
                "question": m.get("question"),
                "yes": m.get("currentPrice"),
                "change1d": m.get("oneDayPriceChange"),
                "closed": bool(m.get("closed")),
                "eventSlug": ev.get("slug"),
                "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
                "sparkline": [[h["t"], round(h["p"], 3)] for h in hist[::step]],
                "yesTokenId": token,
            })
        out[cat] = items
    # 7-day history for the three biggest moves outside culture: feeds the headline chart card
    top = sorted((m for c, ms in out.items() if c != "culture" for m in ms),
                 key=lambda m: abs(m["change1d"] or 0), reverse=True)
    seen = set()
    for m in top:
        if m["question"] in seen or not m.get("yesTokenId"):
            continue
        seen.add(m["question"])
        d = get(f"{CLOB}/prices-history", {"market": m["yesTokenId"], "interval": "1w", "fidelity": 120})
        m["history7d"] = [[h["t"], round(h["p"], 3)] for h in ((d or {}).get("history") or [])]
        if len(seen) >= 3:
            break
    return out


# ----------------------------------------------------------------------------- @Polymarket X feed (mirror on /breaking)
_POST_RE = re.compile(
    r'<a href="https://x\.com/polymarket/status/(\d+)"[^>]*>.*?font-medium">([^<]*)</p>'
    r'<div class="text-body-sm text-pk-content-secondary">([^<]*)</div>.*?break-words">(.*?)</p>',
    re.S,
)


def lane_for(text: str) -> str:
    for lane, rx in LANES:
        if rx.search(text):
            return lane
    return "other"


def fetch_x_feed(now: datetime) -> list[dict]:
    html_doc = get(f"{SITE}/breaking", ua=BROWSER_UA, raw=True) or ""
    posts = []
    for pid, kind, ts, body in _POST_RE.findall(html_doc):
        text = html.unescape(html.unescape(re.sub(r"<[^>]+>", "", body))).strip()  # source is double-escaped
        text = re.sub(r"\s*\n\s*", " / ", text)
        # SSR timestamps are UTC, e.g. "Oct 15, 4:23 PM"; year is the current one
        when = None
        try:
            when = datetime.strptime(f"{ts.strip()} {now.year}", "%b %d, %I:%M %p %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        if when and when > now + timedelta(days=1):  # year rollover
            when = when.replace(year=now.year - 1)
        if when and when < now - timedelta(hours=LOOKBACK_H + 2):
            continue
        is_new = kind.lower().startswith("new")
        posts.append({
            "id": pid,
            "url": f"https://x.com/polymarket/status/{pid}",
            "kind": "new" if is_new else "breaking",
            "lane": "new" if is_new else lane_for(text),
            "time": when.astimezone(TZ).strftime("%m-%d %H:%M") if when else ts,
            "timestamp": int(when.timestamp()) if when else None,
            "text": text,
            "odds": bool(re.search(r"\d+%\s*chance", text, re.I)),
        })
    posts.sort(key=lambda p: p["timestamp"] or 0, reverse=True)
    return posts


# ----------------------------------------------------------------------------- Polymarket's own column (RSS)
def fetch_column(now: datetime) -> list[dict]:
    xml = get(NEWS_RSS, ua=BROWSER_UA, raw=True) or ""
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S)[:5]:
        def grab(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
            return html.unescape(m.group(1).strip()) if m else None
        pub = grab("pubDate")
        try:
            when = datetime.strptime(pub.replace(" GMT", " +0000"), "%a, %d %b %Y %H:%M:%S %z") if pub else None
        except ValueError:
            when = None
        out.append({
            "title": grab("title"),
            "link": grab("link"),
            "date": when.astimezone(TZ).strftime("%Y-%m-%d") if when else pub,
            "fresh": bool(when and when > now - timedelta(hours=LOOKBACK_H + 2)),
            "summary": re.sub(r"<[^>]+>", "", grab("description") or "")[:200],
        })
    return out


# ----------------------------------------------------------------------------- ranked candidates for the short post
REPORTS = ROOT / "reports"
MENTION_DAYS = 7
MENTION_MIN_MOVE = 0.10
PICKS_MAX = 14
PER_EVENT_MAX = 2
PER_DOMAIN_MAX = 5
# recurring daily markets ("... on October 14, 2026?") resolve every day; never news on their own
DAILY_RE = re.compile(r"\bon (January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}(, \d{4})?\?$")
# nouns too common to link a market to a news post by themselves
WEAK_NOUNS = {"Trump", "Iran", "Israel", "Russia", "Ukraine", "China", "United", "States", "America", "American",
              "Democratic", "Republican", "Anthropic", "Google", "OpenAI", "Bitcoin", "Federal", "Reserve",
              "Congress", "Court", "Supreme", "Biden", "Harris", "Election", "Party", "Government", "Military",
              "Treasury", "Senate", "House", "White", "Republicans", "Democrats", "Europe", "European"}
_STOP = {"Will", "What", "Which", "Who", "When", "Where", "How", "Yes", "The", "October", "November", "December",
         "January", "February", "March", "April", "May", "June", "July", "August", "September", "Before", "After",
         "Next", "Another", "Does", "Did", "Does", "Market", "President", "Senate", "House"}


def recent_mentions(today: str) -> dict[str, dict]:
    """Questions used in the short post over the last MENTION_DAYS days, from reports/<date>.cards.json 'picked'."""
    out = {}
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=MENTION_DAYS)).strftime("%Y-%m-%d")
    for f in sorted(REPORTS.glob("????-??-??.cards.json")):
        d = f.name[:10]
        if d >= today or d < cutoff:
            continue
        try:
            for p in json.loads(f.read_text()).get("picked", []):
                if p.get("question"):
                    out[p["question"]] = {"date": d, "yes": p.get("yes")}
        except (ValueError, OSError):
            continue
    return out


def proper_nouns(text: str) -> set[str]:
    return {w.strip("'’?,.:;()\"") for w in re.findall(r"\b[A-Z][A-Za-z\-]{3,}\b", text or "")} - _STOP


def ladder_summary(ev: dict | None) -> dict | None:
    """For threshold ladders, the rungs nearest 50% are the market's median estimate."""
    if not ev or ev.get("marketCount", 0) < 2:
        return None
    leaders = ev.get("leaders") or []
    total = sum(l["yes"] or 0 for l in leaders) if leaders else 0
    if ev.get("marketCount", 0) > 1 and total > 1.5:
        return {"type": "ladder", "rungs": [{"label": l["label"], "yes": l["yes"]} for l in leaders[:4]]}
    return {"type": "exclusive", "rungs": [{"label": l["label"], "yes": l["yes"]} for l in leaders[:3]]}


def build_picks(day: dict, events: dict[str, dict]) -> list[dict]:
    by_slug = {e["slug"]: e for e in events.values()}
    prev = recent_mentions(day["date"])
    feed = day.get("feed") or []
    cands: dict[str, dict] = {}

    def add(kind, question, yes, change, vol, domain, ev, closed=False, label=None, event_title=None, points=None):
        if not question or is_noise(question) or DAILY_RE.search(question):
            return
        if closed and (yes or 0) < 0.5:
            return  # a rung / market resolving No on its own is never news
        change = change or 0
        # A market listed today starts at 50%; its first move is not a move.
        new_market = (points is not None and points < 12 and yes is not None and abs((yes - change) - 0.5) < 0.02)
        if kind == "resolved":
            surprise = 0.15  # we do not know how surprised the market was; keep these as fillers
        elif new_market:
            surprise = 0.08
        else:
            surprise = abs(change) + (0.3 if closed else 0)
        attention = max(1.0, math.log10(max(vol or 0, 1000)) - 2)  # $1k -> 1, $1M -> 4
        mention = prev.get(question)
        repeated = bool(mention and yes is not None and mention.get("yes") is not None
                        and abs(yes - mention["yes"]) < MENTION_MIN_MOVE)
        fresh = 0.2 if repeated else 1.0
        score = round(surprise * attention * fresh, 3)
        nouns = proper_nouns(question) | (proper_nouns(event_title or "") if event_title else set())
        strong = nouns - WEAK_NOUNS
        causes = []
        for p in feed:
            if p["kind"] != "breaking":
                continue
            pn = proper_nouns(p["text"])
            if (strong & pn) or len(nouns & pn) >= 2:
                causes.append({"time": p["time"], "text": p["text"], "url": p["url"]})
            if len(causes) >= 2:
                break
        c = {
            "kind": kind, "domain": domain, "question": question, "label": label,
            "eventTitle": event_title or (ev["title"] if ev else None), "eventSlug": ev["slug"] if ev else None,
            "yes": yes, "change1d": change,
            "from": round(yes - change, 3) if (yes is not None and kind != "resolved") else None,
            "closed": closed, "volume24h": vol, "score": score, "newMarket": new_market,
            "repeated": repeated, "lastMention": mention["date"] if mention else None,
            "siblings": ladder_summary(ev), "causes": causes,
            "url": ev["url"] if ev else None,
        }
        if question not in cands or cands[question]["score"] < score:
            cands[question] = c

    for cat, ms in (day.get("official") or {}).items():
        if cat == "culture":
            continue
        for m in ms:
            ev = by_slug.get(m.get("eventSlug"))
            add("official", m["question"], m["yes"], m["change1d"], ev["volume24h"] if ev else 10_000,
                OFFICIAL_CATEGORIES[cat], ev, m["closed"], points=len(m.get("sparkline") or []))
    for cat in CATEGORIES:
        for m in day["categories"][cat]["movers"]:
            add("mover", m["question"], m["yes"], m["change1d"], m.get("eventVolume24h"), cat,
                by_slug.get(m.get("eventSlug")), False, m["label"], m["eventTitle"])
        for e in day["categories"][cat]["hot"]:
            for l in e["leaders"][:3]:
                if l.get("change1d") and abs(l["change1d"]) >= 0.05:
                    add("hot", l["question"], l["yes"], l["change1d"], e["volume24h"], cat,
                        by_slug.get(e["slug"]), False, l["label"], e["title"])
        for r in day["categories"][cat]["resolved"]:
            yes = 1.0 if str(r.get("result")).lower() == "yes" else 0.0
            add("resolved", r["question"], yes, None, r["volume"], cat, None, True, None, r.get("eventTitle"))

    ranked = sorted(cands.values(), key=lambda c: c["score"], reverse=True)
    per_event, per_domain, out = {}, {}, []
    for c in ranked:
        ek = c["eventSlug"] or c["question"]
        is_ladder = bool(c["siblings"] and c["siblings"]["type"] == "ladder")
        if per_event.get(ek, 0) >= (1 if is_ladder else PER_EVENT_MAX) or per_domain.get(c["domain"], 0) >= PER_DOMAIN_MAX:
            continue
        per_event[ek] = per_event.get(ek, 0) + 1
        per_domain[c["domain"]] = per_domain.get(c["domain"], 0) + 1
        c["rank"] = len(out) + 1
        out.append(c)
        if len(out) >= PICKS_MAX:
            break
    return out


# ----------------------------------------------------------------------------- summary
def pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def delta(x) -> str:
    return "" if x is None else f"{x * 100:+.0f}pt"


def usd(x: float) -> str:
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


def text_summary(day: dict) -> str:
    L = [f"# Polymarket 数据摘要 {day['date']}（生成于 {day['generatedAt']}）", ""]

    L.append("## 候选条目（已打分、去重、限额；小红书正文从这里选 4–10 条）")
    L.append("格式：排名 [分数] 类型/领域  从→到  标题 | 事件 | 24h 成交 | 备注")
    for p in day.get("picks", []):
        frm = pct(p["from"]) if p["from"] is not None else "—"
        title = p["label"] if p["label"] and p["label"] not in ("Yes", p["question"]) else p["question"]
        notes = []
        if p["closed"]:
            notes.append("已结算")
        if p.get("newMarket"):
            notes.append("今天新上线的档位，起点 50% 是默认值，不算变动")
        if p["repeated"]:
            notes.append(f"7 天内报过（{p['lastMention']}），数值没怎么变，慎用")
        elif p["lastMention"]:
            notes.append(f"上次报 {p['lastMention']}，这次变了")
        if p["siblings"] and p["siblings"]["type"] == "ladder":
            notes.append("梯子市场，整体看：" + "; ".join(f"{r['label']} {pct(r['yes'])}" for r in p["siblings"]["rungs"]))
        elif p["siblings"]:
            notes.append("同事件其他选项：" + "; ".join(f"{r['label']} {pct(r['yes'])}" for r in p["siblings"]["rungs"]))
        L.append(f"{p['rank']:>2}. [{p['score']:.2f}] {p['kind']}/{p['domain']}  {frm}→{pct(p['yes'])}  {title}"
                 + (f" | {p['eventTitle']}" if p["eventTitle"] and p["eventTitle"] != title else "")
                 + (f" | {usd(p['volume24h'])}" if p["volume24h"] else "")
                 + (f" | {'；'.join(notes)}" if notes else ""))
        for c in p["causes"]:
            L.append(f"      ↳ 可能的原因（快讯 {c['time'][6:]}）：{c['text'][:160]}")
    L.append("")

    L.append("## 官方突发榜（polymarket.com/breaking，24h 变动最大）")
    groups = {"politics": "时政与全球", "finance": "金融与加密", "tech": "科技", "culture": "文化体育"}
    for grp, label in groups.items():
        rows = [m for cat, g in OFFICIAL_CATEGORIES.items() if g == grp for m in day["official"].get(cat, [])]
        seen, uniq = set(), []
        for m in sorted(rows, key=lambda m: abs(m["change1d"] or 0), reverse=True):
            if m["question"] in seen:
                continue
            seen.add(m["question"])
            uniq.append(m)
        L.append(f"### {label}")
        for m in uniq[:8]:
            tag = "（已结算）" if m["closed"] else ""
            L.append(f"- {delta(m['change1d'])} → {pct(m['yes'])}  {m['question']}{tag}")
    L.append("")

    L.append("## @Polymarket 快讯（24h，按道分组，时间为北京时间）")
    for lane, label in [("politics", "政经"), ("finance", "政经·财经"), ("tech", "科技"), ("culture", "文体"), ("other", "猎奇与其他")]:
        rows = [p for p in day["feed"] if p["lane"] == lane]
        if not rows:
            continue
        L.append(f"### {label}（{len(rows)} 条）")
        for p in rows:
            L.append(f"- {p['time']}  {p['text']}")
    news_posts = [p for p in day["feed"] if p["kind"] == "new"]
    if news_posts:
        L.append("### 官方新盘公告")
        for p in news_posts:
            L.append(f"- {p['time']}  {p['text']}")
    L.append("")
    fresh = [c for c in day["column"] if c["fresh"]]
    if fresh:
        L.append("## Polymarket 专栏新文章（news.polymarket.com）")
        for c in fresh:
            L.append(f"- {c['date']}  {c['title']}  {c['link']}")
        L.append("")

    for cat, cfg in CATEGORIES.items():
        c = day["categories"][cat]
        L.append(f"## {cfg['label']}")
        L.append("### 24h 最大变动")
        for m in c["movers"]:
            L.append(f"- {delta(m['change1d'])} → {pct(m['yes'])}  [{m['eventTitle']}] {m['label']}  (24h vol {usd(m['volume24h'])})")
        L.append("### 24h 热门")
        for e in c["hot"]:
            lead = "; ".join(f"{l['label']} {pct(l['yes'])}{(' ' + delta(l['change1d'])) if l['change1d'] else ''}" for l in e["leaders"][:3])
            L.append(f"- {usd(e['volume24h'])}  {e['title']}  → {lead}")
        if c["new"]:
            L.append("### 24h 新上线")
            for e in c["new"]:
                lead = "; ".join(f"{l['label']} {pct(l['yes'])}" for l in e["leaders"][:3])
                L.append(f"- {e['title']}  → {lead}")
        if c["resolved"]:
            L.append("### 刚结算")
            for r in c["resolved"]:
                L.append(f"- 结果 {r['result']}: {r['question']}  (vol {usd(r['volume'])})")
        L.append("")
    if day["whales"]:
        L.append("## 大单（≥$10k，24h）")
        for w in day["whales"][:12]:
            L.append(f"- {usd(w['usd'])} {w['side']} {w['outcome']} @{w['price']:.2f}  {w['trader']}  | {w['title']}")
        L.append("")
    if day["watchlist"]:
        L.append("## 固定关注池")
        for e in day["watchlist"]:
            lead = "; ".join(f"{l['label']} {pct(l['yes'])}{(' ' + delta(l['change1d'])) if l['change1d'] else ''}" for l in e["leaders"])
            L.append(f"- {e['title']} → {lead}")
    return "\n".join(L)


# ----------------------------------------------------------------------------- main
def main() -> int:
    now = datetime.now(timezone.utc)
    date = now.astimezone(TZ).strftime("%Y-%m-%d")
    if len(sys.argv) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", sys.argv[1]):
        date = sys.argv[1]

    print(f"[info] fetching events for {date} ...", file=sys.stderr)
    events = fetch_category_events(now)
    print(f"[info] {len(events)} candidate events after noise filter", file=sys.stderr)

    categories = {}
    for cat in CATEGORIES:
        movers = pick_movers(events, cat)
        add_sparklines(movers)
        categories[cat] = {
            "label": CATEGORIES[cat]["label"],
            "movers": movers,
            "hot": pick_hot(events, cat),
            "new": pick_new(events, cat, now),
            "resolved": fetch_resolved(cat, now),
        }
        print(f"[info] {cat}: movers={len(movers)} hot={len(categories[cat]['hot'])} "
              f"new={len(categories[cat]['new'])} resolved={len(categories[cat]['resolved'])}", file=sys.stderr)

    official = fetch_official_movers()
    feed = fetch_x_feed(now)
    column = fetch_column(now)
    print(f"[info] official movers: " + ", ".join(f"{k}={len(v)}" for k, v in official.items()), file=sys.stderr)
    lanes = {}
    for p in feed:
        lanes[p["lane"]] = lanes.get(p["lane"], 0) + 1
    print(f"[info] x feed: {len(feed)} posts, lanes={lanes}", file=sys.stderr)

    day = {
        "date": date,
        "generatedAt": now.astimezone(TZ).strftime("%Y-%m-%d %H:%M %z"),
        "stats": {
            "eventsScanned": len(events),
            "feedPosts": len(feed),
            "requests": None,
        },
        "categories": categories,
        "official": official,
        "feed": feed,
        "column": column,
        "whales": fetch_whales(events, now),
        "watchlist": fetch_watchlist(),
    }
    day["picks"] = build_picks(day, events)
    print(f"[info] picks: {len(day['picks'])} ranked candidates "
          f"({sum(1 for p in day['picks'] if p['repeated'])} repeated from last {MENTION_DAYS} days)", file=sys.stderr)
    day["stats"]["requests"] = _session_requests

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date}.json").write_text(json.dumps(day, ensure_ascii=False, indent=1))
    dates = sorted({p.stem for p in DATA_DIR.glob("????-??-??.json")}, reverse=True)
    (DATA_DIR / "index.json").write_text(json.dumps({"dates": dates, "latest": dates[0]}, indent=1))

    summary = text_summary(day)
    (DATA_DIR / "latest-summary.txt").write_text(summary)
    print(summary)
    print(f"\n[info] wrote data/{date}.json ({_session_requests} requests)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
