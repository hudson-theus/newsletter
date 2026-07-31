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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return url, r.status < 400
    except urllib.error.HTTPError as e:
        return url, e.code not in (404, 410)
    except Exception:
        return url, False


def all_items(issue: dict):
    for s in issue["sections"]:
        for b in s.get("blocks", []):
            for i in b.get("items", []):
                yield i


def verify(issue: dict, allowed: set[str]) -> dict:
    invented = [i for i in all_items(issue) if i.get("url") and i["url"] not in allowed]
    for i in invented:
        print(f"  INVENTED (not in candidates): {i['url']}")
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

    issue["_stats"] = {"checked": len(urls) + len(invented),
                       "invented": len(invented), "dead": len(dead)}
    return issue


def render(issue: dict, edition: str, now: dt.datetime) -> str:
    st = issue["_stats"]
    rows = [head(edition, f"{now:%A, %B %-d, %Y}".upper(), f"{now:%-I:%M %p} CT")]
    count = 0
    for s in issue["sections"]:
        rows.append(sec(edition, s["title"]))
        if s["title"].upper().startswith("FRONT"):
            rows.append(lead(issue["front_matter"]))
            continue
        for b in s.get("blocks", []):
            if b.get("label"):
                rows.append(sub(b["label"]))
            items = b.get("items", [])
            for n, it in enumerate(items):
                rows.append(item(it["text"], it.get("src"), it.get("url"),
                                 rule=n != len(items) - 1))
                count += 1
    dropped = st["invented"] + st["dead"]
    rows.append(foot(edition,
        f"COMPASS / {'MORNING' if edition == 'am' else 'AFTERNOON'} EDITION / "
        f"{now:%-d %b %Y}".upper() + "<br>"
        f"{count} items. {st['checked']} links checked, {st['checked'] - dropped} shipped, "
        f"{dropped} dropped ({st['invented']} unsourced, {st['dead']} unresolved)."))
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
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], required=True)
    ap.add_argument("--issue", default="issue.json")
    ap.add_argument("--candidates", default="candidates.json")
    ap.add_argument("--send", action="store_true")
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

    if args.send:
        send(html, args.edition, now)
        print("sent")
    else:
        print("not sent (COMPASS_SEND is not 'true')")


if __name__ == "__main__":
    main()
