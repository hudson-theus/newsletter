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
import art  # noqa: E402
from compass import head, sec, sub, item, note, lead, foot, wrap, greet  # noqa: E402

CT = ZoneInfo("America/Chicago")

# The reader is addressed by name, and the greeting is set here rather than by the
# curator. A model that forgets it once would silently drop the most personal line
# in the brief; generated here it is guaranteed, and fallback.py inherits it free.
NAME = os.environ.get("COMPASS_NAME", "Hudson")
GREETING = {"am": "Good morning", "pm": "Good afternoon"}
EPOCH = dt.date(2026, 7, 30)   # issue No 1, morning edition
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


def issue_number(day: dt.date, edition: str) -> int:
    """Editions since the first one. AM daily, PM weekdays only."""
    n = 0
    d = EPOCH
    while d < day:
        n += 2 if d.weekday() < 5 else 1
        d += dt.timedelta(days=1)
    return n + (2 if edition == "pm" else 1)


def opener(edition: str, sentence: str) -> str:
    """Address the reader, then hand straight over to the day's five clauses."""
    for stale in ("Good morning,", "Good afternoon,", "Good morning.", "Good afternoon."):
        if sentence.strip().startswith(stale):
            sentence = sentence.strip()[len(stale):].lstrip()
    return sentence


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


def render(issue: dict, edition: str, now: dt.datetime, assets: dict,
           market: dict, issue_no: int) -> str:
    st = issue["_stats"]
    have = assets.__contains__
    rows = [head(edition, f"{now:%A, %B %-d, %Y}".upper(), f"{now:%-I:%M %p} CT",
                 issue_no, cover=have("cover"), ticker=have("ticker"),
                 y10=market.get("y10"))]
    count = 0
    spark_used = False
    for s_ in issue["sections"]:
        if not s_["title"].upper().startswith("FRONT") and not any(
                b.get("items") for b in s_.get("blocks", [])):
            continue  # omitted section: never render a heading with nothing under it
        rows.append(sec(edition, s_["title"], marker=have("marker")))
        if s_["title"].upper().startswith("FRONT"):
            fm = issue.get("front_matter")
            rows.append(greet(f"{GREETING[edition]}, {NAME}.", card=have("greet")))
            if fm:
                rows.append(lead(opener(edition, fm)))
            continue
        for b in s_.get("blocks", []):
            if b.get("label"):
                rows.append(sub(b["label"]))
            items = b.get("items", [])
            for n, it in enumerate(items):
                if it.get("note"):
                    # Numbers lines (the rate path, the CRE snapshot) render as a
                    # tinted callout. They are the standing state-of-things the
                    # reader asked to see every issue, so they should not read as
                    # just another bullet. The first one carries the 10Y sparkline.
                    use = have("spark") and not spark_used
                    spark_used = spark_used or use
                    rows.append(note(edition, it["text"], spark=use))
                else:
                    rows.append(item(edition, it["text"], it.get("src"),
                                     it.get("url"), rule=n != len(items) - 1))
                count += 1
    dropped = st["invented"] + st["dead"]
    rows.append(foot(edition,
        f"COMPASS / {'MORNING' if edition == 'am' else 'AFTERNOON'} EDITION / "
        f"NO {issue_no} / {now:%-d %b %Y}".upper() + "<br>"
        f"{count} items. {st['checked']} links checked, {st['checked'] - dropped} shipped, "
        f"{dropped} dropped ({st['invented']} unsourced, {st['dead']} unresolved)."))
    issue["_stats"]["items"] = count
    return wrap(rows)


def send(html: str, edition: str, now: dt.datetime, assets: dict) -> None:
    msg = EmailMessage()
    # [SPREAD] is a routing token the Apps Script matches on, not a display name.
    msg["Subject"] = f"[SPREAD] COMPASS {'AM' if edition == 'am' else 'PM'} / {now:%b %-d}"
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["RECIPIENT"]
    msg.set_content("COMPASS is an HTML email. Enable HTML to read it.")
    msg.add_alternative(html, subtype="html")
    # The motion rides along as content-id parts rather than hotlinked URLs: the
    # art is rebuilt every issue, so there is nothing in the repo to link to, and
    # cid attachments render without a round trip to raw.githubusercontent.
    html_part = msg.get_payload()[-1]
    for key, data in assets.items():
        html_part.add_related(data, maintype="image", subtype="gif", cid=f"<{key}>")
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
    ap.add_argument("--market", default="market.json")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--send-at", metavar="HH:MM",
                    help="hold the send until this Chicago time, so delivery lands "
                         "on the minute regardless of when the runner started")
    args = ap.parse_args()

    now = dt.datetime.now(CT)
    issue = json.load(open(args.issue))
    allowed = {i["url"] for i in json.load(open(args.candidates))["items"]}

    try:
        market = json.load(open(args.market))
    except Exception as e:
        # The snapshot is a nice-to-have for the cover; the brief is not.
        print(f"no market data ({type(e).__name__}) — cover falls back to plain")
        market = {}

    print(f"verifying against {len(allowed)} sourced URLs")
    issue = verify(issue, allowed)

    no = issue_number(now.date(), args.edition)
    # Built before the send-hold so GIF encoding can never eat into the minute the
    # edition is due, and guarded whole: no image is worth losing an edition over.
    try:
        assets = art.build(args.edition, market, f"{now:%A, %B %-d, %Y}".upper(),
                           f"{now:%-I:%M %p} CT", no, NAME)
    except Exception as e:
        print(f"art stage failed entirely ({type(e).__name__}: {e}) — shipping plain")
        assets = {}

    html = render(issue, args.edition, now, assets, market, no)

    stem = f"issue_{now:%Y-%m-%d}_{args.edition}"
    out = f"{stem}.html"
    # The on-disk copy points at sidecar files so the issue can be opened and
    # checked in a browser; only the emailed copy uses cid.
    disk = html
    for key, data in assets.items():
        name = f"{stem}_{key}.gif"
        with open(name, "wb") as f:
            f.write(data)
        disk = disk.replace(f"cid:{key}", name)
    with open(out, "w") as f:
        f.write(disk)
    st = issue["_stats"]
    kb = len(html.encode()) / 1024
    print(f"wrote {out} — {st['checked']} checked, "
          f"{st['invented']} invented, {st['dead']} dead, {kb:.0f} kB html")
    if kb > 95:
        # Gmail clips the body past ~102 kB and hides the tail behind a "View
        # entire message" link, which would silently amputate the last sections.
        print(f"  WARNING: {kb:.0f} kB is close to Gmail's 102 kB clip threshold")

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
        send(html, args.edition, now, assets)
        print(f"sent at {dt.datetime.now(CT):%H:%M:%S} CT")
    else:
        print("not sent (COMPASS_SEND is not 'true')")


if __name__ == "__main__":
    main()
