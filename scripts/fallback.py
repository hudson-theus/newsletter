#!/usr/bin/env python3
"""Last-resort issue builder. Deterministic, no model.

The curation step is the only part of this pipeline that can fail for reasons
outside the repo's control. On 2026-08-27 it did: the afternoon edition died in
423ms with an auth rejection, and because nothing stood behind it, that edition
simply never existed. One transient blip, one missing brief.

This is what stands behind it. Given the same candidates.json the curator would
have read, it writes a plain issue.json — real headlines, real links, grouped into
the standing sections, no commentary and no ranking beyond recency. It is visibly
plainer than a curated issue and says so in its front matter, which is the point:
the reader can tell at a glance that the model stage did not run, and still knows
what happened today.

A plain brief beats no brief. Never let this file raise.
"""

import json
import sys

# (section title, subsection label or None, candidate section keys, max items)
LAYOUT = [
    ("THE ECONOMY",        None,                       ["economy"],          6),
    ("CRE SNAPSHOT",       None,                       ["cre"],              4),
    ("CRE DESK",           "RETAIL",                   ["retail"],           4),
    ("DEAL FLOW",          "VC",                       ["vc"],              12),
    ("DEAL FLOW",          "IPOS &amp; LISTINGS",      ["ipo"],              6),
    ("DEAL FLOW",          "M&amp;A / STRATEGIC",      ["deals"],            8),
    ("UNITED STATES",      None,                       ["us"],               6),
    ("THE WORLD",          None,                       ["world"],            7),
    ("DALLAS / TEXAS",     None,                       ["dallas"],           5),
    ("SPORTS",             None,                       ["sports", "uva"],    5),
    ("WORTH READING",      None,                       ["read"],             4),
]


def main() -> None:
    cands = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "candidates.json"))
    pool: dict[str, list] = {}
    for i in cands.get("items", []):
        pool.setdefault(i.get("section", ""), []).append(i)

    sections, used = [], set()
    for title, label, keys, cap in LAYOUT:
        items = []
        for k in keys:
            for c in pool.get(k, []):
                if c["url"] in used or len(items) >= cap:
                    continue
                used.add(c["url"])
                items.append({"text": c["title"], "src": c["src"], "url": c["url"]})
        if not items:
            continue
        existing = next((s for s in sections if s["title"] == title), None)
        if existing is None:
            sections.append({"title": title, "blocks": [{"label": label,
                                                         "items": items}]})
        else:
            existing["blocks"].append({"label": label, "items": items})

    total = sum(len(b["items"]) for s in sections for b in s["blocks"])
    issue = {
        # No greeting here: publish.py prepends it, so this line stays a
        # sentence and the reader is addressed by name even on a fallback issue.
        "front_matter": ("The curation step did not complete, so this is an "
                         "unedited edition: real headlines from today's sources, "
                         "grouped but not ranked or written up."),
        "sections": [{"title": "FRONT MATTER", "blocks": []}] + sections,
    }
    with open("issue.json", "w") as f:
        json.dump(issue, f, indent=1)
    print(f"fallback issue written — {total} items across {len(sections)} sections")


if __name__ == "__main__":
    main()
