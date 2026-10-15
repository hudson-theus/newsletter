#!/usr/bin/env python3
"""Is this curated issue.json shippable? Deterministic, no model.

Runs after EVERY curation attempt, not once after all three. On 2026-10-09 the
afternoon edition shipped unedited: attempt 1 exited 0 after 107 seconds, so the
retry gates (which only looked at the step's outcome) skipped attempts 2 and 3,
and the single check at the end found nothing usable and built the fallback. Two
retries that might have produced a real issue never ran. Worse, that check sent
its error to /dev/null and fallback.py overwrote issue.json, so why the curated
file was rejected could not be recovered afterwards.

So now: a rejected file says why, in the log, and with --reject-to it is moved
aside (with a summary of the curator's execution log, when there is one) for the
run's artifact, which also leaves no issue.json behind for the next attempt to be
confused by. Exit 0 = ship it, exit 1 = retry or fall back.

The shape rules live in normalize(), which publish.py imports, so "the check
passed" and "publish.py can render it" are the same statement.

  check_issue.py                                   validate only
  check_issue.py --reject-to rejected --label a1 \
                 --log "$RUNNER_TEMP/claude-execution-output.json"
"""

import argparse
import html
import json
import os
import shutil
import sys

# A real curated issue runs 40-70 items; the fallback alone carries ~60. Anything
# this thin is a curator that stopped early, not a quiet news day.
MIN_ITEMS = 10

# The canonical titles and block labels, compared after html.unescape and upper,
# the same way sections.py --record credits them. A heading the tracker cannot
# match ("DALLAS/TEXAS", "IPOs and Listings") silently counts that block as dark,
# so a spelling slip would escalate a section that actually shipped. Warned about
# only: a curated issue with a misspelt heading still beats the fallback.
CANON = {
    "FRONT MATTER":    (),
    "THE ECONOMY":     (),
    "CRE SNAPSHOT":    (),
    "CRE DESK":        ("RETAIL", "PROPTECH & CRE-TECH"),
    "DEAL FLOW":       ("VC", "IPOS & LISTINGS", "M&A / STRATEGIC", "DEBT",
                        "FUNDS & SECONDARIES", "DISTRESSED"),
    "AI & ENTERPRISE": (),
    "UNITED STATES":   (),
    "THE WORLD":       (),
    "DALLAS / TEXAS":  ("NEWS", "FOOD & DRINK", "GOING OUT"),
    "SPORTS":          ("COWBOYS & NFL", "MAVERICKS", "VIRGINIA"),
    "WORTH READING":   (),
}

_STR = (str, type(None))
_LIST = (list, type(None))


def _canon(s: str | None) -> str:
    return html.unescape(s or "").strip().upper()


def _kind(v) -> str:
    return type(v).__name__


def normalize(issue) -> tuple[dict | None, list[str]]:
    """The one definition of a well-formed issue, shared with publish.py.

    Returns a cleaned copy plus every defect it had to repair. This file rejects
    an issue with any defect; publish.py renders the cleaned copy. Before the
    two shared it, the check passed a list-typed front_matter, an untitled
    section and a list-typed url, and each then crashed publish.py after the
    fallback step had already trusted the check, so nothing went out at all.
    None means there is nothing to render.
    """
    if not isinstance(issue, dict):
        return None, [f"top level is {_kind(issue)}, not an object"]
    sections = issue.get("sections")
    if not isinstance(sections, list):
        return None, [f"'sections' is {_kind(sections)}, not a list"]

    bad = []
    fm = issue.get("front_matter")
    if not isinstance(fm, _STR):
        bad.append(f"front_matter is {_kind(fm)}, not a string")
        fm = None

    clean = []
    for n, s in enumerate(sections):
        if not isinstance(s, dict):
            bad.append(f"section {n} is {_kind(s)}, not an object")
            continue
        title = s.get("title")
        if not (isinstance(title, str) and title.strip()):
            bad.append(f"section {n} has no 'title' string")
            continue
        # null blocks/items are an empty block, not a defect: publish.py reads
        # them the same way.
        blocks = s.get("blocks")
        if not isinstance(blocks, _LIST):
            bad.append(f"'blocks' in {title!r} is {_kind(blocks)}, not a list")
        blocks_out = []
        for b in blocks if isinstance(blocks, list) else []:
            if not isinstance(b, dict):
                bad.append(f"a block in {title!r} is not an object")
                continue
            label = b.get("label")
            if not isinstance(label, _STR):
                bad.append(f"a block label in {title!r} is {_kind(label)}, not a string")
                label = None
            items = b.get("items")
            if not isinstance(items, _LIST):
                bad.append(f"'items' in {title!r} is {_kind(items)}, not a list")
            items_out = []
            for i in items if isinstance(items, list) else []:
                # publish.py reads it["text"] unguarded, so a missing key would
                # crash the send; an empty string merely does not count.
                if not (isinstance(i, dict) and isinstance(i.get("text"), str)):
                    bad.append(f"an item in {title!r} has no 'text' string")
                    continue
                i = dict(i)
                for k in ("url", "src"):
                    if not isinstance(i.get(k), _STR):
                        bad.append(f"an item in {title!r} has a {_kind(i[k])} {k!r}")
                        i[k] = None
                items_out.append(i)
            blocks_out.append({**b, "label": label, "items": items_out})
        clean.append({**s, "title": title, "blocks": blocks_out})
    return {**issue, "front_matter": fm, "sections": clean}, bad


def lint(issue: dict) -> list[str]:
    """Editorial slips worth seeing in the log, never worth rejecting for.
    Takes a normalized issue."""
    out = []
    for s in issue["sections"]:
        t = _canon(s["title"])
        labels = CANON.get(t)
        if labels is None:
            out.append(f"section title {s['title']!r} is not canonical — "
                       "sections.py will not credit it")
            continue
        for b in s["blocks"]:
            lab = b.get("label")
            if lab is not None and b["items"] and _canon(lab) not in labels:
                out.append(f"block label {lab!r} under {s['title']!r} is not canonical")
    # publish.py unwraps all markup but b/strong/i/em, so a link written into
    # text never ships live. Still worth knowing the curator is doing it.
    texts = [issue.get("front_matter") or ""] + [
        i["text"] for s in issue["sections"] for b in s["blocks"] for i in b["items"]]
    n = sum(1 for t in texts if "<a " in t.lower() or "<img" in t.lower())
    if n:
        out.append(f"{n} text field(s) carry <a> or <img> markup — stripped at "
                   "publish; the url field is the only link")
    return out


def load(path: str) -> tuple[dict | None, list[str]]:
    """The normalized issue (None if unreadable) and every reason it cannot ship."""
    if not os.path.exists(path):
        return None, [f"{path} was not written"]
    try:
        with open(path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return None, [f"{path} is not valid JSON: {e}"]
    except Exception as e:
        return None, [f"{path} could not be read: {type(e).__name__}: {e}"]
    issue, out = normalize(raw)
    if issue is None:
        return None, out
    items = sum(1 for s in issue["sections"] for b in s["blocks"]
                for i in b["items"] if i["text"].strip())
    if items < MIN_ITEMS:
        out.append(f"only {items} items with text (need at least {MIN_ITEMS})")
    return issue, out


def problems(path: str) -> list[str]:
    """Every reason the file cannot ship. Empty list = shippable."""
    return load(path)[1]


def _summarize(log: str, dest: str) -> None:
    """Keep the curator's run as a summary, never as the raw transcript.

    The repo is public, so every artifact is downloadable by any GitHub user,
    and the execution log is the full SDK transcript that claude-code-action
    itself keeps out of the run log ("full output hidden for security"). The
    summary (turns, cost, denials, final text) is what diagnosing 2026-10-09
    actually needed. The raw file is deleted whatever happens here, which also
    keeps an attempt that dies before writing its own log from having this
    one's filed under its name.
    """
    try:
        import curator_summary
        s = curator_summary.summarize(log)
        if s is None:
            print(f"  {log}: no readable execution log to summarize")
        else:
            with open(dest, "w") as f:
                json.dump(s, f, indent=1)
    except Exception as e:
        print(f"::warning::could not summarize {log} ({type(e).__name__}: {e})")
    try:
        os.remove(log)
    except OSError as e:
        print(f"::warning::could not delete {log} ({type(e).__name__}: {e})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="issue.json")
    ap.add_argument("--reject-to", metavar="DIR",
                    help="on rejection, move the issue (and a summary of --log) here")
    ap.add_argument("--label", default="check", help="name for the rejected files")
    ap.add_argument("--log", help="curator execution log to summarize on rejection")
    a = ap.parse_args()

    issue, bad = load(a.issue)
    if issue is not None:
        for w in lint(issue):
            print(f"::warning::{a.label}: {w}")
        if not (issue["front_matter"] or "").strip():
            # Worth seeing, not worth rejecting: a full curated issue without its
            # opening sentence still beats the unedited fallback.
            print("::warning::front_matter is missing or empty — shipping without it")
    if not bad:
        print(f"{a.issue}: ok")
        return

    for p in bad:
        print(f"::warning::{a.label}: {p}")
    if a.reject_to:
        os.makedirs(a.reject_to, exist_ok=True)
        if os.path.exists(a.issue):
            shutil.move(a.issue, os.path.join(a.reject_to, f"issue.{a.label}.json"))
        if a.log and os.path.exists(a.log):
            _summarize(a.log, os.path.join(a.reject_to,
                                           f"execution.{a.label}.summary.json"))
        with open(os.path.join(a.reject_to, "reasons.txt"), "a") as f:
            for p in bad:
                f.write(f"{a.label}: {p}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
