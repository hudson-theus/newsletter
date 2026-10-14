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
aside (with the curator's execution log, when there is one) for the run's
artifact, which also leaves no issue.json behind for the next attempt to be
confused by. Exit 0 = ship it, exit 1 = retry or fall back.

  check_issue.py                                   validate only
  check_issue.py --reject-to rejected --label a1 \
                 --log "$RUNNER_TEMP/claude-execution-output.json"
"""

import argparse
import json
import os
import shutil
import sys

# A real curated issue runs 40-70 items; the fallback alone carries ~60. Anything
# this thin is a curator that stopped early, not a quiet news day.
MIN_ITEMS = 10


def problems(path: str) -> list[str]:
    """Every reason the file cannot ship. Empty list = shippable."""
    if not os.path.exists(path):
        return [f"{path} was not written"]
    try:
        with open(path) as f:
            issue = json.load(f)
    except json.JSONDecodeError as e:
        return [f"{path} is not valid JSON: {e}"]
    except Exception as e:
        return [f"{path} could not be read: {type(e).__name__}: {e}"]
    if not isinstance(issue, dict):
        return [f"top level is {type(issue).__name__}, not an object"]

    sections = issue.get("sections")
    if not isinstance(sections, list):
        return [f"'sections' is {type(sections).__name__}, not a list"]

    out, items = [], 0
    for n, s in enumerate(sections):
        if not isinstance(s, dict):
            out.append(f"section {n} is {type(s).__name__}, not an object")
            continue
        # null blocks/items are an empty block, not a defect: publish.py reads
        # them the same way.
        for b in s.get("blocks") or []:
            if not isinstance(b, dict):
                out.append(f"a block in {s.get('title')!r} is not an object")
                continue
            for i in b.get("items") or []:
                # publish.py reads it["text"] unguarded, so a missing key would
                # crash the send; an empty string merely does not count.
                if not (isinstance(i, dict) and isinstance(i.get("text"), str)):
                    out.append(f"an item in {s.get('title')!r} has no 'text' string")
                elif i["text"].strip():
                    items += 1
    if items < MIN_ITEMS:
        out.append(f"only {items} items with text (need at least {MIN_ITEMS})")
    fm = issue.get("front_matter")
    if not (isinstance(fm, str) and fm.strip()):
        # Worth seeing, not worth rejecting: a full curated issue without its
        # opening sentence still beats the unedited fallback.
        print("::warning::front_matter is missing or empty — shipping without it")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", default="issue.json")
    ap.add_argument("--reject-to", metavar="DIR",
                    help="on rejection, move the issue (and --log) here")
    ap.add_argument("--label", default="check", help="name for the rejected files")
    ap.add_argument("--log", help="curator execution log to keep on rejection")
    a = ap.parse_args()

    bad = problems(a.issue)
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
            # Moved, not copied: an attempt that dies before writing its own log
            # must not have this one's filed under its name.
            shutil.move(a.log, os.path.join(a.reject_to, f"execution.{a.label}.json"))
        with open(os.path.join(a.reject_to, "reasons.txt"), "a") as f:
            for p in bad:
                f.write(f"{a.label}: {p}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
