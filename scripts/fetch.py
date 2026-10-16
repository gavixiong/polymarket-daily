#!/usr/bin/env python3
"""Collect one day of Polymarket data for the digest. Standard library only.

    python3 scripts/fetch.py [YYYY-MM-DD]

Writes  data/<date>.json   structured data (schema below)
        data/index.json    available dates
        data/latest-summary.txt  the text summary also printed to stdout

Schema of data/<date>.json
  date, generatedAt, stats
  official  {category: [market]}   Polymarket's own "Breaking" board, 24h biggest movers
  feed      [post]                 @Polymarket X posts mirrored on /breaking (internal: causes, odds posts, new-market posts)
  column    [article]              news.polymarket.com RSS
  hot       {domain: [event]}      top events by 24h volume; `pinned` marks the rule-selected long-running panel
  new       {domain: [event]}      events created in the last 24h
  resolved  {domain: [market]}     markets closed in the last 36h
  flows     [market]               net money by market from >= $10k trades
  picks     [candidate]            scored, de-duplicated candidates for the short post

Sections: config · http · shaping · sources · scoring · summary · main
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

# ============================================================================ config
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PINS_FILE = ROOT / "config" / "pins.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
SITE = "https://polymarket.com"            # undocumented: /api/biggest-movers, /breaking
NEWS_RSS = "https://news.polymarket.com/feed"

API_UA = "polymarket-daily-digest/1.0"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")

TZ = timezone(timedelta(hours=8))          # Beijing, no tzdata dependency

# Digest domains -> Gamma tag ids (verified 2026-10-15, see docs/api-research.md)
DOMAINS = {
    "politics": {"label": "时政", "tag_ids": [2, 100265]},            # Politics, Geopolitics
    "finance": {"label": "金融", "tag_ids": [100328, 120, 107, 235]},  # Economy, Finance, Business, Bitcoin
    "tech": {"label": "科技", "tag_ids": [1401, 439, 74]},             # Tech, AI, Science
}
# Official "Breaking" board categories -> digest domain
OFFICIAL_CATEGORIES = {"politics": "politics", "world": "politics", "finance": "finance",
                       "crypto": "finance", "tech": "tech", "culture": "culture"}

# Markets that are noise for a politics / finance / tech digest
NOISE = re.compile(
    r"Up or Down|temperature|\bvs\.?\b|Game \d|Map \d|shots on target|Spread:|O/U|Over/Under|"
    r"tweets|Truth Social posts|Will .* say \"|\bsay\b.*\btimes\b|What will .* say|dozen eggs|Week of|"
    r"first half|second half|halftime|\(BO\d\)|"
    r"above ___ on|price on \w+ \d{1,2}\??$|hit on \w+ \d{1,2}\??$|hit \w+ \d{1,2}-\d{1,2}|closes above ___ on|"
    r"price of \w+ be above|be above \$[\d,]+ on|price of \w+ be between",
    re.I)
# Outcome labels that are thresholds (dates, prices, arrows) rather than alternatives
THRESHOLD_LABEL = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}(, \d{4})?$|"
    r"^[↑↓<>]|^\$|^Below\b|^Above\b|\bor more\b|^Q[1-4] \d{4}$|^\d{4}$", re.I)
# Recurring daily markets ("... on October 14, 2026?") resolve every day; never news on their own
DAILY = re.compile(r"\bon (January|February|March|April|May|June|July|August|September|October|November|December)"
                   r" \d{1,2}(, \d{4})?\?$")

# Thresholds
LOOKBACK_H = 24
RESOLVED_LOOKBACK_H = 36
HOT_PER_DOMAIN = 10
PERSIST_DAYS = 7           # look-back window for the long-running panel
PERSIST_MIN_SHARE = 0.7    # must be in a domain top-10 on 70% of those days
PERSIST_MAX = 5            # rule-selected long-running events (plus any `always` pins)
NEW_PER_DOMAIN = 8
RESOLVED_PER_DOMAIN = 8
HOT_MIN_VOL24H = 20_000
RESOLVED_MIN_VOL = 2_000
MOVER_MIN_CHANGE = 0.05
MOVER_PRICE_RANGE = (0.03, 0.97)
MOVER_MIN_VOL24H = 5_000
WHALE_MIN_USD = 10_000
FLOW_PRICE_RANGE = (0.10, 0.90)
PAGES_PER_TAG = 2

# Scoring for the short post
PICKS_MAX = 14
PER_EVENT_MAX = 2
PER_DOMAIN_MAX = 5
CULTURE_MAX = 2          # sports / film / music: at most two, always at the end of the post
MENTION_DAYS = 7
MENTION_MIN_MOVE = 0.10
WEAK_NOUNS = {"Trump", "Iran", "Israel", "Russia", "Ukraine", "China", "United", "States", "America", "American",
              "Democratic", "Republican", "Anthropic", "Google", "OpenAI", "Bitcoin", "Federal", "Reserve",
              "Congress", "Court", "Supreme", "Biden", "Harris", "Election", "Party", "Government", "Military",
              "Treasury", "Senate", "House", "White", "Republicans", "Democrats", "Europe", "European"}
STOP_NOUNS = {"Will", "What", "Which", "Who", "When", "Where", "How", "Yes", "The", "Before", "After", "Next",
              "Another", "Does", "Did", "Market", "President", "January", "February", "March", "April", "May",
              "June", "July", "August", "September", "October", "November", "December"}

_requests = 0


# ============================================================================ http
def get(url: str, params: dict | None = None, *, ua: str = API_UA, raw: bool = False, retries: int = 3):
    global _requests
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                _requests += 1
                time.sleep(0.2)
                body = r.read().decode("utf-8", errors="ignore")
                return body if raw else json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                print(f"[warn] giving up on {url}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))


# ============================================================================ shaping
def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace(" ", "T")
    s = re.sub(r"\.\d+", "", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def fnum(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def prices(m: dict) -> list[float]:
    try:
        return [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return []


def is_noise(*texts: str | None) -> bool:
    return any(NOISE.search(t or "") for t in texts)


def outcome(m: dict) -> dict:
    """One market inside an event, as an outcome row."""
    p = prices(m)
    label = m.get("groupItemTitle") or m.get("question")
    return {
        "question": m.get("question"),
        "label": "Yes" if label == m.get("question") else label,
        "yes": p[0] if p else None,
        "change1d": m.get("oneDayPriceChange"),
        "volume24h": fnum(m.get("volume24hr")),
    }


def event_brief(e: dict, domain: str) -> dict:
    """An event with its outcomes classified as binary / exclusive / ladder.

    exclusive: outcomes are alternatives and sum to ~1 (who wins) -> show as shares
    ladder:    outcomes are thresholds and sum well above 1 (by which date / above which price)
    """
    markets = [m for m in e.get("markets", []) if m.get("active") and not m.get("closed")]
    rows = [outcome(m) for m in markets]
    total = sum(r["yes"] or 0 for r in rows)
    threshold_labels = sum(bool(THRESHOLD_LABEL.search(r["label"] or "")) for r in rows)
    if len(rows) <= 1:
        kind = "binary"
    elif total > 1.5 or threshold_labels * 2 >= len(rows):
        kind = "ladder"
    else:
        kind = "exclusive"
    all_rows = rows
    if kind == "ladder":  # the informative rungs sit near 50%; the 99% and 1% ends say nothing
        rows = sorted(rows, key=lambda r: abs((r["yes"] or 0) - 0.5))[:6]
    rows = sorted(rows, key=lambda r: r["yes"] or 0, reverse=True)
    return {
        "id": e.get("id"), "slug": e.get("slug"), "title": e.get("title"),
        "url": f"https://polymarket.com/event/{e.get('slug')}",
        "domain": domain, "kind": kind,
        "volume24h": fnum(e.get("volume24hr")), "volume1wk": fnum(e.get("volume1wk")), "volume": fnum(e.get("volume")),
        "createdAt": e.get("createdAt"), "endDate": e.get("endDate"),
        "outcomeCount": len(rows),
        "outcomes": rows[:6],
        "_all": all_rows,
    }


def public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def delta(x) -> str:
    return "" if not x else f"{x * 100:+.0f}pt"


def la_offset(utc: datetime) -> timedelta:
    """America/Los_Angeles offset without tzdata: PDT (UTC-7) from the 2nd Sunday of March to the 1st Sunday of November."""
    y = utc.year
    march = datetime(y, 3, 1, tzinfo=timezone.utc)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7 + 7, hours=10)      # 2nd Sunday 02:00 PST
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=9)                 # 1st Sunday 02:00 PDT
    return timedelta(hours=-7) if dst_start <= utc < dst_end else timedelta(hours=-8)


def stamp(utc: datetime) -> str:
    bj = utc.astimezone(TZ)
    la = utc.astimezone(timezone(la_offset(utc)))
    return f"数据截至 北京 {bj.month}/{bj.day} {bj:%H:%M} · 加州 {la.month}/{la.day} {la:%H:%M}"


def usd(x: float) -> str:
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


# ============================================================================ sources
def fetch_events() -> dict[str, dict]:
    """All active events under the digest tags. First matching domain wins."""
    seen: dict[str, dict] = {}
    for domain, cfg in DOMAINS.items():
        for tag in cfg["tag_ids"]:
            for order, limit, pages in (("volume24hr", 100, PAGES_PER_TAG), ("createdAt", 50, 1)):
                for page in range(pages):
                    rows = get(f"{GAMMA}/events", {"tag_id": tag, "active": "true", "closed": "false",
                                                   "order": order, "ascending": "false",
                                                   "limit": limit, "offset": page * limit}) or []
                    for e in rows:
                        if e["id"] not in seen and not is_noise(e.get("title")):
                            seen[e["id"]] = event_brief(e, domain)
                    if len(rows) < limit:
                        break
    return seen


def slug_of(s: str) -> str:
    """Accept a bare slug or a polymarket.com/event/<slug> URL."""
    return s.rstrip("/").split("/event/")[-1].split("?")[0]


def load_pins() -> tuple[set[str], set[str]]:
    """config/pins.json: {"always": [...], "never": [...]} — both optional, URLs or slugs."""
    try:
        cfg = json.loads(PINS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return set(), set()
    return {slug_of(x) for x in cfg.get("always", [])}, {slug_of(x) for x in cfg.get("never", [])}


def persistence(today: str) -> tuple[dict[str, int], int]:
    """How many of the previous PERSIST_DAYS-1 days each event sat in its domain's volume top-10.

    Read from the archived data/<date>.json files, so no extra state. Returns (counts, days_seen).
    """
    counts: dict[str, int] = {}
    days = 0
    for f in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        if f.name[:10] >= today:
            continue
        try:
            hot = json.loads(f.read_text()).get("hot")
        except (ValueError, OSError):
            continue
        if not hot:
            continue
        days += 1
        for evs in hot.values():
            for e in evs:
                rank = e.get("volRank")
                if rank is not None and rank <= 10:
                    counts[e["slug"]] = counts.get(e["slug"], 0) + 1
        if days >= PERSIST_DAYS - 1:
            break
    return counts, days


def hot_panel(events: dict[str, dict], today: str) -> dict[str, list[dict]]:
    """Top events by 24h volume per domain. The long-running panel ("pinned") is rule-based:
    events that have been in a top-10 on at least 70% of the last 7 days, best 5 by persistence then volume.
    Nothing about end dates: a big event stays until it resolves, then drops out through the resolved card.
    config/pins.json `always` forces inclusion, `never` forces exclusion."""
    counts, hist_days = persistence(today)
    always, never = load_pins()
    # Bootstrap: with under three days of history every top-10 event qualifies; the volume tiebreak decides.
    need = 1 if hist_days < 2 else max(2, math.ceil(PERSIST_MIN_SHARE * (hist_days + 1)))

    ranked: dict[str, list[dict]] = {}
    for domain in DOMAINS:
        lst = sorted((e for e in events.values() if e["domain"] == domain and e["slug"] not in never
                      and e["volume24h"] >= HOT_MIN_VOL24H), key=lambda e: e["volume24h"], reverse=True)
        for i, e in enumerate(lst, 1):
            e["volRank"] = i
            e["persistDays"] = counts.get(e["slug"], 0) + (1 if i <= 10 else 0)
        ranked[domain] = lst

    # forced events that are not in today's pool at all
    by_slug = {e["slug"]: e for e in events.values()}
    for slug in always - set(by_slug):
        rows = get(f"{GAMMA}/events", {"slug": slug}) or []
        if rows:
            e = event_brief(rows[0], "politics")
            e["volRank"], e["persistDays"] = None, counts.get(slug, 0)
            ranked[e["domain"]].insert(0, e)
            by_slug[slug] = e
        else:
            print(f"[warn] always-pin not found: {slug}", file=sys.stderr)

    cands = [e for lst in ranked.values() for e in lst[:10]
             if e["persistDays"] >= need or e["slug"] in always]
    cands.sort(key=lambda e: (e["slug"] in always, e["persistDays"], e["volume24h"]), reverse=True)
    pinned = {e["slug"] for e in cands[:PERSIST_MAX + len(always)]}
    print(f"[info] long-running panel: {len(pinned)} events (history {hist_days} days, need {need})", file=sys.stderr)

    out = {}
    for domain, lst in ranked.items():
        head = [e for e in lst if e["slug"] in pinned]
        tail = [e for e in lst if e["slug"] not in pinned]
        for e in head:
            e["pinned"] = True
        out[domain] = [public(e) for e in (head + tail)[:HOT_PER_DOMAIN]]
    return out


def new_panel(events: dict[str, dict], now: datetime) -> dict[str, list[dict]]:
    cutoff = now - timedelta(hours=LOOKBACK_H)
    out = {}
    for domain in DOMAINS:
        rows = [e for e in events.values() if e["domain"] == domain
                and (parse_iso(e.get("createdAt")) or cutoff) > cutoff]
        rows.sort(key=lambda e: e["volume24h"], reverse=True)
        out[domain] = [public(e) for e in rows[:NEW_PER_DOMAIN]]
    return out


def fetch_resolved(now: datetime) -> dict[str, list[dict]]:
    cutoff = now - timedelta(hours=RESOLVED_LOOKBACK_H)
    out = {}
    for domain, cfg in DOMAINS.items():
        rows, seen = [], set()
        for tag in cfg["tag_ids"]:
            for m in get(f"{GAMMA}/markets", {"closed": "true", "tag_id": tag, "order": "closedTime",
                                               "ascending": "false", "limit": 100}) or []:
                q = m.get("question") or ""
                closed = parse_iso(m.get("closedTime"))
                if (m["id"] in seen or not closed or closed < cutoff or fnum(m.get("volume")) < RESOLVED_MIN_VOL
                        or is_noise(q) or DAILY.search(q)):
                    continue
                seen.add(m["id"])
                p = prices(m)
                outcomes = json.loads(m.get("outcomes") or '["Yes","No"]')
                idx = max(range(len(p)), key=lambda i: p[i]) if p else 0
                ev = (m.get("events") or [{}])[0]
                rows.append({"question": q, "result": outcomes[idx] if idx < len(outcomes) else None,
                             "closedTime": m.get("closedTime"), "volume": fnum(m.get("volume")),
                             "eventTitle": ev.get("title"),
                             "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None})
        rows.sort(key=lambda r: r["volume"], reverse=True)
        out[domain] = rows[:RESOLVED_PER_DOMAIN]
    return out


def movers(events: dict[str, dict]) -> list[dict]:
    """Internal candidate source: biggest 24h move per event with a volume floor (not written to JSON)."""
    out = []
    for e in events.values():
        best = None
        for r in e["_all"]:
            ch = r["change1d"]
            if (ch is None or abs(ch) < MOVER_MIN_CHANGE or r["yes"] is None
                    or not MOVER_PRICE_RANGE[0] <= r["yes"] <= MOVER_PRICE_RANGE[1]
                    or r["volume24h"] < MOVER_MIN_VOL24H or is_noise(r["question"])):
                continue
            if best is None or abs(ch) > abs(best["change1d"]):
                best = r
        if best:
            out.append({**best, "event": e})
    return out


def fetch_official() -> dict[str, list[dict]]:
    """Polymarket's own 'Breaking' board, per category, plus a 7-day curve for the top three."""
    out = {}
    for cat in OFFICIAL_CATEGORIES:
        rows = (get(f"{SITE}/api/biggest-movers", {"locale": "en", "category": cat}, ua=BROWSER_UA) or {}).get("markets") or []
        items = []
        for m in rows:
            if is_noise(m.get("question")):
                continue
            ev = (m.get("events") or [{}])[0]
            hist = m.get("history") or []
            ids = m.get("clobTokenIds") or []
            ids = json.loads(ids) if isinstance(ids, str) else list(ids)
            items.append({
                "question": m.get("question"), "yes": m.get("currentPrice"), "change1d": m.get("oneDayPriceChange"),
                "closed": bool(m.get("closed")), "eventSlug": ev.get("slug"),
                "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
                "sparkline": [[h["t"], round(h["p"], 3)] for h in hist[::max(1, len(hist) // 24)]],
                "points": len(hist), "yesTokenId": str(ids[0]) if ids else None,
            })
        out[cat] = items
    top = sorted((m for c, ms in out.items() if c != "culture" for m in ms),
                 key=lambda m: abs(m["change1d"] or 0), reverse=True)
    done = set()
    for m in top:
        if m["question"] in done or not m["yesTokenId"]:
            continue
        done.add(m["question"])
        d = get(f"{CLOB}/prices-history", {"market": m["yesTokenId"], "interval": "1w", "fidelity": 120})
        m["history7d"] = [[h["t"], round(h["p"], 3)] for h in ((d or {}).get("history") or [])]
        if len(done) >= 3:
            break
    for ms in out.values():
        for m in ms:
            m.pop("yesTokenId", None)
    return out


_POST = re.compile(r'<a href="https://x\.com/polymarket/status/(\d+)"[^>]*>.*?font-medium">([^<]*)</p>'
                   r'<div class="text-body-sm text-pk-content-secondary">([^<]*)</div>.*?break-words">(.*?)</p>', re.S)


def fetch_feed(now: datetime) -> list[dict]:
    """@Polymarket X posts, scraped from the mirror embedded in polymarket.com/breaking (no API)."""
    page = get(f"{SITE}/breaking", ua=BROWSER_UA, raw=True) or ""
    posts = []
    for pid, kind, ts, body in _POST.findall(page):
        text = html.unescape(html.unescape(re.sub(r"<[^>]+>", "", body))).strip()
        text = re.sub(r"\s*\n\s*", " / ", text)
        try:  # SSR timestamps are UTC without a year, e.g. "Oct 15, 4:23 PM"
            when = datetime.strptime(f"{ts.strip()} {now.year}", "%b %d, %I:%M %p %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when > now + timedelta(days=1):
            when = when.replace(year=now.year - 1)
        if when < now - timedelta(hours=LOOKBACK_H + 2):
            continue
        is_new = kind.lower().startswith("new")
        posts.append({
            "id": pid, "url": f"https://x.com/polymarket/status/{pid}",
            "kind": "new" if is_new else "breaking",
            "time": when.astimezone(TZ).strftime("%m-%d %H:%M"), "timestamp": int(when.timestamp()),
            "text": text, "odds": bool(re.search(r"\d+%\s*chance", text, re.I)),
        })
    posts.sort(key=lambda p: p["timestamp"], reverse=True)
    return posts


def fetch_column(now: datetime) -> list[dict]:
    xml = get(NEWS_RSS, ua=BROWSER_UA, raw=True) or ""
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S)[:5]:
        def grab(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
            return html.unescape(m.group(1).strip()) if m else None
        pub = (grab("pubDate") or "").replace(" GMT", " +0000")
        try:
            when = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            when = None
        out.append({"title": grab("title"), "link": grab("link"),
                    "date": when.astimezone(TZ).strftime("%Y-%m-%d") if when else pub,
                    "fresh": bool(when and when > now - timedelta(hours=LOOKBACK_H + 2))})
    return out


def fetch_flows(events: dict[str, dict], now: datetime) -> list[dict]:
    """Net money per market from >= $10k trades in our candidate events, mid-range prices only."""
    by_slug = {e["slug"]: e for e in events.values()}
    cutoff = (now - timedelta(hours=LOOKBACK_H)).timestamp()
    rows = get(f"{DATA_API}/trades", {"limit": 1000, "takerOnly": "true",
                                       "filterType": "CASH", "filterAmount": WHALE_MIN_USD}) or []
    agg: dict[str, dict] = {}
    for t in rows:
        ev = by_slug.get(t.get("eventSlug"))
        price = fnum(t.get("price"))
        if (t.get("timestamp", 0) < cutoff or not ev or is_noise(t.get("title"))
                or not FLOW_PRICE_RANGE[0] <= price <= FLOW_PRICE_RANGE[1]):
            continue
        usd_ = fnum(t.get("size")) * price
        # money flowing toward "Yes": buying Yes or selling No
        toward_yes = (t.get("side") == "BUY") == (t.get("outcomeIndex", 0) == 0)
        a = agg.setdefault(t["title"], {"question": t["title"], "eventTitle": ev["title"], "url": ev["url"],
                                        "domain": ev["domain"], "net": 0.0, "gross": 0.0, "trades": 0,
                                        "lastPrice": price})
        a["net"] += usd_ if toward_yes else -usd_
        a["gross"] += usd_
        a["trades"] += 1
    out = sorted(agg.values(), key=lambda a: abs(a["net"]), reverse=True)[:5]
    for a in out:
        a["net"], a["gross"] = round(a["net"]), round(a["gross"])
    return out


# ============================================================================ scoring
def recent_mentions(today: str) -> dict[str, dict]:
    """Questions used in the short post over the last MENTION_DAYS days (reports/<date>.zh.json 'picked')."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=MENTION_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for f in sorted(REPORTS_DIR.glob("????-??-??.zh.json")):
        d = f.name[:10]
        if cutoff <= d < today:
            try:
                for p in json.loads(f.read_text()).get("picked", []):
                    if p.get("question"):
                        out[p["question"]] = {"date": d, "yes": p.get("yes")}
            except (ValueError, OSError):
                pass
    return out


def nouns(text: str | None) -> set[str]:
    return {w.strip("'’?,.:;()\"") for w in re.findall(r"\b[A-Z][A-Za-z\-]{3,}\b", text or "")} - STOP_NOUNS


def build_picks(day: dict, events: dict[str, dict], mv: list[dict]) -> list[dict]:
    by_slug = {e["slug"]: e for e in events.values()}
    prev = recent_mentions(day["date"])
    posts = [p for p in day["feed"] if p["kind"] == "breaking"]
    cands: dict[str, dict] = {}

    def add(kind, question, yes, change, vol, domain, ev, *, closed=False, label=None, event_title=None, points=None):
        if not question or is_noise(question) or DAILY.search(question):
            return
        if closed and (yes or 0) < 0.5:
            return                                  # a rung resolving No on its own is never news
        change = change or 0
        new_market = points is not None and points < 12 and yes is not None and abs((yes - change) - 0.5) < 0.02
        if kind == "resolved":
            surprise = 0.15                         # unknown prior; keep as filler
        elif kind == "news":
            surprise = 0.2                          # Polymarket chose to attach odds to this headline
        elif new_market:
            surprise = 0.08                         # first move of a market listed at 50% is not a move
        else:
            surprise = abs(change) + (0.3 if closed else 0)
        attention = max(1.0, math.log10(max(vol or 0, 1000)) - 2)      # $1k -> 1, $1M -> 4
        mention = prev.get(question)
        repeated = bool(mention and yes is not None and mention.get("yes") is not None
                        and abs(yes - mention["yes"]) < MENTION_MIN_MOVE)
        score = round(surprise * attention * (0.2 if repeated else 1.0), 3)
        words = nouns(question) | nouns(event_title)
        strong = words - WEAK_NOUNS
        causes = [{"time": p["time"], "text": p["text"], "url": p["url"]} for p in posts
                  if (strong & nouns(p["text"])) or len(words & nouns(p["text"])) >= 2][:2]
        c = {
            "kind": kind, "domain": domain, "question": question, "label": label,
            "eventTitle": event_title or (ev["title"] if ev else None), "eventSlug": ev["slug"] if ev else None,
            "eventKind": ev["kind"] if ev else None,
            "yes": yes, "change1d": change,
            "from": round(yes - change, 3) if (yes is not None and kind not in ("resolved", "news")) else None,
            "closed": closed, "newMarket": new_market, "volume24h": vol, "score": score,
            "repeated": repeated, "lastMention": mention["date"] if mention else None,
            "siblings": [{"label": o["label"], "yes": o["yes"]} for o in ev["outcomes"][:4]] if ev and ev["kind"] != "binary" else [],
            "causes": causes, "url": ev["url"] if ev else None,
        }
        if question not in cands or cands[question]["score"] < score:
            cands[question] = c

    # Culture (sports, film, music) rides along as its own domain with a small cap: it only enters the post
    # when a market moved, never as a bare headline.
    for cat, ms in day["official"].items():
        for m in ms:
            ev = by_slug.get(m["eventSlug"])
            add("official", m["question"], m["yes"], m["change1d"], ev["volume24h"] if ev else 10_000,
                OFFICIAL_CATEGORIES[cat], ev, closed=m["closed"], points=m["points"])
    for m in mv:
        ev = m["event"]
        add("mover", m["question"], m["yes"], m["change1d"], ev["volume24h"], ev["domain"], ev,
            label=m["label"], event_title=ev["title"])
    for domain, evs in day["hot"].items():
        for e in evs:
            for o in e["outcomes"][:3]:
                if o["change1d"] and abs(o["change1d"]) >= 0.05:
                    add("hot", o["question"], o["yes"], o["change1d"], e["volume24h"], domain,
                        by_slug.get(e["slug"]), label=o["label"], event_title=e["title"])
    for domain, rows in day["resolved"].items():
        for r in rows:
            add("resolved", r["question"], 1.0 if str(r["result"]).lower() == "yes" else 0.0, None, r["volume"],
                domain, None, closed=True, event_title=r["eventTitle"])
    # News posts where Polymarket itself attached a market ("… 69% chance.") are the only feed items with an
    # objective importance signal; they enter as candidates with the odds as the current price.
    for p in posts:
        m = re.search(r"(\d+)%\s*chance", p["text"], re.I)
        if not m:
            continue
        text = re.sub(r"\s*/\s*\d+% chance\.?.*$", "", p["text"]).strip()
        words = nouns(text)
        hit = next((e for e in by_slug.values() if words & nouns(e["title"])), None)  # domain from a matching event
        domain = hit["domain"] if hit else "politics"
        add("news", text, int(m.group(1)) / 100, None, 50_000, domain, None)
        if text in cands:
            cands[text]["url"], cands[text]["time"] = p["url"], p["time"]

    per_event, per_domain, out = {}, {}, []
    for c in sorted(cands.values(), key=lambda c: c["score"], reverse=True):
        ek = c["eventSlug"] or c["question"]
        cap = 1 if c["eventKind"] == "ladder" else PER_EVENT_MAX
        domain_cap = CULTURE_MAX if c["domain"] == "culture" else PER_DOMAIN_MAX
        if per_event.get(ek, 0) >= cap or per_domain.get(c["domain"], 0) >= domain_cap:
            continue
        per_event[ek] = per_event.get(ek, 0) + 1
        per_domain[c["domain"]] = per_domain.get(c["domain"], 0) + 1
        c["rank"] = len(out) + 1
        out.append(c)
        if len(out) >= PICKS_MAX:
            break
    return out


# ============================================================================ summary
def outcomes_line(e: dict, n: int = 3) -> str:
    return "; ".join(f"{o['label']} {pct(o['yes'])}{(' ' + delta(o['change1d'])) if o['change1d'] else ''}"
                     for o in e["outcomes"][:n])


def text_summary(day: dict) -> str:
    L = [f"# Polymarket 数据摘要 {day['date']}（生成于 {day['generatedAt']}）",
         f"正文第二行照抄：{day['stamp']}", ""]

    L += ["## 候选条目（已打分、去重、限额；小红书正文从这里选 4–10 条）",
          "格式：排名 [分数] 类型/领域  从→到  标题 | 事件 | 24h 成交 | 备注"]
    for p in day["picks"]:
        title = p["label"] if p["label"] and p["label"] not in ("Yes", p["question"]) else p["question"]
        notes = []
        if p["closed"]:
            notes.append("已结算")
        if p["newMarket"]:
            notes.append("今天新上线的档位，起点 50% 是默认值，不算变动")
        if p["repeated"]:
            notes.append(f"7 天内报过（{p['lastMention']}），数值没怎么变，慎用")
        elif p["lastMention"]:
            notes.append(f"上次报 {p['lastMention']}，这次变了")
        if p["siblings"]:
            head = "梯子市场，整体看" if p["eventKind"] == "ladder" else "同事件其他选项"
            notes.append(head + "：" + "; ".join(f"{s['label']} {pct(s['yes'])}" for s in p["siblings"]))
        frm = pct(p["from"]) if p["from"] is not None else "—"
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
        seen = set()
        L.append(f"### {label}")
        for m in sorted(rows, key=lambda m: abs(m["change1d"] or 0), reverse=True):
            if m["question"] in seen:
                continue
            seen.add(m["question"])
            if len(seen) > 8:
                break
            L.append(f"- {delta(m['change1d'])} → {pct(m['yes'])}  {m['question']}{'（已结算）' if m['closed'] else ''}")
    L.append("")

    fresh = [c for c in day["column"] if c["fresh"]]
    if fresh:
        L.append("## Polymarket 专栏新文章（news.polymarket.com）")
        L += [f"- {c['date']}  {c['title']}  {c['link']}" for c in fresh]
        L.append("")

    for domain, cfg in DOMAINS.items():
        L.append(f"## {cfg['label']}")
        L.append("### 热门（24h 成交；★ 为钉住）")
        for e in day["hot"][domain]:
            L.append(f"- {'★ ' if e.get('pinned') else ''}{usd(e['volume24h'])}  {e['title']}  → {outcomes_line(e)}")
        if day["new"][domain]:
            L.append("### 24h 新上线")
            L += [f"- {e['title']}  → {outcomes_line(e)}" for e in day["new"][domain]]
        if day["resolved"][domain]:
            L.append("### 刚结算")
            L += [f"- 结果 {r['result']}: {r['question']}  (vol {usd(r['volume'])})" for r in day["resolved"][domain]]
        L.append("")

    if day["flows"]:
        L.append("## 资金流向（≥$10k 大单按市场净额，正数流向 Yes）")
        L += [f"- {'+' if f['net'] >= 0 else '−'}{usd(abs(f['net']))} 净 / {usd(f['gross'])} 总，{f['trades']} 笔  {f['question']}"
              for f in day["flows"]]
    return "\n".join(L)


# ============================================================================ main
def main() -> int:
    now = datetime.now(timezone.utc)
    date = now.astimezone(TZ).strftime("%Y-%m-%d")
    if len(sys.argv) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", sys.argv[1]):
        date = sys.argv[1]
    log = lambda s: print(f"[info] {s}", file=sys.stderr)  # noqa: E731

    events = fetch_events()
    log(f"{len(events)} candidate events after noise filter")
    if len(events) < 50 or _requests == 0:
        # Network blocked or API down: never overwrite yesterday's data with an empty day.
        print(f"[error] fetched {len(events)} events with {_requests} requests; refusing to write data", file=sys.stderr)
        return 1
    day = {
        "date": date,
        "generatedAt": now.astimezone(TZ).strftime("%Y-%m-%d %H:%M %z"),
        "stamp": stamp(now),
        "stats": {},
        "official": fetch_official(),
        "feed": fetch_feed(now),
        "column": fetch_column(now),
        "hot": hot_panel(events, date),
        "new": new_panel(events, now),
        "resolved": fetch_resolved(now),
        "flows": fetch_flows(events, now),
    }
    day["picks"] = build_picks(day, events, movers(events))
    day["stats"] = {"events": len(events), "feedPosts": len(day["feed"]),
                    "officialMovers": sum(len(v) for k, v in day["official"].items() if k != "culture"),
                    "resolved": sum(len(v) for v in day["resolved"].values()),
                    "new": sum(len(v) for v in day["new"].values()),
                    "picks": len(day["picks"]), "requests": _requests}
    log("official: " + ", ".join(f"{k}={len(v)}" for k, v in day["official"].items()))
    log(f"feed: {len(day['feed'])} posts; picks: {len(day['picks'])} "
        f"({sum(p['repeated'] for p in day['picks'])} repeated); flows: {len(day['flows'])}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date}.json").write_text(json.dumps(day, ensure_ascii=False, indent=1))
    dates = sorted({p.stem for p in DATA_DIR.glob("????-??-??.json")}, reverse=True)
    (DATA_DIR / "index.json").write_text(json.dumps({"dates": dates, "latest": dates[0]}, indent=1))
    summary = text_summary(day)
    (DATA_DIR / "latest-summary.txt").write_text(summary)
    print(summary)
    log(f"wrote data/{date}.json ({_requests} requests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
