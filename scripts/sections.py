#!/usr/bin/env python3
"""Keep thin sections from going permanently dark. Deterministic, no model.

The thin subsections — VIRGINIA, IPOS & LISTINGS, MAVERICKS — legitimately have
nothing to say on many days, and padding them would be worse than omitting them.
But "omit when empty" with no memory has a failure mode: a subsection that is
*slightly* below the bar every single edition never appears at all, and the reader
stops believing it exists.

So absence is made self-correcting. Each edition records which tracked blocks
shipped. The longer a block has been dark, the more the curator is told to lower
its bar — but only ever to the best *genuinely worthwhile* candidate, and only
when candidates for it actually exist. A quiet week for UVA still yields nothing.
A month of near-misses does not.

Satire is neither supply nor coverage. A humor piece can run, labelled, but it
never satisfies a block (prompts/curate.md), so it is not counted when deciding a
block has candidates, and a block holding only satire has not shipped. Nor is a
fallback edition coverage: it is an unranked feed dump, and recording it cleared
IPOS & LISTINGS on 2026-10-09 with press releases nobody chose as IPO news.

  --prep    before curation: write the drought state into candidates.json
  --record  after publishing: update state/sections.json from the shipped issue
"""

import argparse
import html
import json
import os
import re

STATE = "state/sections.json"

# Tracked block -> the candidate sections that could feed it. A block is only ever
# escalated when its feeder sections actually have candidates this run; nudging a
# section with nothing behind it just invites invention.
TRACKED = {
    "VIRGINIA":          ["uva"],
    "IPOS & LISTINGS":   ["ipo"],
    "MAVERICKS":         ["sports"],
    "COWBOYS & NFL":     ["sports"],
    "DALLAS / TEXAS":    ["dallas"],
    "FOOD & DRINK":      ["food"],
    "GOING OUT":         ["goingout"],
    "WORTH READING":     ["read"],
}

# Blocks that share a broad feeder with busier ones. "sports" always carries NFL
# stories, so counting the whole section said the Mavericks had material every
# single run — and after nine dark editions the curator would be REQUIRED to ship
# a Mavericks item in the middle of the offseason, from nothing but other teams'
# news. For these, supply counts only candidates whose title actually names them,
# or that come from the team's own site (whose headlines often name only players).
MATCH = {
    "MAVERICKS": re.compile(r"\bmav(ericks|s)\b", re.I),
}
MATCH_SRC = {
    "MAVERICKS": {"Mavs Moneyball"},
}

# Editions dark before the bar moves, counted in EDITIONS: about 12 a week, a
# morning every day and an afternoon each weekday. NUDGE 8 is four to five days,
# FLOOR 18 a week and a half. These were 4 and 9 under a comment that read them as
# mornings, so "required" arrived in about four weekdays: MAVERICKS went from 1 to
# 9 between 2026-10-07 and 10-12. Both are deliberately slow — the point is to
# catch permanent absence, not to reshuffle a normal quiet stretch.
NUDGE = 8
FLOOR = 18


def norm(s: str) -> str:
    return html.unescape(s or "").strip().upper()


def counts_for(block: str, it: dict) -> bool:
    """Whether a candidate is supply for a tracked block. Satire never is."""
    if it.get("satire") or it.get("section") not in TRACKED.get(block, ()):
        return False
    if block in MATCH:
        return bool(MATCH[block].search(it.get("title") or "")) \
            or it.get("src") in MATCH_SRC.get(block, ())
    return True


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"editions": 0, "dark": {}}


def prep(cand_path: str) -> None:
    # Imported here so --record never depends on the collector importing cleanly.
    from collect import write_candidates

    st = load_state()
    cand = json.load(open(cand_path))
    items = [it for it in cand.get("items") or [] if isinstance(it, dict)]

    attention = []
    for block in TRACKED:
        dark = int(st.get("dark", {}).get(block, 0))
        if dark < NUDGE:
            continue
        # Counted from the items, not by_section, so satire is left out.
        supply = sum(1 for it in items if counts_for(block, it))
        if supply == 0:
            # Nothing to work with. Not the curator's problem this run.
            print(f"  {block}: dark {dark} editions but no candidates — skipping")
            continue
        attention.append({
            "block": block,
            "editions_dark": dark,
            "candidates_available": supply,
            "level": "required" if dark >= FLOOR else "elevated",
        })
        print(f"  {block}: dark {dark} editions, {supply} candidates -> "
              f"{'REQUIRED' if dark >= FLOOR else 'elevated'}")

    cand["attention"] = attention
    write_candidates(cand_path, cand)   # attention near the top, not after the items
    if not attention:
        print("  no sections in drought")


def satire_urls(cand_path: str) -> set:
    """Links the collector flagged as satire. candidates.json is still in the
    workspace when the issue is recorded; without it nothing is excluded."""
    try:
        return {it["url"] for it in json.load(open(cand_path)).get("items") or []
                if isinstance(it, dict) and it.get("satire") and it.get("url")}
    except Exception as e:
        print(f"  {cand_path} unreadable ({type(e).__name__}) — satire not excluded")
        return set()


def record(issue_path: str, cand_path: str) -> None:
    issue = json.load(open(issue_path))
    if issue.get("_fallback"):
        print("  fallback edition — nothing recorded: an unranked feed dump says "
              "nothing about which blocks the curator would have shipped")
        return
    st = load_state()
    satire = satire_urls(cand_path)

    def real(items) -> bool:
        return any(isinstance(i, dict) and i.get("url") not in satire
                   for i in items or [])

    present = set()
    for s in issue.get("sections") or []:
        if not isinstance(s, dict):
            continue
        blocks = [b for b in s.get("blocks") or [] if isinstance(b, dict)]
        shipped = [b for b in blocks if real(b.get("items"))]
        if shipped:
            present.add(norm(s.get("title")))
        for b in shipped:
            present.add(norm(b.get("label")))

    st["editions"] = int(st.get("editions", 0)) + 1
    dark = st.setdefault("dark", {})
    for block in TRACKED:
        if block in present:
            if dark.get(block):
                print(f"  {block}: shipped — drought broken after {dark[block]}")
            dark[block] = 0
        else:
            dark[block] = int(dark.get(block, 0)) + 1

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    dry = {k: v for k, v in sorted(dark.items()) if v}
    print(f"  edition {st['editions']} recorded; dark: {dry or 'none'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--candidates", default="candidates.json")
    ap.add_argument("--issue", default="issue.json")
    a = ap.parse_args()
    if a.prep:
        prep(a.candidates)
    elif a.record:
        record(a.issue, a.candidates)
    else:
        ap.error("one of --prep or --record is required")


if __name__ == "__main__":
    main()
