#!/usr/bin/env python3
"""Stage 3 of 3: verify, render, send. Deterministic, no model.

Two independent guards against bad links, in order of strength:

  1. Provenance. Every URL in issue.json must appear in candidates.json. The model
     only ever sees that candidate list, so a URL outside it was invented. This makes
     fabrication structurally impossible rather than merely detectable.
  2. Resolution. Whatever survives is fetched. 404/410 loses its link.

An item that fails either check ships unlinked rather than being dropped, per the
spec: an unlinked true item is fine, a confidently wrong link is not.
"""

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import os
import smtplib
import sys
import time
import urllib.request
import urllib.error
from email.message import EmailMessage
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from compass import head, sec, sub, item, note, lead, foot, wrap  # noqa: E402

CT = ZoneInfo("America/Chicago")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def resolves(url: str) -> tuple[str, bool]:
    """403 still renders for a human; only a missing page disqualifies a link."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return url, r.status < 400
        except urllib.error.HTTPError as e:
            return url, e.code not in (404, 410)
        except Exception:
            # Timeout, DNS blip, reset. Provenance already vouches for this URL —
            # it came out of a feed parsed minutes ago — so a transient failure is
            # not evidence the page is gone. Retry once, then keep the link rather
            # than silently degrading a good item.
            if attempt == 2:
                print(f"  (unreachable, keeping link on provenance): {url}")
                return url, True
    return url, True


def all_items(issue: dict):
    for s in issue["sections"]:
        for b in s.get("blocks", []):
            for i in b.get("items", []):
                yield i


def verify(issue: dict, allowed: set[str]) -> dict:
    invented_seen = set()
    for i in all_items(issue):
        if i.get("url") and i["url"] not in allowed:
            if i["url"] not in invented_seen:
                print(f"  INVENTED (not in candidates): {i['url']}")
            invented_seen.add(i["url"])
            i["url"] = None

    urls = {i["url"] for i in all_items(issue) if i.get("url")}
    dead = set()
    if urls:
        with futures.ThreadPoolExecutor(max_workers=12) as ex:
            for url, ok in ex.map(resolves, urls):
                if not ok:
                    dead.add(url)
                    print(f"  DEAD (failed to resolve): {url}")
    for i in all_items(issue):
        if i.get("url") in dead:
            i["url"] = None

    invented_urls = {i for i in invented_seen}
    issue["_stats"] = {"checked": len(urls) + len(invented_urls),
                       "invented": len(invented_urls), "dead": len(dead)}
    return issue


def render(issue: dict, edition: str, now: dt.datetime) -> str:
    st = issue["_stats"]
    rows = [head(edition, f"{now:%A, %B %-d, %Y}".upper(), f"{now:%-I:%M %p} CT")]
    count = 0
    for s in issue["sections"]:
        rows.append(sec(edition, s["title"]))
        if s["title"].upper().startswith("FRONT"):
            fm = issue.get("front_matter")
            if fm:
                rows.append(lead(fm))
            continue
        for b in s.get("blocks", []):
            if b.get("label"):
                rows.append(sub(b["label"]))
            items = b.get("items", [])
            for n, it in enumerate(items):
                rows.append(item(edition, it["text"], it.get("src"), it.get("url"),
                                 rule=n != len(items) - 1))
                count += 1
    dropped = st["invented"] + st["dead"]
    rows.append(foot(edition,
        f"COMPASS / {'MORNING' if edition == 'am' else 'AFTERNOON'} EDITION / "
        f"{now:%-d %b %Y}".upper() + "<br>"
        f"{count} items. {st['checked']} links checked, {st['checked'] - dropped} shipped, "
        f"{dropped} dropped ({st['invented']} unsourced, {st['dead']} unresolved)."))
    issue["_stats"]["items"] = count
    return wrap(rows)


def send(html: str, edition: str, now: dt.datetime) -> None:
    msg = EmailMessage()
    # [SPREAD] is a routing token the Apps Script matches on, not a display name.
    msg["Subject"] = f"[SPREAD] COMPASS {'AM' if edition == 'am' else 'PM'} / {now:%b %-d}"
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["RECIPIENT"]
    msg.set_content("COMPASS is an HTML email. Enable HTML to read it.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        try:
            s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        except smtplib.SMTPAuthenticationError as e:
            # A raw traceback here is unreadable in a cron log, and the cause is
            # almost always one of a short list. Say so.
            raise SystemExit(
                f"Gmail rejected the login ({e.smtp_code}).\n"
                f"  GMAIL_ADDRESS is {os.environ['GMAIL_ADDRESS']!r}\n"
                "  Check, in order:\n"
                "    1. GMAIL_APP_PASSWORD is a 16-character app password, not the\n"
                "       account password.\n"
                "    2. It was stored with the spaces removed (Google displays it\n"
                "       as 4 groups of 4).\n"
                "    3. 2-step verification is on for that account — app passwords\n"
                "       do not exist without it.\n"
                "    4. The app password was generated for this exact address.\n"
            ) from None
        s.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], required=True)
    ap.add_argument("--issue", default="issue.json")
    ap.add_argument("--candidates", default="candidates.json")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--send-at", metavar="HH:MM",
                    help="hold the send until this Chicago time, so delivery lands "
                         "on the minute regardless of when the runner started")
    args = ap.parse_args()

    now = dt.datetime.now(CT)
    issue = json.load(open(args.issue))
    allowed = {i["url"] for i in json.load(open(args.candidates))["items"]}

    print(f"verifying against {len(allowed)} sourced URLs")
    issue = verify(issue, allowed)
    html = render(issue, args.edition, now)

    out = f"issue_{now:%Y-%m-%d}_{args.edition}.html"
    with open(out, "w") as f:
        f.write(html)
    st = issue["_stats"]
    print(f"wrote {out} — {st['checked']} checked, "
          f"{st['invented']} invented, {st['dead']} dead")

    if args.send and st["items"] == 0:
        raise SystemExit("issue has zero items — refusing to send an empty brief")
    if args.send:
        if args.send_at:
            hh, mm = (int(x) for x in args.send_at.split(":"))
            target = dt.datetime.now(CT).replace(hour=hh, minute=mm, second=0,
                                                 microsecond=0)
            wait = (target - dt.datetime.now(CT)).total_seconds()
            if wait > 0:
                # The runner may have started anywhere in a two-hour window; idling
                # here is what converts an unreliable start into an exact delivery.
                print(f"built at {dt.datetime.now(CT):%H:%M:%S} — holding "
                      f"{wait/60:.1f} min to send at {target:%H:%M:00} CT")
                time.sleep(wait)
            else:
                print(f"target {args.send_at} CT already passed — sending now "
                      f"({-wait/60:.1f} min late)")
        send(html, args.edition, now)
        print(f"sent at {dt.datetime.now(CT):%H:%M:%S} CT")
    else:
        print("not sent (COMPASS_SEND is not 'true')")


if __name__ == "__main__":
    main()
