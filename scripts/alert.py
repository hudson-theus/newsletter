#!/usr/bin/env python3
"""Send a short plain-text alert about the pipeline itself. No model, no deps.

Silence used to be ambiguous: when an edition did not arrive there was nothing to
distinguish "nothing happened today" from "the pipeline is broken". The reader
found out by not receiving a brief. This makes a miss say so.

  alert.py SUBJECT BODY

Two channels, because the first one shares its failure modes with the brief. The
email goes through the same Gmail account and app password that deliver the
edition, so a revoked password, an SMTP block or a Gmail incident silenced the
edition and its alert together. The second channel is a comment on an open
GitHub issue titled "COMPASS alerts" (created on first use), which GitHub turns
into its own notification. It needs GH_TOKEN and GITHUB_REPOSITORY in the
environment and `issues: write` on the workflow.

Exit 0 when at least one channel delivered, 1 when both failed. Callers run it
with continue-on-error; the exit code is for the log, not a gate.
"""

import json
import os
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage

ISSUE_TITLE = "COMPASS alerts"

# smtplib's default is no timeout at all (socket.getdefaulttimeout() is None), so
# a hung TLS session used to sit until the job timeout.
SMTP_TIMEOUT = 60
SMTP_ATTEMPTS = 2
GH_TIMEOUT = 60


def by_email(subject: str, body: str) -> bool:
    try:
        addr, pw, to = (os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"],
                        os.environ["RECIPIENT"])
    except KeyError as e:
        print(f"  email: not configured ({e.args[0]} is unset)")
        return False
    if not (addr and pw and to):
        print("  email: not configured (empty Gmail secret)")
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[SPREAD] {subject}"
    msg["From"] = addr
    msg["To"] = to
    msg.set_content(body)
    for attempt in range(1, SMTP_ATTEMPTS + 1):
        try:
            s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=SMTP_TIMEOUT)
            try:
                s.login(addr, pw)
                s.send_message(msg)
            finally:
                # The message is accepted by now; a QUIT that does not answer 221
                # must not turn a delivered alert into a "failed" one.
                try:
                    s.quit()
                except Exception:
                    pass
            print(f"  email: sent (attempt {attempt})")
            return True
        except smtplib.SMTPAuthenticationError as e:
            # Retrying a rejected password only adds a lockout risk.
            print(f"  email: Gmail rejected the login ({e.smtp_code})")
            return False
        except Exception as e:
            print(f"  email: attempt {attempt} failed ({type(e).__name__}: {e})")
            if attempt < SMTP_ATTEMPTS:
                time.sleep(15)
    return False


def gh(*args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                         timeout=GH_TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: "
                           f"{(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


def by_issue(subject: str, body: str) -> bool:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (os.environ.get("GH_TOKEN") and repo):
        print("  issue: not configured (needs GH_TOKEN and GITHUB_REPOSITORY)")
        return False
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    # Hard line breaks rather than a code block, so the run links stay clickable.
    lines = "  \n".join(body.strip().splitlines())
    text = f"**{subject}**  \n_{stamp}_\n\n{lines}\n"
    try:
        # The issues listing rather than search: search is indexed with a delay,
        # and two alerts a minute apart must not open two issues.
        raw = gh("api", "--paginate",
                 f"repos/{repo}/issues?state=open&per_page=100",
                 "--jq", '.[] | select(.pull_request == null) | [.number, .title] | @json')
        number = None
        for line in raw.splitlines():
            n, title = json.loads(line)
            if title == ISSUE_TITLE:
                number = n
                break
        if number is None:
            gh("api", f"repos/{repo}/issues", "--method", "POST",
               "-f", f"title={ISSUE_TITLE}", "-f", f"body={text}", "--silent")
            print("  issue: opened a new 'COMPASS alerts' issue")
        else:
            gh("api", f"repos/{repo}/issues/{number}/comments", "--method", "POST",
               "-f", f"body={text}", "--silent")
            print(f"  issue: commented on #{number}")
        return True
    except Exception as e:
        print(f"  issue: failed ({type(e).__name__}: {e})")
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: alert.py SUBJECT [BODY]")
        return 1
    subject = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"alert: {subject}")
    # Both, always: the point of the second channel is that it does not depend on
    # the first having failed in a way this script can see.
    mailed = by_email(subject, body)
    filed = by_issue(subject, body)
    if not (mailed or filed):
        print("::warning::alert could not be delivered on either channel")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
