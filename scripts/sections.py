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

  --prep    before curation: write the drought state into candidates.json
  --record  after publishing: update state/sections.json from the shipped issue
"""

import argparse
import html
import json
import os

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
    "WORTH READING":     ["read"],
}

# Editions dark before the bar moves. Roughly: NUDGE is four days of mornings,
# FLOOR is a week and a half. Both are deliberately slow — the point is to catch
# permanent absence, not to reshuffle a normal quiet stretch.
NUDGE = 4
FLOOR = 9


def norm(s: str) -> str:
    return html.unescape(s or "").strip().upper()


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"editions": 0, "dark": {}}


def prep(cand_path: str) -> None:
    st = load_state()
    cand = json.load(open(cand_path))
    have = cand.get("by_section", {})

    attention = []
    for block, feeders in TRACKED.items():
        dark = int(st.get("dark", {}).get(block, 0))
        if dark < NUDGE:
            continue
        supply = sum(have.get(f, 0) for f in feeders)
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
    with open(cand_path, "w") as f:
        json.dump(cand, f, indent=1)
    if not attention:
        print("  no sections in drought")


def record(issue_path: str) -> None:
    st = load_state()
    issue = json.load(open(issue_path))

    present = set()
    for s in issue.get("sections", []):
        title = norm(s.get("title"))
        blocks = s.get("blocks", []) or []
        if any(b.get("items") for b in blocks):
            present.add(title)
        for b in blocks:
            if b.get("items"):
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
        record(a.issue)
    else:
        ap.error("one of --prep or --record is required")


if __name__ == "__main__":
    main()
