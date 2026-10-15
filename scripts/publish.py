#!/usr/bin/env python3
"""Stage 3 of 3: verify, render, send. Deterministic, no model.

Two independent guards against bad links, in order of strength:

  1. Provenance. Every URL in issue.json must appear in candidates.json. The model
     only ever sees that candidate list, so a URL outside it was invented. This makes
     fabrication structurally impossible rather than merely detectable.
  2. Resolution. Whatever survives is fetched. 404/410 loses its link.

Both guards only see the `url` field, so the text itself is sanitized first: every
tag but b/strong/i/em is unwrapped to its text. An <a> written into an item's text
would otherwise ship as a live link that neither guard ever looked at.

An item that fails either check ships unlinked rather than being dropped, per the
spec: an unlinked true item is fine, a confidently wrong link is not.

Satire is labelled here too, for the same reason: it cannot be left to the model.
collect.py flags every satirical candidate; any item built from one ships with a
SATIRE: label, whatever the curator wrote and even from fallback.py.
"""

import argparse
import concurrent.futures as futures
import datetime as dt
import email.utils
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import art  # noqa: E402
import check_issue  # noqa: E402
import market as mkt  # noqa: E402
from compass import head, sec, sub, item, note, lead, foot, wrap, greet  # noqa: E402

CT = ZoneInfo("America/Chicago")

# The reader is addressed by name, and the greeting is set here rather than by the
# curator. A model that forgets it once would silently drop the most personal line
# in the brief; generated here it is guaranteed, and fallback.py inherits it free.
NAME = os.environ.get("COMPASS_NAME", "Hudson")
GREETING = {"am": "Good morning", "pm": "Good afternoon"}
EDITION = {"am": "MORNING EDITION", "pm": "AFTERNOON EDITION"}
EPOCH = dt.date(2026, 7, 30)   # issue No 1, morning edition
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# --- text --------------------------------------------------------------------

_KEEP = ("b", "strong", "i", "em")
# A "<" that does not open a complete tag is text: "rates <5%" must survive, and
# so must "Yields <hit 5%" with no closing ">", which html.parser would otherwise
# hold as an unfinished tag and drop along with the rest of the sentence.
_STRAY_LT = re.compile(r"<(?!/?[A-Za-z][^<>]*>|!--)")


class _Markup(HTMLParser):
    """Keep `keep` tags (bare, no attributes), unwrap every other tag to its text.
    Built on html.parser rather than a tag regex because a regex reads the text
    between a literal "<" and ">" as a tag; collect.py's TAGSTRIP turned
    "S&P <6,000 by spring, >5,500 floor" into "S&P 5,500 floor"."""

    def __init__(self, keep: tuple, escape: bool):
        super().__init__(convert_charrefs=True)
        self.keep, self.escape = keep, escape
        self.out, self.open, self.dropped = [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in self.keep:
            self.out.append(f"<{tag}>")
            self.open.append(tag)
        else:
            if tag == "br":
                self.out.append(" ")
            self.dropped.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.keep and tag in self.open:
            while self.open:
                t = self.open.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(html.escape(data, quote=False) if self.escape else data)

    def result(self, s: str) -> str:
        self.feed(_STRAY_LT.sub("&lt;", s))
        self.close()
        # An unclosed <b> would bold everything after it in the email.
        self.out.extend(f"</{t}>" for t in reversed(self.open))
        return "".join(self.out)


def sanitize(s: str) -> tuple[str, list[str]]:
    """Curator markup reduced to b/strong/i/em, entities kept (as one escape)."""
    p = _Markup(_KEEP, escape=True)
    return p.result(s or ""), p.dropped


def plain(s: str) -> str:
    """Tags stripped, entities unescaped, whitespace collapsed."""
    return " ".join(_Markup((), escape=False).result(s or "").split())


def sanitize_issue(issue: dict) -> None:
    """Every piece of curator text that reaches the HTML, before label_satire
    and verify see it."""
    def clean(s, where):
        out, dropped = sanitize(s)
        if dropped:
            print(f"  unwrapped <{'>, <'.join(sorted(set(dropped)))}> in {where}")
        return out

    issue["front_matter"] = clean(issue.get("front_matter") or "", "front_matter")
    for s_ in issue["sections"]:
        s_["title"] = clean(s_["title"], "a section title")
        for b in s_["blocks"]:
            if b.get("label"):
                b["label"] = clean(b["label"], f"a label in {plain(s_['title'])}")
            for i in b["items"]:
                i["text"] = clean(i["text"], f"{i.get('url') or 'an item'}")


def source_from_candidates(issue: dict, by_url: dict) -> None:
    """A linked item's source tag is the candidate's, whatever the curator wrote:
    the chip is the reader's attribution for that link and is rendered verbatim."""
    changed = 0
    for i in all_items(issue):
        c = by_url.get(i.get("url"))
        if c and c.get("src") and i.get("src") != c["src"]:
            changed += 1
            i["src"] = c["src"]
    if changed:
        print(f"  {changed} source tag(s) set from their candidates")


# --- links -------------------------------------------------------------------

def _homepage(asked: str, final: str) -> bool:
    """A redirect from an article to a site root. Only roots: WordPress /slug/
    permalinks reached through a redirect look like one segment, and those are
    live articles."""
    a, f = urlsplit(asked), urlsplit(final)
    return bool(a.path.strip("/")) and not f.path.strip("/") and not f.query


def resolves(url: str) -> tuple[str, bool, str]:
    """403 still renders for a human; only a missing page disqualifies a link.
    The third value says why a link was kept, for the summary line."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                # Spec 6.2 also rules out a homepage redirect. Logged, not yet
                # dropped: until the log shows how often a live story lands on a
                # root, dropping would be guessing.
                if r.status < 400 and _homepage(url, r.geturl()):
                    print(f"  (redirected to a homepage, keeping for now): {url} -> {r.geturl()}")
                    return url, True, "homepage"
                return url, r.status < 400, "ok" if r.status < 400 else "dead"
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return url, False, "dead"
            return url, True, f"http {e.code}"
        except Exception:
            # Timeout, DNS blip, reset. Provenance already vouches for this URL —
            # it came out of a feed parsed minutes ago — so a transient failure is
            # not evidence the page is gone. Retry once, then keep the link rather
            # than silently degrading a good item.
            if attempt == 2:
                print(f"  (unreachable, keeping link on provenance): {url}")
                return url, True, "unreachable"
    return url, True, "unreachable"


def issue_number(day: dt.date, edition: str) -> int:
    """Editions since the first one. AM daily, PM weekdays only."""
    n = 0
    d = EPOCH
    while d < day:
        n += 2 if d.weekday() < 5 else 1
        d += dt.timedelta(days=1)
    return n + (2 if edition == "pm" else 1)


# Any greeting the curator wrote anyway, with or without the name, and whatever
# punctuation it ran into the first clause with. The name or punctuation is
# required, so a headline like "Good morning for borrowers..." is left alone.
_PUNCT = r"[,.:;!—–-]"
_GREETING = re.compile(
    rf"^\s*good\s+(?:morning|afternoon|evening)"
    rf"(?:\s*,?\s*{re.escape(NAME)}\b\s*{_PUNCT}*|\s*{_PUNCT}+)\s*", re.I)
_LEADING_MARKUP = re.compile(r"^(?:\s|<[^>]+>|&[a-z]+;|[\"'“‘(])*", re.I)


def opener(edition: str, sentence: str) -> str:
    """Address the reader, then hand straight over to the day's five clauses.

    The spec models this sentence on Exec Sum's opener, which reads "Good
    morning, stocks...". On 2026-10-14 the curator wrote exactly that shape,
    "Good afternoon, hot inflation at 3.7%...". Stripping the greeting left the
    sentence starting lowercase, which read as an edition cut off mid-sentence.
    So the greeting is stripped in any form and the first letter is restored.
    """
    sentence = _GREETING.sub("", sentence.strip(), count=1)
    m = _LEADING_MARKUP.match(sentence)
    k = m.end() if m else 0
    return sentence[:k] + sentence[k:k + 1].upper() + sentence[k + 1:]


def all_items(issue: dict):
    # `or []`: a curator that writes "blocks": null or "items": null has
    # written an empty block, not a reason to crash the send.
    for s in issue.get("sections") or []:
        for b in s.get("blocks") or []:
            for i in b.get("items") or []:
                yield i


def is_front(section: dict) -> bool:
    return plain(section["title"]).upper().startswith("FRONT")


SATIRE_LABEL = "<b>SATIRE:</b> "
_LEADING_TAGS = re.compile(r"^(?:\s|<[^>]+>)*")


def label_satire(issue: dict, satire: set[str]) -> None:
    """Prefix every item sourced from a satire-flagged candidate. On 2026-09-24 a
    Cavalier Daily humor piece ran as "UVA's president announced the historic
    Academical Village will be demolished" -- stated as fact in the reader's own
    brief. Runs before verify(), which may null a dead link and with it the only
    way to recognise the item."""
    for i in all_items(issue):
        if i.get("url") not in satire:
            continue
        plain_ = _LEADING_TAGS.sub("", i.get("text", ""))
        if not plain_.lower().startswith("satire"):
            i["text"] = SATIRE_LABEL + i.get("text", "")
        print(f"  SATIRE (labelled): {i['url']}")


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
    kinds: dict[str, int] = {}
    if urls:
        with futures.ThreadPoolExecutor(max_workers=12) as ex:
            for url, ok, kind in ex.map(resolves, urls):
                kinds[kind] = kinds.get(kind, 0) + 1
                if not ok:
                    dead.add(url)
                    print(f"  DEAD (failed to resolve): {url}")
    for i in all_items(issue):
        if i.get("url") in dead:
            i["url"] = None
    # "0 dead" reads as "every link verified" unless it says how many were kept
    # only because a 403, a 5xx or a timeout is not proof the page is gone.
    kept = {k: v for k, v in sorted(kinds.items()) if k not in ("ok", "dead")}
    print(f"  links: {kinds.get('ok', 0)} resolved, {len(dead)} dead"
          + "".join(f", {v} kept on {k}" for k, v in kept.items()))

    invented_urls = {i for i in invented_seen}
    issue["_stats"] = {"checked": len(urls) + len(invented_urls),
                       "invented": len(invented_urls), "dead": len(dead)}
    return issue


# --- render ------------------------------------------------------------------

def render(issue: dict, edition: str, when: dt.datetime, assets: dict,
           market: dict, issue_no: int) -> str:
    """Deterministic in its inputs, so it is simply run again after the pre-send
    refresh. `when` is the moment the edition is due, not the moment it was built."""
    st = issue["_stats"]
    have = assets.__contains__
    fm = opener(edition, issue.get("front_matter") or "")
    # The masthead, greeting and opener are rendered here, always, rather than
    # when the loop meets a FRONT MATTER section: an issue that left that
    # boilerplate section out passed the check and shipped with no greeting and
    # no opener, the two lines the spec guarantees every issue.
    rows = [head(edition, f"{when:%A, %B %-d, %Y}".upper(), f"{when:%-I:%M %p} CT",
                 issue_no, cover=have("cover"), ticker=have("ticker"),
                 y10=market.get("y10"), spy=market.get("spy")),
            sec(edition, "FRONT MATTER", marker=have("marker")),
            greet(f"{GREETING[edition]}, {NAME}.", card=have("greet"))]
    if fm:
        rows.append(lead(fm))
    count = 0
    spark_used = False
    for s_ in issue["sections"]:
        if is_front(s_) or not any(b.get("items") for b in s_.get("blocks") or []):
            continue  # omitted section: never render a heading with nothing under it
        rows.append(sec(edition, s_["title"], marker=have("marker")))
        for b in s_.get("blocks") or []:
            items = b.get("items") or []
            if b.get("label") and items:
                rows.append(sub(b["label"]))
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
        f"COMPASS / {EDITION[edition]} / NO {issue_no} / {when:%-d %b %Y}".upper() + "<br>"
        f"{count} items. {st['checked']} links checked, {st['checked'] - dropped} shipped, "
        f"{dropped} dropped ({st['invented']} unsourced, {st['dead']} unresolved)."))
    issue["_stats"]["items"] = count
    preview = html.escape(plain(fm) or f"{GREETING[edition]}, {NAME}.", quote=False)
    return wrap(rows, preview=preview)


def plaintext(issue: dict, edition: str, when: dt.datetime, issue_no: int) -> str:
    """The text/plain part: the whole brief, readable on a watch or in a client
    that will not render HTML. It used to be "COMPASS is an HTML email."."""
    out = [f"COMPASS / {EDITION[edition]} / NO {issue_no}",
           f"{when:%A, %B %-d, %Y} / {when:%-I:%M %p} CT".upper(), "",
           f"{GREETING[edition]}, {NAME}."]
    fm = plain(opener(edition, issue.get("front_matter") or ""))
    if fm:
        out += ["", fm]
    for s_ in issue["sections"]:
        if is_front(s_) or not any(b.get("items") for b in s_.get("blocks") or []):
            continue
        out += ["", "", plain(s_["title"]).upper()]
        for b in s_.get("blocks") or []:
            items = [i for i in b.get("items") or [] if plain(i["text"])]
            if b.get("label") and items:
                out += ["", plain(b["label"]).upper()]
            for it in items:
                line = ("// " if it.get("note") else "- ") + plain(it["text"])
                if it.get("src"):
                    line += f" [{plain(it['src'])}]"
                out += ["", line]
                if it.get("url"):
                    out.append(f"  {it['url']}")
    return "\n".join(out) + "\n"


def write_disk(stem: str, page: str, assets: dict) -> str:
    """The on-disk copy points at sidecar files so the issue can be opened and
    checked in a browser; only the emailed copy uses cid. Rewritten after the
    pre-send refresh, so the shipped artifact holds the cover that was emailed
    rather than the one drawn an hour before it."""
    disk = page
    for key, data in used(page, assets).items():
        name = f"{stem}_{key}.gif"
        with open(name, "wb") as f:
            f.write(data)
        disk = disk.replace(f"cid:{key}", name)
    out = f"{stem}.html"
    with open(out, "w") as f:
        f.write(disk)
    return out


def used(page: str, assets: dict) -> dict:
    """Only the assets the HTML references. The spark only appears beside a
    numbers line, and the Oct 9 fallback (which had none) carried it anyway as
    a nameless image attachment at the foot of the message."""
    return {k: v for k, v in assets.items() if f'src="cid:{k}"' in page}


# --- send --------------------------------------------------------------------

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
# Without a timeout a hung TLS session holds until the 120-minute job timeout.
SMTP_TIMEOUT = 60
# A transient Gmail blip at the send minute used to fail the step outright and
# leave the edition to the next sweep slot or the watchdog: hours late, and
# curated a second time. Two waits = three attempts, well inside the job budget.
RETRY_WAITS = (30, 90)

LOGIN_HELP = (
    "Gmail rejected the login ({code}).\n"
    "  GMAIL_ADDRESS is {addr!r}\n"
    "  Check, in order:\n"
    "    1. GMAIL_APP_PASSWORD is a 16-character app password, not the\n"
    "       account password.\n"
    "    2. It was stored with the spaces removed (Google displays it\n"
    "       as 4 groups of 4).\n"
    "    3. 2-step verification is on for that account — app passwords\n"
    "       do not exist without it.\n"
    "    4. The app password was generated for this exact address.\n")


def message(page: str, text: str, edition: str, when: dt.datetime,
            assets: dict) -> EmailMessage:
    msg = EmailMessage()
    sender = os.environ["GMAIL_ADDRESS"]
    # [SPREAD] is a routing token the Apps Script matches on, not a display name.
    msg["Subject"] = f"[SPREAD] COMPASS {'AM' if edition == 'am' else 'PM'} / {when:%b %-d}"
    msg["From"] = sender
    msg["To"] = os.environ["RECIPIENT"]
    msg["Date"] = email.utils.format_datetime(dt.datetime.now(CT))
    # Literal and deterministic, so every send of this edition carries the same
    # id and a duplicate (a retry after an ambiguous DATA failure, or a second
    # run that never saw the marker) can collapse into one message. make_msgid()
    # would not do: it mixes in time, pid and random bits.
    domain = sender.rpartition("@")[2] or "localhost"
    msg["Message-ID"] = f"<compass-{edition}-{when:%Y-%m-%d}@{domain}>"
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    # The motion rides along as content-id parts rather than hotlinked URLs: the
    # art is rebuilt every issue, so there is nothing in the repo to link to, and
    # cid attachments render without a round trip to raw.githubusercontent.
    html_part = msg.get_payload()[-1]
    for key, data in used(page, assets).items():
        html_part.add_related(data, maintype="image", subtype="gif", cid=f"<{key}>")
    return msg


def _retryable(e: Exception) -> bool:
    """Retry what might work a minute later; never what cannot, or what could
    already have been delivered to a recipient that refused the rest."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(e, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(e, smtplib.SMTPResponseException):
        return 400 <= e.smtp_code < 500
    if isinstance(e, smtplib.SMTPException):
        return False          # recipients refused, extension not supported
    return isinstance(e, OSError)   # timeout, reset, DNS, TLS


def _delivered(raw: bytes, stem: str) -> None:
    """Record the send the moment Gmail has accepted the message.

    The shipped marker used to be a later workflow step gated on the whole job
    succeeding, so any failure after the send (smtplib raising on a non-221
    QUIT, the artifact upload failing, the runner lost) left no marker and every
    later slot sent the edition again. sent.ok and the step output are what the
    marker now keys on. Each write is guarded: the mail is already out, and
    nothing here may turn a delivered edition into a failed step.
    """
    try:
        with open("sent.ok", "w") as f:
            f.write(f"{dt.datetime.now(CT).isoformat()}\n")
    except OSError as e:
        print(f"::warning::could not write sent.ok ({e})")
    if os.environ.get("GITHUB_OUTPUT"):
        try:
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("sent=true\n")
        except OSError as e:
            print(f"::warning::could not write sent=true to GITHUB_OUTPUT ({e})")
    try:
        with open(f"{stem}.eml", "wb") as f:
            f.write(raw)
    except OSError as e:
        print(f"::warning::could not write {stem}.eml ({e})")


def send(msg: EmailMessage, stem: str) -> None:
    # Serialised once and sent as bytes, so the .eml kept for the archive is
    # byte-for-byte what Gmail accepted, and every retry sends the same message.
    raw = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
    sender = os.environ["GMAIL_ADDRESS"]
    rcpts = [a for _, a in email.utils.getaddresses([msg["To"]]) if a]
    attempt = 0
    while True:
        attempt += 1
        try:
            s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT)
            try:
                s.login(sender, os.environ["GMAIL_APP_PASSWORD"])
                refused = s.sendmail(sender, rcpts, raw)
            except BaseException:
                s.close()
                raise
        except smtplib.SMTPAuthenticationError as e:
            # A raw traceback here is unreadable in a cron log, and the cause is
            # almost always one of a short list. Say so, and do not retry it.
            print(f"::error::Gmail rejected the login ({e.smtp_code}) — not sent")
            raise SystemExit(LOGIN_HELP.format(code=e.smtp_code, addr=sender)) from None
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            if not _retryable(e) or attempt > len(RETRY_WAITS):
                print(f"::error::send failed on attempt {attempt}, giving up: {why}")
                try:
                    # For a hand re-send from the failure artifact, without
                    # collecting and curating the edition a second time.
                    with open(f"{stem}.unsent.eml", "wb") as f:
                        f.write(raw)
                except OSError:
                    pass
                raise SystemExit(f"not sent: {why}") from None
            wait = RETRY_WAITS[attempt - 1]
            print(f"  send attempt {attempt} failed ({why}) — retrying in {wait}s")
            time.sleep(wait)
            continue
        _delivered(raw, stem)
        if refused:
            print(f"::warning::Gmail refused some recipients: {refused}")
        # Its own try, not the `with` block it used to be: SMTP.__exit__ raises
        # when QUIT does not answer 221, which failed a step whose message had
        # already been accepted.
        try:
            s.quit()
        except Exception as e:
            print(f"  (QUIT after delivery failed: {type(e).__name__} — already sent)")
            s.close()
        return


def refresh(edition: str, market: dict, assets: dict, when: dt.datetime,
            no: int) -> tuple[dict, dict]:
    """Re-read SPY and redraw the cover and ticker. Returns the new assets and the
    market they were drawn from, so the HTML (the cover's alt text) is rendered
    again from the same numbers. Returns the old pair on any failure — a stale
    chart is fine, a late or missing edition is not."""
    try:
        fresh = dict(market)
        fresh.update(mkt.spy_intraday())
        mkt.settle_spy(fresh)
        new = art.build(edition, fresh, f"{when:%A, %B %-d, %Y}".upper(),
                        f"{when:%-I:%M %p} CT", no, NAME, only=("cover", "ticker"))
        out = dict(assets)
        for key in ("cover", "ticker"):
            if key in new and key in assets:
                out[key] = new[key]
        print(f"  refreshed SPY to {fresh.get('spy')} "
              f"({len(fresh.get('spy_intraday') or [])} points)")
        # The alt text describes the cover; it moves only if the cover did.
        return out, fresh if "cover" in new else market
    except Exception as e:
        print(f"  SPY refresh failed ({type(e).__name__}: {e}) — sending the art as built")
        return assets, market


def send_target(at: str | None, now: dt.datetime) -> dt.datetime | None:
    if not at:
        return None
    try:
        hh, mm = (int(x) for x in at.split(":"))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        print(f"::warning::--send-at {at!r} is not HH:MM — sending without a hold")
        return None


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
    ed = args.edition

    now = dt.datetime.now(CT)
    target = send_target(args.send_at, now)
    # The dateline, footer and subject carry the time the edition is due. They
    # used to carry the build time: on 2026-10-14 the 2:00 PM edition was built
    # at 13:00:54, held an hour, and arrived reading "1:00 PM CT".
    when = max(now, target) if target else now

    issue, defects = check_issue.normalize(json.load(open(args.issue)))
    for d in defects:
        # Only reachable when the check was bypassed (a manual run); the check
        # rejects these. Dropped or nulled rather than crashing the send.
        print(f"::warning::issue.json: {d} — dropped or nulled for the render")
    if issue is None:
        print("::error::issue.json has no renderable sections")
        raise SystemExit(1)
    cands = json.load(open(args.candidates))["items"]
    by_url = {c["url"]: c for c in cands if c.get("url")}
    allowed = set(by_url)
    satire = {u for u, c in by_url.items() if c.get("satire")}

    try:
        market = mkt.load_market(args.market)
    except Exception as e:
        # The snapshot is a nice-to-have for the cover; the brief is not.
        print(f"no market data ({type(e).__name__}) — cover falls back to plain")
        market = {}

    sanitize_issue(issue)
    source_from_candidates(issue, by_url)
    label_satire(issue, satire)
    print(f"verifying against {len(allowed)} sourced URLs")
    issue = verify(issue, allowed)

    no = issue_number(when.date(), ed)
    # Built before the send-hold so GIF encoding can never eat into the minute the
    # edition is due, and guarded whole: no image is worth losing an edition over.
    try:
        assets = art.build(ed, market, f"{when:%A, %B %-d, %Y}".upper(),
                           f"{when:%-I:%M %p} CT", no, NAME)
    except Exception as e:
        print(f"art stage failed entirely ({type(e).__name__}: {e}) — shipping plain")
        assets = {}

    page = render(issue, ed, when, assets, market, no)
    text = plaintext(issue, ed, when, no)
    stem = f"issue_{when:%Y-%m-%d}_{ed}"
    out = write_disk(stem, page, assets)
    st = issue["_stats"]
    kb = len(page.encode()) / 1024
    print(f"wrote {out} — {st['checked']} checked, "
          f"{st['invented']} invented, {st['dead']} dead, {kb:.0f} kB html")
    if kb > 95:
        # Gmail clips the body past ~102 kB and hides the tail behind a "View
        # entire message" link, which would silently amputate the last sections.
        print(f"  WARNING: {kb:.0f} kB is close to Gmail's 102 kB clip threshold")

    if args.send and st["items"] == 0:
        raise SystemExit("issue has zero items — refusing to send an empty brief")
    if not args.send:
        print("not sent (COMPASS_SEND is not 'true')")
        return
    if target:
        wait = (target - dt.datetime.now(CT)).total_seconds()
        if wait > 0:
            # The runner may have started anywhere in a two-hour window; idling
            # here is what converts an unreliable start into an exact delivery.
            print(f"built at {dt.datetime.now(CT):%H:%M:%S} — holding "
                  f"{wait/60:.1f} min to send at {target:%H:%M:00} CT")
            # Sleep to just short of the minute, then redraw the cover and
            # ticker from a fresh SPY tape. The issue was built 20-40 minutes
            # ago; this is what makes the chart run to the moment it lands —
            # the open for the morning, most of the session for the afternoon.
            time.sleep(wait - min(wait, 150))
            assets, market = refresh(ed, market, assets, when, no)
            page = render(issue, ed, when, assets, market, no)
            write_disk(stem, page, assets)
            wait = (target - dt.datetime.now(CT)).total_seconds()
            if wait > 0:
                time.sleep(wait)
        else:
            print(f"target {args.send_at} CT already passed — sending now "
                  f"({-wait/60:.1f} min late)")
    send(message(page, text, ed, when, assets), stem)
    print(f"sent at {dt.datetime.now(CT):%H:%M:%S} CT")


if __name__ == "__main__":
    main()
