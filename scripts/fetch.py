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
  resolved  {domain: [market]}     markets closed in the last 36h, with the day-before price (prior)
  board     [storyline]            long-running events, one number each, change since last written
  upcoming  [event]                events resolving within 48h
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
# Numeric brackets ("between $407,000 and $411,000", "100-119", "25m and 30m"): never a headline on their own
BRACKET = re.compile(r"\bbetween\b|\b\d[\d,.]*[kmb]?\s*(-|–|to)\s*\$?\d[\d,.]*[kmb]?\b", re.I)
# Earnings-season markets: "(NFLX) Q3 operating margin 32% to 34%?". Thin by nature, but each settled bracket is a
# real-world result (the company reported). They bypass the volume floors and the bracket rule.
EARNINGS = re.compile(r"\((?!HIGH\)|LOW\))[A-Z]{1,5}\)\s+Q[1-4]\b|\bQ[1-4]\b.*\b(revenue|margin|EPS|earnings|sales|growth|subscribers|deliveries|load factor|procedure)", re.I)
EARNINGS_MIN_VOL = 2_000
# The per-company "Will Meta (META) beat quarterly earnings?" markets (tag Earnings). A few thousand dollars each, so
# they never clear a volume floor, but a settled one is the quarter's headline result. They get their own 财报 line.
EARNINGS_TAG = 1013
BEAT = re.compile(r"^Will (.+?) \(([A-Z.]{1,6})\) beat quarterly earnings\?$")
EARNINGS_LINE_MAX = 6
# Crypto price ladders ("What price will Bitcoin hit in October?", "Will Ethereum dip to $2,300?") move every day;
# Bitcoin and Ethereum are one story for the post.
CRYPTO_PRICE = re.compile(r"(?=.*\b(Bitcoin|BTC|Ethereum|ETH|Solana|SOL|XRP|Dogecoin)\b)(?=.*\b(hit|reach|dip|above|below|price)\b)", re.I)
DATE_RUNG =re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}(, \d{4})?$")

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
RESOLVED_MIN_VOL = 20_000          # a settled market nobody traded is not a result worth a line
RESOLVED_PRIOR_LOOKUPS = 15        # settled markets (by volume) whose day-before price we fetch
RESOLVED_EXPECTED = 0.90           # prior this far toward the outcome = "as expected": zero information
RESOLVED_EXPECTED_MIN_VOL = 500_000  # ...unless the market is this big, then one line may say "如期"
RESOLVED_MIN_INFO = 2_000           # volume × surprise below this is neither big nor surprising
RESOLVED_EXPECTED_MAX = 3           # "as expected" rows allowed, always at the bottom
MOVER_MIN_CHANGE = 0.05
YOUNG_H = 23                # an event created less than this many hours ago has no trustworthy 24h change
OPEN_SETTLE_S = 3600        # a 24h-ago price that did not hold for an hour is not a baseline (see settle_spike)
MOVER_PRICE_RANGE = (0.03, 0.97)
MOVER_MIN_VOL24H = 5_000
WHALE_MIN_USD = 10_000
FLOW_PRICE_RANGE = (0.10, 0.90)
PAGES_PER_TAG = 2

# Scoring for the short post
CAND_MIN_VOL24H = 20_000           # below this a move is one trader, not a market
VOL_TIERS = (100_000, 500_000)     # attention 1 / 2 / 3 by 24h volume
HEADLINE_MIN_VOL = 100_000         # a headline needs real money on the market itself...
HEADLINE_EVENT_MIN_VOL = 500_000   # ...or on its event
HEADLINE_MIN_MOVE = 0.30           # or it crossed 50%, or it settled against a confident prior
REWRITE_MOVE = 0.10                # an event written in the last 7 days earns another line only after this much movement
STORY_REWRITE_MOVE = 0.15          # ...and a long-running event (already on the board every day) needs more
STORY_WRITTEN_DAYS = 3             # written on this many of the last 7 days -> promoted to the long-running board
BOARD_MAX = 8                      # long-running events kept (the card shows 6, the 盘面 line 4)
TAIL = 0.12                        # a move that stays within 12% of either end is not a story unless it settles
SECOND_ITEM_MIN_VOL = 50_000       # items 2 and 3 of the post need this much 24h volume
QUIET_SCORE = 0.5                  # no non-culture candidate above this -> a quiet day
NEW_MIN_VOL24H = 20_000            # a new market worth a "新题" line already has money on it
UPCOMING_H = 48                    # "明天揭晓": events ending within this window
UPCOMING_MIN_VOL = 100_000
PICKS_MAX = 14
PER_EVENT_MAX = 2
PER_DOMAIN_MAX = 5
CULTURE_MAX = 2          # sports / film / music: at most two, always at the end of the post
MENTION_DAYS = 7
EVENT_REPEAT_DAYS = MENTION_DAYS   # an event written in the window comes back only after REWRITE_MOVE / STORY_REWRITE_MOVE
                                   # on whichever rung it is today (Fable "by Oct 24", "by Oct 29" is one story)
HEADLINE_REPEAT_DAYS = 3   # an event that was the title in the last three days is not the title again unless it settled
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
    ids = m.get("clobTokenIds") or []
    ids = json.loads(ids) if isinstance(ids, str) else list(ids)
    return {
        "question": m.get("question"),
        "tokenId": str(ids[0]) if ids else None,
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
    # Second entrance: stories the post itself kept writing (3 of the last 7 days) join the board, so they stop
    # being "new" items every morning and live on the 盘面 line instead. Exit: settled, or 7 days without being
    # written and without staying in a volume top-10.
    written = {fam for fam, m in recent_families(today).items()
               if m.get("count", 0) >= STORY_WRITTEN_DAYS and fam != "crypto-price"} - never
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
    for slug in (always | written) - set(by_slug):
        rows = get(f"{GAMMA}/events", {"slug": slug}) or []
        if rows:
            e = event_brief(rows[0], "politics")
            if not e["outcomes"]:
                continue                                    # settled since it was written: it leaves the board
            e["volRank"], e["persistDays"] = None, counts.get(slug, 0)
            ranked[e["domain"]].insert(0, e)
            by_slug[slug] = e
        elif slug in always:
            print(f"[warn] always-pin not found: {slug}", file=sys.stderr)

    cands = [e for lst in ranked.values() for e in lst[:10]
             if e["persistDays"] >= need or e["slug"] in always]
    cands.sort(key=lambda e: (e["slug"] in always, e["persistDays"], e["volume24h"]), reverse=True)
    pinned = {e["slug"] for e in cands[:PERSIST_MAX + len(always)]}
    by_volume = set(pinned)
    editorial = {slug for slug in written if slug in by_slug and by_slug[slug]["outcomes"]} - pinned
    pinned |= editorial
    if len(pinned) > BOARD_MAX:                             # keep the biggest; the rest are plain hot rows
        pinned = set(sorted(pinned, key=lambda x: (x in always, by_slug[x]["volume24h"]), reverse=True)[:BOARD_MAX])
    for slug in pinned:
        by_slug[slug]["pinnedBy"] = "always" if slug in always else "volume" if slug in by_volume else "written"
    print(f"[info] long-running panel: {len(pinned)} events ({len(editorial & pinned)} by being written; "
          f"history {hist_days} days, need {need})", file=sys.stderr)

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


def prior_price(token_id: str, closed: datetime) -> float | None:
    """Yes price about 24h before the market closed (last point at or before that moment)."""
    hist = (get(f"{CLOB}/prices-history", {"market": token_id, "interval": "1w", "fidelity": 60}) or {}).get("history") or []
    if not hist:
        return None
    cut = closed.timestamp() - 24 * 3600
    before = [h for h in hist if h["t"] <= cut]
    return round((before[-1] if before else hist[0])["p"], 4)


def fetch_resolved(now: datetime, events: dict[str, dict], prev_questions: set[str]) -> dict[str, list[dict]]:
    """Markets closed in the last 36h, ranked by information: volume × how wrong the day-before price was.

    Dropped: anything shown yesterday; the losing rows of an exclusive event (only the winner is news);
    date rungs that expired to No while the same ladder is still open (that is a calendar, not a result);
    outcomes the market already priced at 90%+ (one such line may survive if the market is very large)."""
    cutoff = now - timedelta(hours=RESOLVED_LOOKBACK_H)
    rows, seen = [], set()
    for domain, cfg in DOMAINS.items():
        for tag in cfg["tag_ids"]:
            for m in get(f"{GAMMA}/markets", {"closed": "true", "tag_id": tag, "order": "closedTime",
                                               "ascending": "false", "limit": 100}) or []:
                q = m.get("question") or ""
                closed = parse_iso(m.get("closedTime"))
                if (m["id"] in seen or not closed or closed < cutoff
                        or fnum(m.get("volume")) < (EARNINGS_MIN_VOL if EARNINGS.search(q) else RESOLVED_MIN_VOL)
                        or is_noise(q) or DAILY.search(q) or q in prev_questions):
                    continue
                seen.add(m["id"])
                p = prices(m)
                outcomes = json.loads(m.get("outcomes") or '["Yes","No"]')
                idx = max(range(len(p)), key=lambda i: p[i]) if p else 0
                ev = (m.get("events") or [{}])[0]
                ids = m.get("clobTokenIds") or []
                ids = json.loads(ids) if isinstance(ids, str) else list(ids)
                rows.append({"question": q, "result": outcomes[idx] if idx < len(outcomes) else None,
                             "resultYes": idx == 0, "closedTime": m.get("closedTime"), "_closed": closed,
                             "volume": fnum(m.get("volume")), "domain": domain,
                             "eventTitle": ev.get("title"), "eventSlug": ev.get("slug"),
                             "label": m.get("groupItemTitle") or None, "_token": str(ids[0]) if ids else None,
                             "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None})
    # exclusive events: keep the winner only; with no winner in the set keep the biggest loser
    by_event: dict[str, list[dict]] = {}
    for r in rows:
        by_event.setdefault(r["eventSlug"] or r["question"], []).append(r)
    keep = []
    for group in by_event.values():
        winners = [r for r in group if r["resultYes"]]
        if winners:
            keep += winners
        else:
            keep.append(max(group, key=lambda r: r["volume"]))
    # a date rung expiring to No while later rungs are open is not news
    keep = [r for r in keep if not (not r["resultYes"] and r["label"] and DATE_RUNG.match(r["label"])
                                    and events.get(r["eventSlug"], {}).get("kind") == "ladder")]
    keep.sort(key=lambda r: r["volume"], reverse=True)
    for r in keep[:RESOLVED_PRIOR_LOOKUPS]:
        r["prior"] = prior_price(r["_token"], r["_closed"]) if r["_token"] else None
    for r in keep:
        prior = r.get("prior")
        outcome = 1.0 if r["resultYes"] else 0.0
        r["expected"] = prior is not None and abs(outcome - prior) <= 1 - RESOLVED_EXPECTED
        r["info"] = round(r["volume"] * (abs(outcome - prior) if prior is not None else 0.25))
    # one line per event: when a thing happens every later date rung settles Yes at once; keep the earliest
    # date (the one that "came true"), otherwise the most informative row
    def rung_date(r):
        try:
            return datetime.strptime(r["label"] + ("" if "," in r["label"] else f", {now.year}"), "%B %d, %Y")
        except (TypeError, ValueError):
            return None
    grouped: dict[str, list[dict]] = {}
    for r in keep:
        grouped.setdefault(r["eventSlug"] or r["question"], []).append(r)
    keep = []
    for group in grouped.values():
        dated = [r for r in group if rung_date(r)]
        keep.append(min(dated, key=rung_date) if len(dated) == len(group) and group[0]["resultYes"] else max(group, key=lambda r: r["info"]))
    keep.sort(key=lambda r: r["info"], reverse=True)
    out_rows, expected_used = [], 0
    for r in keep:
        if r["info"] < RESOLVED_MIN_INFO and not r["expected"] and not EARNINGS.search(r["question"]):
            continue
        if r["expected"]:
            if r["volume"] < RESOLVED_EXPECTED_MIN_VOL or expected_used >= RESOLVED_EXPECTED_MAX:
                continue
            expected_used += 1
        out_rows.append(r)
    if len(out_rows) < 3:   # a thin day (weekends): let a few big "as expected" results fill the card, they are still answers
        chosen = {id(r) for r in out_rows}
        for r in keep:
            if id(r) not in chosen and r["expected"] and r["volume"] >= 100_000 and expected_used < RESOLVED_EXPECTED_MAX:
                out_rows.append(r)
                expected_used += 1
    out_rows.sort(key=lambda r: (not r["expected"], r["info"]), reverse=True)   # surprises first, 如期 last
    out = {d: [] for d in DOMAINS}
    for r in out_rows:
        if len(out[r["domain"]]) < RESOLVED_PER_DOMAIN:
            out[r["domain"]].append({k: v for k, v in r.items() if not k.startswith("_") and k != "domain"})
    return out


def previous_resolved(today: str) -> set[str]:
    """Questions on yesterday's settled lists (the card and the 财报 line), so a result never shows twice."""
    for f in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        if f.name[:10] < today:
            try:
                d = json.loads(f.read_text())
                return ({r["question"] for rows in d.get("resolved", {}).values() for r in rows}
                        | {r["question"] for r in (d.get("earnings") or {}).get("settled", [])})
            except (ValueError, OSError, KeyError):
                return set()
    return set()


def fetch_earnings(now: datetime, prev_questions: set[str]) -> dict[str, list[dict]]:
    """Earnings-season "beat quarterly earnings?" markets: the ones settled in the last 36h with their day-before
    price, and the ones reporting on the next US day (the date stated in the market text). Ranked by lifetime volume, which tracks how well-known the company is."""
    def row(e: dict) -> dict | None:
        m = (e.get("markets") or [{}])[0]
        hit = BEAT.match(m.get("question") or "")
        if not hit:
            return None
        cons = re.search(r"consensus estimate for .*?((?:non-)?GAAP) EPS.*? is (-?\$-?[\d.]+)", m.get("description") or "", re.I)
        when = re.search(r"release earnings on (\w+ \d{1,2}, \d{4})", m.get("description") or "")
        try:
            report = datetime.strptime(when.group(1), "%B %d, %Y").date().isoformat() if when else None
        except ValueError:
            report = None
        ids = m.get("clobTokenIds") or []
        ids = json.loads(ids) if isinstance(ids, str) else list(ids)
        return {"company": hit.group(1), "ticker": hit.group(2), "question": m["question"], "reportDate": report,
                "eps": cons.group(1) if cons else None, "consensus": cons.group(2) if cons else None,
                "yes": (prices(m) or [None])[0], "volume": fnum(e.get("volume")), "endDate": e.get("endDate"),
                "url": f"https://polymarket.com/event/{e.get('slug')}", "_m": m, "_token": str(ids[0]) if ids else None}

    settled = []
    cutoff = now - timedelta(hours=RESOLVED_LOOKBACK_H)
    for e in get(f"{GAMMA}/events", {"tag_id": EARNINGS_TAG, "closed": "true", "order": "endDate",
                                     "ascending": "false", "limit": 100}) or []:
        r = row(e)
        closed = parse_iso((r or {}).get("_m", {}).get("closedTime"))
        if r and closed and closed >= cutoff and r["volume"] >= EARNINGS_MIN_VOL and r["question"] not in prev_questions:
            r["beat"], r["_closed"] = (r["yes"] or 0) >= 0.5, closed
            settled.append(r)
    settled.sort(key=lambda r: r["volume"], reverse=True)
    settled = settled[:EARNINGS_LINE_MAX]
    for r in settled:
        r["prior"] = prior_price(r["_token"], r["_closed"]) if r["_token"] else None

    upcoming = []
    for e in get(f"{GAMMA}/events", {"tag_id": EARNINGS_TAG, "active": "true", "closed": "false", "order": "volume",
                                     "ascending": "false", "limit": 100}) or []:
        r = row(e)
        # The run is the US afternoon before: today's US reports are already out (they settle as results);
        # tomorrow's US reports are what Beijing readers learn about next.
        if r and r["reportDate"] == ((now + la_offset(now)).date() + timedelta(days=1)).isoformat():
            upcoming.append(r)
    clean = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]  # noqa: E731
    return {"settled": clean(settled), "upcoming": clean(upcoming[:EARNINGS_LINE_MAX])}


def board_panel(day: dict, now: datetime) -> list[dict]:
    """The storylines: long-running events, one number each, with the change since we last wrote about it.
    binary -> Yes; exclusive -> the leader; date ladder -> the nearest deadline still in doubt; price ladder ->
    the rung nearest 50%. `write` says whether it earns its own line today or only a slot on the 盘面 row."""
    prev = recent_mentions(day["date"])
    fams = recent_families(day["date"])
    out = []
    for domain in DOMAINS:
        for e in day["hot"][domain]:
            if not e.get("pinned") or not e["outcomes"]:
                continue
            outs = e["outcomes"]
            if e["kind"] == "binary":
                o = outs[0]
            elif e["kind"] == "exclusive":
                o = max(outs, key=lambda o: o["yes"] or 0)
            else:
                dated = [o for o in outs if DATE_RUNG.match(o["label"] or "")]
                if dated:
                    # the nearest date whose answer is still open (10-90%): "ceasefire through Oct 31 82%",
                    # "Fable by Oct 31 36%", never the far-off rung that is already at 94%
                    def rung_day(o):
                        lab = o["label"]
                        try:
                            return datetime.strptime(lab if "," in lab else f"{lab}, {now.year}", "%B %d, %Y")
                        except ValueError:
                            return datetime.max
                    dated.sort(key=rung_day)
                    open_ = [o for o in dated if 0.10 <= (o["yes"] or 0) <= 0.90]
                    o = open_[0] if open_ else next((o for o in dated if (o["yes"] or 0) < 0.95), dated[-1])
                else:
                    o = min(outs, key=lambda o: abs((o["yes"] or 0) - 0.5))
            end = parse_iso(e.get("endDate"))
            days_left = math.ceil((end - now).total_seconds() / 86400) if end else None
            fam = fams.get(family(o["question"], e["title"], e["slug"]), {})
            mention = prev.get(o["question"]) or ({"date": fam["written"]} if fam.get("written") else None)
            since = round(o["yes"] - mention["yes"], 3) if mention and mention.get("yes") is not None and o["yes"] is not None else None
            move = since if since is not None else (o["change1d"] or 0)           # no same-rung history: judge by the 24h move
            crossed = o["yes"] is not None and (o["yes"] - 0.5) * (o["yes"] - move - 0.5) < 0
            out.append({"title": e["title"], "slug": e["slug"], "kind": e["kind"], "url": e["url"], "domain": domain,
                        "label": o["label"], "question": o["question"], "yes": o["yes"], "change1d": o["change1d"],
                        "volume24h": e["volume24h"], "daysLeft": days_left,
                        "sinceDate": mention["date"] if mention else None, "sinceYes": since, "pinnedBy": e.get("pinnedBy"),
                        "closed": False,
                        "history7d": [[h["t"], round(h["p"], 3)] for h in ((get(f"{CLOB}/prices-history", {"market": o["tokenId"], "interval": "1w", "fidelity": 120}) or {}).get("history") or [])] if o.get("tokenId") else [],
                        "write": bool(abs(move) >= STORY_REWRITE_MOVE or crossed)})
    out.sort(key=lambda b: b["volume24h"], reverse=True)
    return out


def upcoming_panel(events: dict[str, dict], now: datetime) -> list[dict]:
    """Big events that resolve within 48h: tomorrow's answers, worth one line today."""
    out = []
    for e in events.values():
        end = parse_iso(e.get("endDate"))
        if not end or e["volume24h"] < UPCOMING_MIN_VOL:
            continue
        hours = (end - now).total_seconds() / 3600
        if 0 <= hours <= UPCOMING_H:
            out.append({**public(e), "hoursLeft": round(hours, 1)})
    out.sort(key=lambda e: e["volume24h"], reverse=True)
    return out[:6]


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


def settle_spike(hist: list[dict], yes: float | None, official_change: float | None) -> dict:
    """Re-base a 24h change that starts from a price that did not exist for more than a few minutes.

    The board's oneDayPriceChange runs from the first print in its window. On a market listed inside the
    window that is the opening print of a thin book (11% for half an hour, then 88% all day); on an older
    market it can be a blip that reverted within the hour. Either way the "jump" never happened. If the
    opening price held for the rest of the first hour it was real and nothing changes. Otherwise the
    baseline becomes the median price of hours 1 to 6, and the board's figure is kept for the writer."""
    if len(hist) < 4 or yes is None:
        return {}
    t0, p0 = hist[0]["t"], hist[0]["p"]
    held = [h["p"] for h in hist[1:] if h["t"] <= t0 + OPEN_SETTLE_S]
    if any(abs(p - p0) < 0.10 for p in held):
        return {}
    window = [h["p"] for h in hist if t0 + OPEN_SETTLE_S <= h["t"] <= t0 + 6 * OPEN_SETTLE_S] or [hist[min(3, len(hist) - 1)]["p"]]
    base = sorted(window)[len(window) // 2]
    if abs(p0 - base) < 0.15:
        return {}
    return {"rebased": "spike", "firstPrint": round(p0, 3), "officialChange1d": official_change, "change1d": round(yes - base, 4)}


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
            row = {
                "question": m.get("question"), "yes": m.get("currentPrice"), "change1d": m.get("oneDayPriceChange"),
                "closed": bool(m.get("closed")), "eventSlug": ev.get("slug"),
                "url": f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
                "sparkline": [[h["t"], round(h["p"], 3)] for h in hist[::max(1, len(hist) // 24)]],
                "points": len(hist), "yesTokenId": str(ids[0]) if ids else None,
            }
            row.update(settle_spike(hist, row["yes"], row["change1d"]))
            items.append(row)
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


def family(question: str | None, event_title: str | None, slug: str | None) -> str:
    """The story a candidate belongs to: its event, except that all crypto price ladders are one story."""
    if CRYPTO_PRICE.search(question or "") or CRYPTO_PRICE.search(event_title or ""):
        return "crypto-price"
    return slug or question or ""


def recent_families(today: str) -> dict[str, dict]:
    """Stories written in the post on recent days: {family: {"written": last date, "headline": last date as title}}.

    The reports only keep the English questions, so each day's data file maps a question back to its event."""
    oldest = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=max(EVENT_REPEAT_DAYS, HEADLINE_REPEAT_DAYS))).strftime("%Y-%m-%d")
    written_cut = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=EVENT_REPEAT_DAYS)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for f in sorted(REPORTS_DIR.glob("????-??-??.zh.json")):
        d = f.name[:10]
        if not oldest <= d < today:
            continue
        try:
            zh = json.loads(f.read_text())
            data = json.loads((DATA_DIR / f"{d}.json").read_text())
        except (ValueError, OSError):
            continue
        events = {}
        for p in data.get("picks", []):
            events[p["question"]] = (p.get("eventTitle"), p.get("eventSlug"))
        labels: dict[str, list] = {}
        for p in data.get("picks", []):
            if p.get("label") and p["label"] not in ("Yes", p["question"]):
                labels.setdefault(p["label"], []).append((p.get("eventTitle"), p.get("eventSlug")))
        for lab, owners in labels.items():
            if len({o[1] for o in owners}) == 1:                 # "October 31" is only usable when one event owned it that day
                events.setdefault(lab, owners[0])
        for b in data.get("board", []):
            events.setdefault(b["question"], (b["title"], b["slug"]))
        for ms in data.get("official", {}).values():
            for m in ms:
                events.setdefault(m["question"], (None, m.get("eventSlug")))
        for rows in data.get("resolved", {}).values():
            for r in rows:
                events.setdefault(r["question"], (r.get("eventTitle"), r.get("eventSlug")))
        fam = lambda q: family(q, *events.get(q, (None, None)))  # noqa: E731
        if d >= written_cut:
            for p in zh.get("picked", []):
                if p.get("question"):
                    m = out.setdefault(fam(p["question"]), {})
                    m["written"] = d
                    m.setdefault("days", set()).add(d)
        if zh.get("headlineQuestion"):
            out.setdefault(fam(zh["headlineQuestion"]), {})["headline"] = d
    for m in out.values():
        m["count"] = len(m.get("days", ()))
    return out


def apply_recent(cands: list[dict], today: str) -> None:
    """Event-level memory on top of the per-question one: a story written in the last two days is demoted unless it
    moved 20+ points today or settled; a story that was the title in the last three days cannot be the title."""
    recent = recent_families(today)
    for c in cands:
        c["family"] = family(c["question"], c.get("eventTitle"), c.get("eventSlug"))
        seen = recent.get(c["family"], {})
        c["writtenDays"] = seen.get("count", 0)
        threshold = STORY_REWRITE_MOVE if c.get("story") else REWRITE_MOVE
        move = c["sinceMove"] if c.get("sinceMove") is not None else (c["change1d"] or 0)   # no same-rung history: today's move
        frm, yes = c.get("from"), c.get("yes")
        crossed = frm is not None and yes is not None and (frm - 0.5) * (yes - 0.5) < 0
        if seen.get("written") and not c["repeated"] and not c["closed"] and not crossed and abs(move) < threshold:
            c["repeated"], c["repeatedEvent"], c["lastMention"], c["sinceMove"] = True, True, seen["written"], round(move, 3)
            c["score"], c["headlineOk"] = round(c["score"] * 0.2, 3), False
        elif seen.get("written") and not c.get("lastMention"):
            c["lastMention"], c["sinceMove"] = seen["written"], round(move, 3)
        if seen.get("headline") and c["kind"] != "resolved" and c["headlineOk"]:
            c["headlineOk"], c["recentHeadline"] = False, seen["headline"]


def cap_picks(cands: list[dict]) -> list[dict]:
    """Best first, at most two rows of a binary event, one of any other event, one crypto price line, five per
    domain, two culture. Crypto rows that lost to the kept one ride along as `related` so they can share its line."""
    per_event, per_domain, out, kept = {}, {}, [], {}
    for c in sorted(cands, key=lambda c: c["score"], reverse=True):
        ek = c.get("family") or c["eventSlug"] or c["question"]
        cap = PER_EVENT_MAX if c["eventKind"] == "binary" and ek != "crypto-price" else 1   # Yes/No of one decision, or two rungs, are one story
        domain_cap = CULTURE_MAX if c["domain"] == "culture" else PER_DOMAIN_MAX
        if per_event.get(ek, 0) >= cap:
            if ek == "crypto-price" and ek in kept and c["eventSlug"] != kept[ek]["eventSlug"] and len(kept[ek]["related"]) < 2:
                kept[ek]["related"].append({"question": c["question"], "yes": c["yes"], "from": c["from"]})
            continue
        if per_domain.get(c["domain"], 0) >= domain_cap:
            continue
        per_event[ek] = per_event.get(ek, 0) + 1
        per_domain[c["domain"]] = per_domain.get(c["domain"], 0) + 1
        c["rank"] = len(out) + 1
        c["related"] = []
        kept.setdefault(ek, c)
        out.append(c)
        if len(out) >= PICKS_MAX:
            break
    return out


def nouns(text: str | None) -> set[str]:
    return {w.strip("'’?,.:;()\"") for w in re.findall(r"\b[A-Z][A-Za-z\-]{3,}\b", text or "")} - STOP_NOUNS


def build_picks(day: dict, events: dict[str, dict], mv: list[dict]) -> list[dict]:
    by_slug = {e["slug"]: e for e in events.values()}
    prev = recent_mentions(day["date"])
    posts = [p for p in day["feed"] if p["kind"] == "breaking"]
    cands: dict[str, dict] = {}

    young_cut = datetime.now(TZ) - timedelta(hours=YOUNG_H)

    def logit(p):
        p = min(0.99, max(0.01, p))
        return math.log(p / (1 - p))

    def add(kind, question, yes, change, vol, domain, ev, *, closed=False, label=None, event_title=None, points=None,
            young=None, prior=None):
        if not question or is_noise(question) or DAILY.search(question):
            return
        earnings = bool(EARNINGS.search(question))
        if earnings and ev:
            vol = max(vol or 0, ev["volume24h"])    # an earnings ladder is read as one event
        if (vol or 0) < (EARNINGS_MIN_VOL if earnings else CAND_MIN_VOL24H):
            return                                  # one trader is not a market
        if young is None:  # Gamma rows carry no price path; go by the event's listing time
            created = parse_iso(ev.get("createdAt")) if ev else None
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            young = {"listedAgoH": round((datetime.now(TZ) - created).total_seconds() / 3600, 1)} if created and created > young_cut else None
        if closed and (yes or 0) < 0.5 and kind != "resolved":
            return                                  # a rung resolving No on its own is never news
        if kind == "resolved" and prior is not None:
            change = round(yes - prior, 4)
        change = change or 0
        frm = yes - change if yes is not None else None
        crossed = frm is not None and yes is not None and (frm - 0.5) * (yes - 0.5) < 0
        new_market = points is not None and points < 12 and yes is not None and abs((yes - change) - 0.5) < 0.02
        upset = (abs((1.0 if yes >= 0.5 else 0.0) - prior) if kind == "resolved" and prior is not None and yes is not None else None)
        if kind == "resolved":
            surprise = (upset + (0.3 if upset >= 0.5 else 0)) if upset is not None else 0.15
        elif kind == "news":
            surprise = 0.2                          # Polymarket chose to attach odds to this headline
        elif new_market:
            surprise = 0.08                         # first move of a market listed at 50% is not a move
        elif young and not young.get("rebased"):
            surprise = abs(change) * 0.3            # event created today, no price path to re-base on: discount
        else:
            if abs(change) < MOVER_MIN_CHANGE:
                return                              # under five points is noise on any market
            # half log-odds (30% -> 70% matters more than 1% -> 3%), half plain points (1% -> 7% is still small);
            # crossing 50% is a verdict change
            surprise = 0.5 * min(2.0, abs(logit(yes) - logit(frm)) / 3) + abs(change) + (0.3 if crossed else 0)
            if not closed and (max(yes, frm) < TAIL or min(yes, frm) > 1 - TAIL):
                return                              # 13% -> 5% is still "almost certainly not": not a story
        attention = 1 if vol < VOL_TIERS[0] else 2 if vol < VOL_TIERS[1] else 3
        mention = prev.get(question)
        story = bool(ev and ev.get("pinned"))
        days_left = None
        if story and ev.get("endDate"):
            end = parse_iso(ev["endDate"])
            days_left = math.ceil((end - datetime.now(timezone.utc)).total_seconds() / 86400) if end else None
        # Same market written before: another line needs real movement since then (overlap between days is fine
        # when the number moved). The event-level version of this rule is apply_recent().
        since_move = round(yes - mention["yes"], 3) if (mention and mention.get("yes") is not None and yes is not None) else None
        repeated = bool(mention and since_move is not None and not closed and not crossed
                        and abs(since_move) < (STORY_REWRITE_MOVE if story else REWRITE_MOVE))
        score = round(surprise * attention * (0.2 if repeated else 1.0), 3)
        event_vol = ev["volume24h"] if ev else 0
        headline_ok = (kind in ("official", "mover", "hot", "resolved") and yes is not None
                       and (vol >= HEADLINE_MIN_VOL or event_vol >= HEADLINE_EVENT_MIN_VOL)
                       and (abs(change) >= HEADLINE_MIN_MOVE or crossed or (upset is not None and upset >= 0.5))
                       and (earnings or not (BRACKET.search(label or "") or re.search(r"\bbetween\b", question, re.I)))
                       and not new_market and not repeated)
        words = nouns(question) | nouns(event_title)
        strong = words - WEAK_NOUNS
        causes = [{"time": p["time"], "text": p["text"], "url": p["url"]} for p in posts
                  if (strong & nouns(p["text"])) or len(words & nouns(p["text"])) >= 2][:2]
        c = {
            "kind": kind, "domain": domain, "question": question, "label": label,
            "eventTitle": event_title or (ev["title"] if ev else None), "eventSlug": ev["slug"] if ev else None,
            "eventKind": ev["kind"] if ev else None,
            "yes": yes, "change1d": change,
            "from": round(yes - change, 3) if (yes is not None and kind != "news" and (kind != "resolved" or prior is not None)) else None,
            "prior": prior, "closed": closed, "newMarket": new_market, "young": young or None, "volume24h": vol,
            "score": score, "headlineOk": headline_ok, "story": story, "daysLeft": days_left, "earnings": earnings,
            "repeated": repeated, "lastMention": mention["date"] if mention else None, "sinceMove": since_move,
            "siblings": [{"label": o["label"], "yes": o["yes"]} for o in ev["outcomes"][:4]] if ev and ev["kind"] != "binary" else [],
            "causes": causes, "url": ev["url"] if ev else None,
        }
        if question not in cands or cands[question]["score"] < score:
            cands[question] = c

    # Culture (sports, film, music) rides along as its own domain with a small cap: it only enters the post
    # when a market moved, never as a bare headline.
    priors = {r["question"]: r.get("prior") for rows in day["resolved"].values() for r in rows}  # settled board rows share the day-before price
    for cat, ms in day["official"].items():
        for m in ms:
            ev = by_slug.get(m["eventSlug"])
            add("official", m["question"], m["yes"], m["change1d"], ev["volume24h"] if ev else (m.get("eventVolume24h") or CAND_MIN_VOL24H),
                OFFICIAL_CATEGORIES[cat], ev, closed=m["closed"], points=m["points"], prior=priors.get(m["question"]) if m["closed"] else None,
                young={k: m[k] for k in ("rebased", "firstPrint", "officialChange1d")} if m.get("rebased") else False)
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
            if r.get("expected"):
                continue                            # the market already knew; it is a storyline line, not news
            add("resolved", r["question"], 1.0 if r.get("resultYes") else 0.0, None, r["volume"],
                domain, by_slug.get(r.get("eventSlug")), closed=True, event_title=r["eventTitle"], prior=r.get("prior"))
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

    apply_recent(list(cands.values()), day["date"])
    return cap_picks(list(cands.values()))


# ============================================================================ summary
def outcomes_line(e: dict, n: int = 3) -> str:
    return "; ".join(f"{o['label']} {pct(o['yes'])}{(' ' + delta(o['change1d'])) if o['change1d'] else ''}"
                     for o in e["outcomes"][:n])


def text_summary(day: dict) -> str:
    L = [f"# Polymarket 数据摘要 {day['date']}（生成于 {day['generatedAt']}）",
         f"正文日期行（图注之后、标签之前）照抄：Polymarket 日报 · {day['date'][5:].replace('-0', '/').replace('-', '/')} · {day['stamp']}", ""]

    if day.get("quiet"):
        L += ["## 今天是安静日：没有像样的非文体新闻。正文 4 到 6 条就够，不往下捞小市场凑数；头条用盘面角度；"
              "文体若成交 10 万以上且变 30 个点以上可排第 2 条。正文和图上不提\"安静\"，只在汇报里写。", ""]
    heads = [p for p in day["picks"] if p.get("headlineOk")]
    L += ["## 可当头条的候选（二元、成交够大、变动够大；头条只能从这里选，再按主语是否人人认识排除）"]
    L += [f"- 第 {p['rank']} 名  {p['question']}" for p in heads] or ["- 今天没有满足硬条件的头条，选候选第一名并在汇报里说明"]
    L.append("")
    L += ["## 候选条目（已打分、去重、限额；正文从这里选 5–10 条）",
          "格式：排名 [分数] 类型/领域  从→到  英文原题 [档位] | 事件 | 24h 成交 | 备注；[头条✓] = 满足头条硬条件。picked 里填英文原题，不要填档位"]
    for p in day["picks"]:
        title = p["question"] + (f"  [档位 {p['label']}]" if p["label"] and p["label"] not in ("Yes", p["question"]) else "")
        notes = []
        if p["closed"]:
            notes.append(f"已结算，结算前一天 {pct(p['prior'])}" if p.get("prior") is not None else "已结算，结算前价格未知")
        if p.get("earnings"):
            notes.append("财报类：按事件写，区间就是答案，\"X 三季度某指标落在多少，市场前一天给多少\"")
        if p.get("story"):
            notes.append("长期关注事件" + (f"，还有 {p['daysLeft']} 天揭晓" if p.get("daysLeft") is not None else ""))
        if p["newMarket"]:
            notes.append("今天新上线的档位，起点 50% 是默认值，不算变动")
        elif p.get("young"):
            y = p["young"]
            if y.get("rebased"):
                notes.append(f"24 小时前的 {pct(y['firstPrint'])} 只存在了不到一小时（新上线的开盘首笔或一个尖峰），官方榜写的 "
                             f"{delta(y['officialChange1d'])} 不要用；按稳定价格算起点是 {pct(p['from'])}")
            else:
                notes.append(f"上线才 {y['listedAgoH']} 小时，起点可能是开盘首笔，变动不可靠，只写现在多少")
        if p["repeated"]:
            notes.append(f"{p['lastMention']} 写过这件事，之后只变了 {delta(p.get('sinceMove'))}，不选")
        elif p["lastMention"]:
            notes.append(f"{p['lastMention']} 写过这件事，之后变了 {delta(p.get('sinceMove'))}，可以再写")
        if p.get("writtenDays", 0) >= STORY_WRITTEN_DAYS - 1 and not p.get("story"):
            notes.append(f"7 天内已写 {p['writtenDays']} 次，再写一次就进长期榜")
        if (p["volume24h"] or 0) < SECOND_ITEM_MIN_VOL and not p.get("earnings"):
            notes.append("成交不足 5 万，只能放第 4 条以后")
        if p.get("recentHeadline"):
            notes.append(f"{p['recentHeadline']} 当过标题，今天不能再当标题，可以写在正文里")
        if p.get("related"):
            notes.append("同类加密价格，并进这一句写：" + "; ".join(
                f"{r['question']} {pct(r['from']) if r['from'] is not None else '—'}→{pct(r['yes'])}" for r in p["related"]))
        if p["siblings"]:
            head = "梯子市场，整体看" if p["eventKind"] == "ladder" else "同事件其他选项"
            notes.append(head + "：" + "; ".join(f"{s['label']} {pct(s['yes'])}" for s in p["siblings"]))
        frm = pct(p["from"]) if p["from"] is not None else "—"
        L.append(f"{p['rank']:>2}. [{p['score']:.2f}] {'[头条✓] ' if p.get('headlineOk') else ''}{p['kind']}/{p['domain']}  {frm}→{pct(p['yes'])}  {title}"
                 + (f" | {p['eventTitle']}" if p["eventTitle"] and p["eventTitle"] != title else "")
                 + (f" | {usd(p['volume24h'])}" if p["volume24h"] else "")
                 + (f" | {'；'.join(notes)}" if notes else ""))
        for c in p["causes"]:
            L.append(f"      ↳ 可能的原因（快讯 {c['time'][6:]}）：{c['text'][:160]}")
    L.append("")

    L.append("## 盘面（长期关注事件；正文固定一行只写数字，标 [单独写] 的才展开成一条；已单独成条的不再进盘面行）")
    for b in day["board"]:
        since = (f"比上次报道（{b['sinceDate']} {pct(b['yes'] - b['sinceYes'])}）{delta(b['sinceYes'])}" if b.get("sinceYes") is not None
                 else "之前没写过")
        left = f" · 还有 {b['daysLeft']} 天" if b.get("daysLeft") is not None and b["daysLeft"] <= 30 else ""
        rung = f"{b['label']} " if b["label"] and b["label"] != "Yes" else ""
        how = {"written": "因连续被写入榜", "always": "固定", "volume": "成交持续靠前"}.get(b.get("pinnedBy"), "")
        L.append(f"- {'[单独写] ' if b['write'] else ''}{b['title']} → {rung}{pct(b['yes'])} · 24h {delta(b['change1d'])} · {since}{left} · {usd(b['volume24h'])}{' · ' + how if how else ''}")
    if not day["board"]:
        L.append("- 今天没有长期关注事件（历史不足）")
    L.append("")

    new_rows = [e for d in DOMAINS for e in day["new"][d] if e["volume24h"] >= NEW_MIN_VOL24H][:4]
    L.append("## 新题（24 小时内新开且已有成交；正文最多 1 条，\"Polymarket 开了一个新问题\"）")
    L += [f"- {usd(e['volume24h'])}  {e['title']}  → {outcomes_line(e)}" for e in new_rows] or ["- 今天没有像样的新题"]
    L.append("")

    ern = day.get("earnings") or {}
    L.append("## 财报（\"会不会超预期\"市场，每家只有几千美元成交，只写前一天概率；有结果就在盘面行之后写一行\"财报\"，候选里的财报类条目也并进这一行）")
    for r in ern.get("settled", []):
        cons = f"，市场预期 {r['eps']} EPS {r['consensus']}" if r.get("consensus") else ""
        L.append(f"- {'超预期' if r['beat'] else '没达预期'} · 前一天 {pct(r.get('prior'))} · {r['company']} ({r['ticker']}){cons} · 累计 {usd(r['volume'])}")
    if not ern.get("settled"):
        L.append("- 过去 36 小时没有财报结果")
    for r in ern.get("upcoming", []):
        L.append(f"- 明天发布 · 现在 {pct(r['yes'])} 认为超预期 · {r['company']} ({r['ticker']}) · 累计 {usd(r['volume'])}")
    L.append("")

    L.append("## 大单行（过去 24 小时 1 万美元以上大单的净流向；正文固定一行，标签\"大单\"，写前 2 到 3 个，60 字内）")
    L += [f"- {'+' if f_['net'] >= 0 else '−'}{usd(abs(f_['net']))} 净流向 {'Yes' if f_['net'] >= 0 else 'No'} · {f_['trades']} 笔  {f_['question']}"
          for f_ in day["flows"][:4]] or ["- 今天没有 1 万美元以上的大单，这一行省略"]
    L.append("")

    L.append("## 明天揭晓（48 小时内到期的大市场；正文一行，带当前概率）")
    L += [f"- {e['hoursLeft']} 小时后  {e['title']}  → {outcomes_line(e)}  {usd(e['volume24h'])}" for e in day["upcoming"]] or ["- 48 小时内没有大市场到期"]
    L.append("")

    L.append("## 官方突发榜（polymarket.com/breaking，24h 变动最大）")
    groups = {"politics": "时政与全球", "finance": "金融与加密", "tech": "科技", "culture": "文化体育"}
    for grp, label in groups.items():
        rows = [m for cat, g in OFFICIAL_CATEGORIES.items() if g == grp for m in day["official"].get(cat, [])]
        seen = set()
        L.append(f"### {label}")
        for m in sorted(rows, key=lambda m: abs(m["change1d"] or 0), reverse=True):
            if m["question"] in seen or (m.get("eventVolume24h") is not None and m["eventVolume24h"] < CAND_MIN_VOL24H
                                         and not EARNINGS.search(m["question"])):
                continue
            seen.add(m["question"])
            if len(seen) > 8:
                break
            young = (f"（官方榜 {delta(m['officialChange1d'])} 从只存在了不到一小时的 {pct(m['firstPrint'])} 算起，"
                     f"已改为稳定价格起点）") if m.get("rebased") else ""
            L.append(f"- {delta(m['change1d'])} → {pct(m['yes'])}  {m['question']}{'（已结算）' if m['closed'] else ''}{young}")
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
            L.append("### 刚结算（按信息量 = 成交 × 市场有多意外；写法：前一天市场给 X%，结果成真 / 落空）")
            L += [f"- 结果 {r['result']} · 前一天 {pct(r['prior']) if r.get('prior') is not None else '未知'}"
                  f"{' · 如期' if r.get('expected') else ''} · {usd(r['volume'])}  {r['question']}" for r in day["resolved"][domain]]
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
        "resolved": fetch_resolved(now, events, previous_resolved(date)),
        "earnings": fetch_earnings(now, previous_resolved(date)),
        "upcoming": upcoming_panel(events, now),
        "flows": fetch_flows(events, now),
    }
    by_slug = {e["slug"]: e for e in events.values()}
    # The breaking board carries no volume. Borrow the event's; for boards rows whose event is outside our
    # tag pool, fetch the event (biggest moves first, a dozen at most) so the volume floor can judge them.
    fetched: set[str] = set()
    for cat, ms in day["official"].items():
        for m in sorted(ms, key=lambda m: abs(m["change1d"] or 0), reverse=True):
            slug = m.get("eventSlug")
            if slug and slug not in by_slug and slug not in fetched and len(fetched) < 12:
                fetched.add(slug)
                rows = get(f"{GAMMA}/events", {"slug": slug}) or []
                if rows:
                    dom = OFFICIAL_CATEGORIES[cat]
                    e = event_brief(rows[0], dom if dom in DOMAINS else "politics")
                    e["domain"] = dom
                    events[slug] = by_slug[slug] = e
    for ms in day["official"].values():
        for m in ms:
            ev = by_slug.get(m.get("eventSlug"))
            m["eventVolume24h"] = ev["volume24h"] if ev else None
    day["board"] = board_panel(day, now)
    day["picks"] = build_picks(day, events, movers(events))
    day["quiet"] = not any(p["score"] >= QUIET_SCORE for p in day["picks"] if p["domain"] != "culture")
    day["stats"] = {"events": len(events), "feedPosts": len(day["feed"]),
                    "officialMovers": sum(len(v) for k, v in day["official"].items() if k != "culture"),
                    "resolved": sum(len(v) for v in day["resolved"].values()),
                    "new": sum(len(v) for v in day["new"].values()),
                    "picks": len(day["picks"]), "headlines": sum(1 for p in day["picks"] if p.get("headlineOk")),
                    "requests": _requests}
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
