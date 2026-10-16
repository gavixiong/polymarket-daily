#!/usr/bin/env python3
"""Verify that every row the six Xiaohongshu cards will show has a Chinese label.

    python3 scripts/check_zh.py [YYYY-MM-DD]

Exit 0 when complete. Exit 1 and list what is missing otherwise, in the exact
JSON shape to add to reports/<date>.zh.json. Mirrors the row selection in
assets/shared.js (officialRows / hotRows / resolvedRows / newRows).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = {"politics": ["politics", "world"], "finance": ["finance", "crypto"], "tech": ["tech"]}
DOMAINS = ["politics", "finance", "tech"]
# Outcome labels that need no translation: built into shared.js, dates, numbers, arrows, proper nouns.
BUILTIN = {"Yes", "No", "No change", "Democratic Party", "Republican Party", "Democrats Sweep", "Republicans Sweep",
           "R Senate, D House", "D Senate, R House", "25 bps increase", "50+ bps increase", "25 bps decrease",
           "50+ bps decrease", "Other"}
DATE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}(, \d{4})?$")
NUMERIC = re.compile(r"^[↑↓<>≥≤]?\s*[\$€£]?[\d.,]+[%+kKMB]*(\s*[-–]\s*[\$]?[\d.,]+[%kKMB]*)?\+?$")
PROPER = re.compile(r"^(?:[A-Z][\w'’.\-]*)(?:\s(?:de|da|do|von|van|of|the|bin|al|el|&|[A-Z][\w'’.\-]*))*$")


def by_abs_change(rows):
    return sorted(rows, key=lambda m: abs(m.get("change1d") or 0), reverse=True)


def official_rows(day, group, skip, n=8):
    seen, out = set(), []
    for m in by_abs_change([m for c in GROUPS[group] for m in day["official"].get(c, [])]):
        if m["question"] in seen or m["question"] in skip:
            continue
        seen.add(m["question"])
        out.append(m)
    return out[:n]


def breaking_rows(day, skip, n=8):
    rows = by_abs_change([m for g in GROUPS for m in official_rows(day, g, skip)])
    seen, out = set(), []
    for m in rows:
        if m["question"] not in seen:
            seen.add(m["question"])
            out.append(m)
    return out[:n]


def hot_rows(day, n=8):
    pinned = [e for d in DOMAINS for e in day["hot"][d] if e.get("pinned")]
    rest = [e for d in DOMAINS for e in day["hot"][d][:3] if not e.get("pinned")]
    return (pinned + rest)[:n]


def resolved_rows(day, n=8):
    seen, out = set(), []
    for r in sorted((r for d in DOMAINS for r in day["resolved"][d]), key=lambda r: r["volume"], reverse=True):
        if r["question"] not in seen:
            seen.add(r["question"])
            out.append(r)
    return out[:n]


def new_rows(day, n=6):
    posts = [p["text"] for p in day["feed"] if p["kind"] == "new"]
    seen = set(posts)
    titles = posts + [e["title"] for d in DOMAINS for e in day["new"][d] if not (e["title"] in seen or seen.add(e["title"]))]
    return titles[:n]


def outcome_ok(label: str, zh: dict) -> bool:
    return (label in BUILTIN or label in zh.get("outcomes", {}) or bool(DATE.match(label))
            or bool(NUMERIC.match(label)) or bool(PROPER.match(label)) or bool(re.search(r"[一-鿿]", label)))


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else json.loads((ROOT / "data" / "index.json").read_text())["latest"]
    day = json.loads((ROOT / "data" / f"{date}.json").read_text())
    zh_path = ROOT / "reports" / f"{date}.zh.json"
    try:
        zh = json.loads(zh_path.read_text())
    except FileNotFoundError:
        print(f"[error] {zh_path.name} 不存在", file=sys.stderr)
        return 1
    labels, skip = zh.get("labels", {}), set(zh.get("skip", []))
    missing_labels, missing_outcomes, missing_fields = [], [], []

    for key in ("headline", "hero", "headlineQuestion", "headlineTitle"):
        if not zh.get(key):
            missing_fields.append(key)

    need = []
    need += [("突发榜", m["question"]) for m in breaking_rows(day, skip)]
    for e in hot_rows(day):
        need.append(("长期盘", e["title"]))
        rungs = e["outcomes"][:3] if e["kind"] == "exclusive" else e["outcomes"][:4]
        for o in rungs:
            if o["label"] and not outcome_ok(o["label"], zh):
                missing_outcomes.append(o["label"])
    need += [("结算", r["question"]) for r in resolved_rows(day)]
    # "新盘" and "快讯" cards are dashboard-only previews, not checked.
    if zh.get("headlineQuestion"):
        need.append(("头条", zh["headlineQuestion"]))
    for card, q in need:
        if q not in labels or not re.search(r"[一-鿿]", labels[q]):
            missing_labels.append((card, q))

    if not (missing_labels or missing_outcomes or missing_fields):
        print(f"[ok] {date}: 六张图上的 {len(need)} 行都有中文", file=sys.stderr)
        return 0
    print(f"[fail] {date}: 中文覆盖不完整，把下面的内容补进 reports/{date}.zh.json", file=sys.stderr)
    if missing_fields:
        print("缺少字段: " + ", ".join(missing_fields))
    if missing_labels:
        print('"labels" 需要补:')
        for card, q in missing_labels:
            print(f'  {json.dumps(q, ensure_ascii=False)}: "<{card}·中文短标题>",')
    if missing_outcomes:
        print('"outcomes" 需要补（选项名）:')
        for o in dict.fromkeys(missing_outcomes):
            print(f'  {json.dumps(o, ensure_ascii=False)}: "<中文>",')
    return 1


if __name__ == "__main__":
    sys.exit(main())
