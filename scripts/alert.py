#!/usr/bin/env python3
"""Record a short plain-text alert about the pipeline itself. No model, no deps.

  alert.py SUBJECT BODY

The newsletter is the only email this pipeline sends. Alerts used to go out by
email and as comments on a "COMPASS alerts" GitHub issue (which GitHub turned
into notification emails too); the reader asked for neither, and notices a
missing edition on his own. So an alert is now written only to the run: a
::warning:: annotation on the run page and a section in its step summary, for
whoever opens the Actions tab while working out what happened.

Always exits 0. Callers run it with continue-on-error or `|| true` anyway.
"""

import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: alert.py SUBJECT [BODY]")
        return 0
    subject = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    # An annotation is one line; the body follows in the log and the summary.
    print(f"::warning::{subject}")
    if body:
        print(body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a") as f:
                f.write(f"### {subject}\n\n```\n{body.strip()}\n```\n")
        except OSError as e:
            print(f"step summary not written ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
