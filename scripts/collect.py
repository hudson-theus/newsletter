#!/usr/bin/env python3
"""Stage 1 of 3: collect candidate items from RSS. Deterministic, no model.

Why RSS rather than fetching index pages:
  - Index pages lie about dates. Commercial Observer stamps every row with the
    current date; on 2026-07-30 the real publish dates spanned 22-30 Jul. RSS
    <pubDate> is authoritative and matched scraped ground truth exactly.
  - Several sources that hard-block a normal page fetch (CNBC, Axios, Connect
    CRE) serve RSS without complaint.
  - One request per source instead of one per article, and a candidate list the
    model can actually hold in context.

Every feed here is FREE to read. No subscription source is included: the reader
has no subscriptions, so a paywalled link is a dead end even when the story is
real. Where a paywalled outlet owns a story, a free wire almost always carries
the same facts, and that free version is what belongs in the issue.

Each feed carries a `section` hint. That hint is what makes the standing sections
fillable: before it existed there were no international feeds at all, so THE WORLD
could only ever be US politics, and there were no local, sports, or IPO feeds, so
those sections could not exist. The hint is a routing aid for the curator, not a
binding assignment — a Dallas story big enough for the front page still belongs
there.

Nothing ships twice across days. The slow sections look back two to six days
(see SLOW) so they have something to choose from, and nothing used to remember
what had already gone out — over 2026-09-16..22, 50 links shipped on more than
one day, some in four editions running. The workflow now drops the previous
days' shipped issues into prior/, and anything whose link or headline shipped on
an earlier day is removed here, before the curator ever sees it. A morning item
may still reappear that afternoon: same-day repeats are allowed on purpose.

The afternoon edition is "what changed since this morning". Its pool still holds
the morning's items, because a follow-up needs the original story within reach,
but the PM candidates also carry `this_morning`: everything the morning edition
actually shipped, read from today's AM artifact in prior/, so the curator can
lead with what moved rather than re-run the morning (see load_this_morning).

Nothing satirical ships as fact. On 2026-09-24 the afternoon edition ran "UVA's
president announced the historic Academical Village will be demolished" as news.
It was a Cavalier Daily humor piece whose first line says it is satire, but the
curator only ever sees a headline -- here one rebuilt from the URL slug, since
that sitemap has no titles -- so it had no way to know. Satire is now detected
here, deterministically (flag_satire), and every such candidate carries
`"satire": true`. publish.py then labels it in the issue whatever the curator
wrote.

Writes candidates.json for the curation stage (layout: write_candidates).
"""

import argparse
import concurrent.futures as futures
import datetime as dt
import glob
import gzip
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

# Extension namespaces that feeds routinely *use* without declaring. D Magazine
# emits <media:content> with no xmlns:media, which is malformed XML and made the
# whole feed unreadable; injecting the declaration recovers it. Only well-known
# prefixes are ever patched, so this cannot invent structure that was not there.
# news: and image: are the Google sitemap extensions, now that sitemaps go through
# the same repair as feeds.
NS_FIX = {
    "media":   "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
    "wfw":     "http://wellformedweb.org/CommentAPI/",
    "slash":   "http://purl.org/rss/1.0/modules/slash/",
    "sy":      "http://purl.org/rss/1.0/modules/syndication/",
    "georss":  "http://www.georss.org/georss/",
    "news":    "http://www.google.com/schemas/sitemap-news/0.9",
    "image":   "http://www.google.com/schemas/sitemap-image/1.1",
}


def _decompress(raw: bytes, encoding: str) -> bytes:
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
    if encoding == "deflate":
        for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except Exception:
                pass
    return raw


def _repair_ns(raw: bytes) -> bytes:
    """Declare any well-known prefix the feed uses but never declared."""
    txt = raw.decode("utf-8", "ignore")
    used = set(re.findall(r"<(\w+):", txt))
    declared = set(re.findall(r"xmlns:(\w+)=", txt))
    missing = [p for p in sorted(used - declared) if p in NS_FIX]
    if not missing:
        return raw
    m = re.search(r"<(?:rss|feed|urlset|rdf:RDF)\b[^>]*", txt)
    if not m:
        return raw
    ins = " ".join(f'xmlns:{p}="{NS_FIX[p]}"' for p in missing)
    return (txt[:m.end()] + " " + ins + txt[m.end():]).encode("utf-8")


def _parse(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as first:
        fixed = _repair_ns(raw)
        if fixed is raw:
            raise
        try:
            return ET.fromstring(fixed)
        except ET.ParseError:
            raise first from None   # the original position is the useful one


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


# ---- FETCHING ---------------------------------------------------------------
# At most one request in flight per host. The old pool fired all four CBS feeds,
# three Yahoo feeds and three TechCrunch feeds at once, and TownNews/BLOX sites
# answer 429 to requests in quick succession from one IP (probed from a runner,
# 2026-10-14). dailyprogress.com and richmond.com are separate hosts on that one
# platform, so they share a slot.
HOST_GROUP = {"dailyprogress.com": "townnews", "richmond.com": "townnews"}

# Retries are only for errors a second try can change: a timeout, a 429 or a 5xx.
# 403, 404 and a bot wall's HTML answer the same way every time, and the old loop
# retried them back to back regardless -- on 2026-10-14 the whole step took 6
# seconds, so its "three attempts" never waited for anything. Waits honour
# Retry-After up to RETRY_CAP seconds, and no retry starts once RETRY_WINDOW
# seconds of collection have passed, so a host that is down cannot hold the edition
# for minutes: collection is budgeted at ~2.5 minutes of the 45-minute sweep lead.
TIMEOUTS = (25, 35, 35)     # seconds, per attempt; its length is the attempt limit
RETRY_CAP = 20
RETRY_WINDOW = 60

_STARTED = time.monotonic()
_slots: dict[str, threading.Lock] = {}
_slots_guard = threading.Lock()


def _slot(url: str) -> threading.Lock:
    host = _host(url)
    key = HOST_GROUP.get(host, host)
    with _slots_guard:
        return _slots.setdefault(key, threading.Lock())


def _retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return (parsedate_to_datetime(value)
                - dt.datetime.now(dt.timezone.utc)).total_seconds()
    except Exception:
        return None


def _retry_wait(e: Exception, attempt: int) -> float | None:
    """Seconds to wait before trying again, or None when a retry cannot help."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code != 429 and e.code < 500:
            return None
        hdrs = getattr(e, "headers", None)
        hinted = _retry_after(hdrs.get("Retry-After") if hdrs is not None else None)
    elif isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError)
                                         and isinstance(e.reason, TimeoutError)):
        hinted = None
    else:
        return None
    return min(max(hinted or 2.0 * attempt, 1.0), RETRY_CAP)


def _why(e: Exception) -> str:
    """The failure as the log should say it: the status code, not just a class."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"URLError: {e.reason}"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _get(url: str, headers: dict) -> bytes:
    """GET url, undo gzip/deflate, and return the body. Raises the last error."""
    req = urllib.request.Request(url, headers=headers)
    with _slot(url):
        for attempt, timeout in enumerate(TIMEOUTS, 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return _decompress(r.read(),
                                       (r.headers.get("Content-Encoding") or "").lower())
            except Exception as e:
                wait = _retry_wait(e, attempt)
                if (wait is None or attempt == len(TIMEOUTS)
                        or time.monotonic() - _STARTED + wait > RETRY_WINDOW):
                    raise
                _log(f"  retrying in {wait:.0f}s ({_why(e)}): {url}")
                time.sleep(wait)    # still holding the slot: a 429 means back off
    raise RuntimeError("unreachable")


_out = threading.Lock()


def _log(line: str) -> None:
    """One whole line per call. Feeds are fetched on 32 threads and print() writes
    the text and its newline separately, so lines from different feeds ran
    together -- and a ::warning:: that does not start its line is not an
    annotation."""
    with _out:
        print(line)


def _warn(msg: str) -> None:
    """A GitHub annotation, so a dead feed shows on the run page. Feeds that failed
    every run for weeks (The Real Deal, VentureBeat, GlobeNewswire, Daily Progress,
    and DW at 0 items) were visible only as one log line among seventy."""
    msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    _log(f"::warning::{msg}")


def _fail(section: str, tag: str, url: str, why: str) -> None:
    _log(f"  {section}/{tag}: FAILED ({why}) {url}")
    _warn(f"feed {section}/{tag} failed: {why} -- {url}")


def _download(section: str, tag: str, url: str, headers: dict) -> ET.Element | None:
    """Fetch and parse one feed or sitemap; log and return None on any failure."""
    raw = b""
    try:
        raw = _get(url, headers)
        return _parse(raw)
    except ET.ParseError as e:
        # The opening bytes tell a bot wall ("<!DOCTYPE html") from a broken feed.
        _fail(section, tag, url, f"ParseError: {e}; body starts {raw[:80]!r}")
    except Exception as e:
        _fail(section, tag, url, _why(e))
    return None


def _report(section: str, tag: str, url: str, n: int, skipped: int) -> None:
    # Undated, unreadable, or outside [now-30d, now+1d] (see _when). A section
    # sitemap lists years of articles, so a large count there is normal; a feed
    # whose every item lands here has gone stale, like VentureBeat's mirror.
    note = f" ({skipped} skipped: no date within 30 days)" if skipped else ""
    _log(f"  {section}/{tag}: {n} items{note}")
    if not n:
        # A feed that parses to nothing is a parser or feed problem, not a quiet
        # day: DW read "0 items" on every run because of a namespace bug.
        _warn(f"feed {section}/{tag} parsed to 0 items{note} -- {url}")

# (section, source tag, feed url). Tags become the [XX] label in the rendered issue.
# CNBC, Axios and Connect CRE all hard-403 a normal page fetch but serve RSS
# without complaint. Reuters retired its public feeds and has no working RSS at
# all, so it stays out of reach.
#
# Do not add: feeds.a.dj.com (WSJ Markets) is frozen, still serving January items.
# ESPN's RSS returns an empty body, Renaissance Capital / Nasdaq-IPO / Axios-local
# / Dallas Morning News / Dallas Business Journal / KERA all 404, and StrictlyVC
# and KUT are abandoned (newest items from 2020 and 2021). Three more failed on
# every sampled run and were probed from a runner on 2026-10-14: The Real Deal
# answers 202 with an empty body (a bot wall), VentureBeat answers 429 (a bot
# wall; its feedburner mirror has been stale since 2026-09-03), and GlobeNewswire
# answers 403 Access Denied on every URL.
FEEDS = [
    # ---- MACRO / US ECONOMY -------------------------------------------------
    ("economy",   "CNBC",    "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("economy",   "CNBC",    "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                           "?partnerId=wrss01&id=10000664"),
    ("economy",   "MarketWatch",      "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("us",        "NPR",     "https://feeds.npr.org/1003/rss.xml"),
    ("us",        "PBS",     "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("us",        "Axios",   "https://www.axios.com/feeds/feed.rss"),

    # ---- WORLD --------------------------------------------------------------
    # The entire reason THE WORLD read as wall-to-wall US politics: there were no
    # international feeds in this list at all.
    ("world",     "BBC",     "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("world",     "Guardian","https://www.theguardian.com/world/rss"),
    ("world",     "Al Jazeera",      "https://www.aljazeera.com/xml/rss/all.xml"),
    ("world",     "France 24",     "https://www.france24.com/en/rss"),
    # DW's RSS 2.0 feed. The /rdf/ (RSS 1.0) URL used before serves the same 140
    # items, but RSS 1.0 namespaces every child and fetch() read it as 0 items on
    # every run until _child() matched by local name. Both parse now; plain RSS is
    # the one least likely to surprise a parser again.
    ("world",     "DW",      "https://rss.dw.com/xml/rss-en-all"),
    ("world",     "NPR",     "https://feeds.npr.org/1004/rss.xml"),
    ("world",     "CNBC",    "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                           "?partnerId=wrss01&id=100727362"),

    # ---- DALLAS / TEXAS -----------------------------------------------------
    ("dallas",    "WFAA",    "https://www.wfaa.com/feeds/syndication/rss/news/local"),
    ("dallas",    "Dallas Observer",      "https://www.dallasobserver.com/dallas/Rss.xml"),
    ("dallas",    "Texas Standard",    "https://www.texasstandard.org/feed/"),
    ("dallas",    "Texas Tribune",      "https://www.texastribune.org/feeds/main/"),
    ("dallas",    "Dallas Innovates",      "https://dallasinnovates.com/feed/"),
    ("dallas",    "Texas Monthly",      "https://www.texasmonthly.com/feed/"),
    ("dallas",    "Free Press",  "https://dallasfreepress.com/feed/"),

    # ---- DALLAS FOOD & GOING OUT --------------------------------------------
    # Restaurants worth going to, shows worth leaving the house for, and the
    # outdoors. CultureMap 404s on every path, Central Track has been abandoned
    # since 2022 and Do214 returns 406. Dallas Observer ignores its own
    # ?section= parameter -- restaurants, music and calendar all return the
    # identical feed -- so its single feed above already carries food and music.
    # D Magazine emits <media:content> without declaring xmlns:media, which is
    # malformed XML; _repair_ns is what makes it readable at all.
    ("food",      "Eater",       "https://dallas.eater.com/rss/index.xml"),
    ("food",      "D Magazine",  "https://www.dmagazine.com/feed/"),
    ("goingout",  "EDM Tunes",   "https://www.edmtunes.com/feed/"),
    ("goingout",  "Texas Highways", "https://texashighways.com/feed/"),

    # ---- SPORTS -------------------------------------------------------------
    # Cowboys, NFL, Mavericks, UVA football and basketball. Nothing else.
    # Household outlets only. ESPN, Fox Sports, USA Today and Sports Illustrated
    # have all retired public RSS (ESPN returns an empty body on every endpoint),
    # so CBS, Yahoo and NBC are what actually remains.
    ("sports",    "NBC Sports", "https://profootballtalk.nbcsports.com/feed/"),
    ("sports",    "Yahoo",   "https://sports.yahoo.com/nfl/rss.xml"),
    ("sports",    "Yahoo",   "https://sports.yahoo.com/nba/rss.xml"),
    ("sports",    "Yahoo",   "https://sports.yahoo.com/college-football/rss.xml"),
    ("sports",    "CBS",     "https://www.cbssports.com/rss/headlines/nfl/"),
    ("sports",    "CBS",     "https://www.cbssports.com/rss/headlines/nba/"),
    ("sports",    "CBS",     "https://www.cbssports.com/rss/headlines/college-football/"),
    ("sports",    "CBS",     "https://www.cbssports.com/rss/headlines/college-basketball/"),
    ("sports",    "WFAA",    "https://www.wfaa.com/feeds/syndication/rss/sports"),
    # Both TownNews/BLOX sites, which rate-limit one IP across the platform: on
    # 2026-10-14 Daily Progress answered 429 when hit right after richmond.com.
    # They share a fetch slot (HOST_GROUP) and a 429 waits out Retry-After.
    ("sports",    "Daily Progress",
                           "https://dailyprogress.com/search/?f=rss&t=article&c=sports*&l=50"),
    ("sports",    "Richmond Times-Dispatch",
                           "https://richmond.com/search/?f=rss&t=article&c=sports*&l=50"),
    # The reader's own teams. Every feed above is league-wide, so the capped
    # sports list is mostly other teams' news, and MAVERICKS has gone 12-16
    # editions dark at a stretch.
    ("sports",    "Mavs Moneyball",    "https://www.mavsmoneyball.com/rss/index.xml"),
    ("sports",    "Blogging the Boys", "https://www.bloggingtheboys.com/rss/index.xml"),
    # VIRGINIA otherwise rests on the Cavalier Daily (SITEMAPS below), a student
    # paper that is mostly campus news.
    ("uva",       "Streaking the Lawn", "https://www.streakingthelawn.com/rss/index.xml"),

    # ---- CRE ----------------------------------------------------------------
    ("cre",       "Bisnow",  "https://www.bisnow.com/national/rss"),
    ("cre",       "Bisnow",  "https://www.bisnow.com/dallas-ft-worth/rss"),
    ("cre",       "Commercial Observer",      "https://commercialobserver.com/feed/"),
    ("cre",       "Connect", "https://www.connectcre.com/feed/"),
    ("cre",       "Trepp",   "https://www.trepp.com/trepptalk/rss.xml"),
    ("retail",    "Retail Dive",      "https://www.retaildive.com/feeds/news/"),

    # ---- VC / STARTUPS / IPO ------------------------------------------------
    # IPOS & LISTINGS used to come up empty every issue because there was no IPO
    # source of any kind in this list. TechCrunch's main feed is further down.
    ("vc",        "TechCrunch",      "https://techcrunch.com/category/venture/feed/"),
    ("vc",        "TechCrunch",      "https://techcrunch.com/category/startups/feed/"),
    ("vc",        "Crunchbase",      "https://news.crunchbase.com/feed/"),
    ("vc",        "Sifted",  "https://sifted.eu/feed"),
    ("vc",        "EU-Startups",     "https://www.eu-startups.com/feed/"),
    ("vc",        "Fortune", "https://fortune.com/feed/fortune-feeds/?id=3230629"),
    ("ipo",       "IPO Scoop",    "https://www.iposcoop.com/feed/"),
    ("ipo",       "BusinessWire",      "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRVQ=="),
    ("ipo",       "Nasdaq",  "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    # With GlobeNewswire dead, M&A / STRATEGIC rested on PR Newswire's general
    # financial-services list alone. The M&A list is the specific one. It also
    # carries German and French translations of the same releases, which
    # _FOREIGN_PATH drops; the English original is in the same feed.
    ("deals",     "PR Newswire",     "https://www.prnewswire.com/rss/financial-services-latest-news/"
                           "acquisitions-mergers-and-takeovers-list.rss"),
    ("deals",     "PR Newswire",     "https://www.prnewswire.com/rss/financial-services-latest-news/"
                           "financial-services-latest-news-list.rss"),

    # ---- AI & ENTERPRISE ----------------------------------------------------
    # The section runs 6-8 items an issue but had no section hint of its own,
    # and its one enterprise-AI outlet, VentureBeat, failed on every sampled run.
    ("ai",        "TechCrunch",    "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("ai",        "The Verge",     "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("ai",        "Ars Technica",  "https://arstechnica.com/ai/feed/"),

    # Last on purpose. TechCrunch's main feed repeats every category feed above,
    # and the first copy of a story decides its section hint (see dedup), so a
    # story from the AI category keeps "ai" instead of the main feed's "vc".
    ("vc",        "TechCrunch",      "https://techcrunch.com/feed/"),

    # ---- WORTH READING ------------------------------------------------------
    # Argument and explainer sources, not wires. Deliberately free-only: the
    # obvious picks here (Stratechery, The Economist, The Atlantic, New Yorker,
    # The Information) are all paywalled and would send the reader into a wall.
    ("read",      "Marginal Revolution",      "https://marginalrevolution.com/feed"),
    ("read",      "Construction Physics",      "https://www.construction-physics.com/feed"),
    ("read",      "Noahpinion",    "https://www.noahpinion.blog/feed"),
    ("read",      "Longreads",      "https://longreads.com/feed/"),
    ("read",      "Hacker News",      "https://hnrss.org/best?points=300"),
    ("read",      "MIT Tech Review",     "https://www.technologyreview.com/feed/"),
    ("read",      "Aeon",    "https://aeon.co/feed.rss"),
]

# Google News sitemaps, for outlets that publish no usable RSS. Three sources the
# reader named specifically have none: the Dallas Morning News and the Cavalier
# Daily never served a feed, and apnews.com blocks RSS outright in robots.txt
# ("Disallow: /*.rss"). A news sitemap carries the same three fields RSS does —
# canonical publisher URL, headline, publication timestamp — so it substitutes
# cleanly and, unlike a Google News RSS search, yields real article links rather
# than opaque news.google.com redirects that only resolve in a browser.
#
# dallasnews.com/sitemap/news/ap.xml is how AP reaches this issue at all: it is AP
# wire copy hosted on a free page with a working link.
SITEMAPS = [
    ("dallas",  "Dallas Morning News",
                "https://www.dallasnews.com/sitemap/news/local.xml"),
    ("us",      "AP",
                "https://www.dallasnews.com/sitemap/news/ap.xml"),
    ("uva",     "Cavalier Daily",
                "https://www.cavalierdaily.com/sitemap/recent.xml"),
    ("uva",     "Cavalier Daily",
                "https://www.cavalierdaily.com/sitemap/section/sports.xml"),
]

SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
         "n": "http://www.google.com/schemas/sitemap-news/0.9"}


def _slug_title(url: str) -> str:
    """Cavalier Daily's sitemap omits <news:title>; its slugs are readable."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", tail)
    return tail.replace("-", " ").strip().capitalize()


_URL_MONTH = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/")


def _lastmod_only(loc: str, when: dt.datetime) -> dt.datetime:
    """<lastmod> is when a page last changed, not when it ran. A Cavalier Daily
    story from /article/2025/03/ touched today would otherwise enter today's window
    as new. When the URL dates the story to an earlier month, cap the date at the
    end of that month and let the window decide."""
    m = _URL_MONTH.search(loc)
    if not m:
        return when
    y, mo = int(m.group(1)), int(m.group(2))
    if (y, mo) >= (when.year, when.month):
        return when
    return dt.datetime(y + mo // 12, mo % 12 + 1, 1, tzinfo=CT) - dt.timedelta(seconds=1)


def fetch_sitemap(entry: tuple[str, str, str], now: dt.datetime | None = None) -> list[dict]:
    section, tag, url = entry
    now = now or dt.datetime.now(CT)
    root = _download(section, tag, url, HDRS)
    if root is None:
        return []

    out, skipped = [], 0
    for e in root.findall("s:url", SM_NS):
        loc = (e.findtext("s:loc", namespaces=SM_NS) or "").strip()
        pub = e.findtext(".//n:publication_date", namespaces=SM_NS)
        raw = pub or e.findtext("s:lastmod", namespaces=SM_NS)
        if not (loc and raw) or _foreign(loc):
            continue
        when = _when(raw, now)
        if when is not None and not pub:
            when = _lastmod_only(loc, when)
        if when is None or when < now - MAX_AGE:
            skipped += 1
            continue
        title = e.findtext(".//n:title", namespaces=SM_NS) or _slug_title(loc)
        out.append({"title": _clean(title), "url": loc, "src": tag,
                    "section": section, "published": when.isoformat(), "_feed": url})
    _report(section, tag, url, len(out), skipped)
    return out


# Per-feed cap. Axios returns 100 items and some aggregators return 300; without a
# cap one chatty feed crowds the whole candidate list and starves every section
# below it. It is applied in main(), newest first, after the date window and after
# anything already shipped or duplicated is gone -- applied to the raw feed, as it
# used to be, a 3-day sports lookback spent a feed's 25 slots on stories that had
# already run or that another feed carried first.
PER_FEED = 25

TAGSTRIP = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return TAGSTRIP.sub("", s).replace("&amp;", "&").strip()


def _parsed(raw: str):
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom, dc:date, sitemaps)."""
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None


# Plausible publication dates. Anything outside is a placeholder or a mis-zoned
# stamp: a Go/.NET zero time (0001-01-01) or a 9999 sentinel raised OverflowError
# in astimezone() outside any try, which re-raised out of ex.map() in main() and
# would have lost the whole edition; a future-dated item sorted to the top of its
# section on every run.
MAX_AGE = dt.timedelta(days=30)
MAX_AHEAD = dt.timedelta(days=1)


def _when(raw: str, now: dt.datetime) -> dt.datetime | None:
    """A feed date in CT, or None when it is unreadable or implausible. Never
    raises: one bad date costs that item, never the feed or the run."""
    try:
        when = _parsed(raw.strip())
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        when = when.astimezone(CT)
        if not (now - MAX_AGE <= when <= now + MAX_AHEAD):
            return None
        return when
    except Exception:
        return None


# Translated copies. PR Newswire's M&A list carries German and French versions of
# English releases under /de/ and /fr/ paths (runner probe, 2026-10-14). The reader
# reads English, and the English original is in the same feed. Kept to the
# languages wires actually translate into, so fewer ordinary two-letter path
# segments (a state code on a local-news site) can be mistaken for one.
_FOREIGN_PATH = re.compile(
    r"^/(?:de|fr|es|it|pt|ja|zh|ko|nl)(?:[-_][a-z]{2,4})?(?:/|$)", re.I)


def _foreign(url: str) -> bool:
    try:
        return bool(_FOREIGN_PATH.match(urllib.parse.urlsplit(url).path))
    except ValueError:
        return False


# Namespaces whose elements are the real title/link/date. An extension's element
# of the same name (media:title, an atom:link inside an RSS item) is only a fallback.
_CORE_NS = {"", "http://purl.org/rss/1.0/", "http://www.w3.org/2005/Atom",
            "http://purl.org/dc/elements/1.1/"}

# Which date an item is filed under. Publication first: Atom's <updated> is a
# modification time, and Eater's restaurant maps are edited in place, so reading it
# first brought a June list back as today's news.
DATE_ORDER = ("pubDate", "date", "published", "updated")   # "date" is dc:date


def _split(tag) -> tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""           # a comment or processing instruction
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _child(it: ET.Element, name: str) -> str | None:
    """Text of the child of `it` called `name`, in whatever namespace it is in.

    RSS 1.0 puts <title> and <link> in a default namespace, so findtext('title')
    never matched: DW's /rdf/ feed parsed, every item was skipped, and the log said
    "0 items" on every run. A core-namespace child wins over an extension's, and an
    empty one is passed over (Atom's <link> carries its URL in href)."""
    fallback = None
    for c in it:
        ns, local = _split(c.tag)
        if local != name or not (c.text or "").strip():
            continue
        if ns in _CORE_NS:
            return c.text
        fallback = fallback or c.text
    return fallback


def _href(it: ET.Element) -> str:
    """Atom's <link href>, preferring the alternate (the article) over replies,
    enclosures and self links."""
    links = [c for c in it if _split(c.tag)[1] == "link" and c.get("href")]
    for c in links:
        if c.get("rel", "alternate") == "alternate":
            return c.get("href")
    return links[0].get("href") if links else ""


def _feed_items(root: ET.Element, section: str, tag: str, url: str,
                now: dt.datetime) -> tuple[list[dict], int]:
    out, skipped = [], 0
    for it in root.iter():
        if _split(it.tag)[1] not in ("item", "entry"):
            continue
        title = _child(it, "title")
        link = (_child(it, "link") or _href(it)).strip()
        if not (title and link) or _foreign(link):
            continue
        when = None
        for name in DATE_ORDER:
            raw = _child(it, name)
            if raw:
                when = _when(raw, now)
                break
        if when is None:
            skipped += 1
            continue
        cats = [_clean(c.text or c.get("term", "")) for c in it
                if _split(c.tag)[1] == "category"]
        row = {"title": _clean(title), "url": link, "src": tag,
               "section": section, "published": when.isoformat(), "_feed": url}
        if any(cats):
            row["categories"] = [c for c in cats if c]
        out.append(row)
    return out, skipped


def fetch(entry: tuple[str, str, str], now: dt.datetime | None = None) -> list[dict]:
    """Every dated item in one RSS 2.0, RSS 1.0 or Atom feed. Never raises."""
    section, tag, url = entry
    now = now or dt.datetime.now(CT)
    root = _download(section, tag, url, {"User-Agent": UA})
    if root is None:
        return []
    try:
        out, skipped = _feed_items(root, section, tag, url, now)
    except Exception as e:
        # Backstop only. A bad item is skipped inside _feed_items; this is for the
        # failure nobody anticipated, and it costs one feed, not the run.
        _fail(section, tag, url, f"{type(e).__name__}: {e}")
        return []
    _report(section, tag, url, len(out), skipped)
    return out


def collect_all(now: dt.datetime) -> list[list[dict]]:
    """Every feed and sitemap, in FEEDS-then-SITEMAPS order (dedup keeps the first
    copy, so order is meaningful). Each future is collected on its own: ex.map()
    re-raises the first worker exception straight out of main(), and one bad date
    in one feed would have cost the edition."""
    jobs = [(fetch, e) for e in FEEDS] + [(fetch_sitemap, e) for e in SITEMAPS]
    batches = []
    with futures.ThreadPoolExecutor(max_workers=32) as ex:
        running = [(e, ex.submit(fn, e, now)) for fn, e in jobs]
        for (section, tag, url), fut in running:
            try:
                batches.append(fut.result())
            except Exception as err:
                _fail(section, tag, url, f"{type(err).__name__}: {err}")
                batches.append([])
    return batches


# The earliest moments, in CT, at which each edition can collect: the sweep's AM
# window opens at 07:00 and its PM window at 12:00 (compass.yml). Windows are keyed
# to these rather than to when the previous run actually collected, so the windows
# overlap instead of leaving a gap (see window_start).
AM_RUN = (7, 0)
PM_RUN = (12, 0)

# Sections whose sources publish on a slower clock than the wires. A weekly essay
# or a Saturday UVA game would never survive a 5-hour window, so these look back
# far enough to have something real to choose from. The curator still has to pick
# only what is worth the reader's time.
SLOW = {"read": dt.timedelta(days=6), "sports": dt.timedelta(days=3),
        "dallas": dt.timedelta(days=2), "cre": dt.timedelta(days=2),
        "ipo": dt.timedelta(days=3), "retail": dt.timedelta(days=2),
        "uva": dt.timedelta(days=4), "food": dt.timedelta(days=4),
        "goingout": dt.timedelta(days=5)}

# Per-section cap, applied after the date filter. Uncapped, the lookback windows
# above produce ~580 candidates, three quarters of them sports, world and Dallas
# items competing for five slots each. That is not more choice, it is a diluted
# list that buries the CRE and deal items the issue is built on. Each cap is
# roughly 4-5x what the section actually ships. How the cap is filled matters as
# much as its size: see cap_sections.
SECTION_CAP = {"economy": 25, "us": 25, "world": 35, "dallas": 20, "sports": 25,
               "cre": 35, "retail": 15, "vc": 35, "ipo": 20, "deals": 25,
               "ai": 20, "read": 20, "uva": 15, "food": 15, "goingout": 15}


def window_start(edition: str, now: dt.datetime) -> dt.datetime:
    """Reach back to the earliest moment the previous edition could have
    collected, so consecutive windows overlap rather than leave a gap.

    The earlier fixed cutoffs (17:00 for AM, 11:00 for PM) left 5.75 hours of
    every weekday in no edition at all. Keying windows to nominal run times of
    08:30 and 13:45 instead still left gaps, because a run collects whenever its
    trigger fires -- the Worker at :15, the GitHub cron hours late. On 2026-10-14
    the PM collected at 12:57 and the next morning's window opened at 13:45, so 48
    minutes of wire, VC and M&A news reached no edition; every Worker-driven
    weekday lost 08:16-08:30 and 13:16-13:45 the same way. A window that starts at
    the earliest possible collection time covers the previous run whenever it
    happened. The overlap is free: anything an earlier day shipped is removed by
    drop_shipped, and a morning item repeating in the afternoon is allowed.
    """
    if edition == "pm":
        return now.replace(hour=AM_RUN[0], minute=AM_RUN[1], second=0, microsecond=0)
    y = now - dt.timedelta(days=1)
    h, m = PM_RUN if y.weekday() < 5 else AM_RUN
    return y.replace(hour=h, minute=m, second=0, microsecond=0)


def url_key(url: str) -> str:
    """Same article, different spelling: scheme, www, trailing slash, fragment and
    tracking parameters vary between feeds. Real query parameters are kept —
    Hacker News links are nothing but ?id=."""
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:          # e.g. a malformed IPv6 host; compare it verbatim
        return url.strip()
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "cmp", "mc_", "fbclid", "gclid", "__source"))]
    host = u.netloc.lower().removeprefix("www.")
    return f"{host}{u.path.rstrip('/')}?{urllib.parse.urlencode(q)}".rstrip("?")


def title_key(title: str) -> str | None:
    """Syndicated copies of one story share a headline under different URLs. Short
    headlines are skipped — "Morning Briefing" recurs without being a repeat."""
    t = re.sub(r"[^a-z0-9 ]", "", _clean(title).lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) >= 30 and t.count(" ") >= 5 else None


def load_shipped(prior_dir: str, today: str) -> tuple[set, set, list]:
    """URL keys and headline keys of every item shipped on an EARLIER day, plus
    the recent headlines themselves for the curator's story-level check.

    prior/<compass-ed-YYYY-MM-DD>-<id>/ holds each edition's issue.json and the
    candidates.json it was built from. The issue carries the curator's rewritten
    text, so the original headline is recovered from the candidates by URL.
    Artifacts are kept 7 days, which covers the longest SLOW lookback (6 days).
    """
    urls, titles, recent = set(), set(), []
    for path in sorted(glob.glob(os.path.join(prior_dir, "compass-*", "issue.json"))):
        name = os.path.basename(os.path.dirname(path))
        m = re.match(r"compass-(am|pm)-(\d{4}-\d{2}-\d{2})", name)
        if not m or m.group(2) >= today:
            continue            # today's AM may repeat; load_this_morning reads it
        try:
            issue = json.load(open(path))
            cpath = os.path.join(os.path.dirname(path), "candidates.json")
            heads = {c["url"]: c["title"] for c in json.load(open(cpath))["items"]} \
                if os.path.exists(cpath) else {}
        except Exception as e:
            print(f"  prior {name}: unreadable ({type(e).__name__}) — skipped")
            continue
        for s_ in issue.get("sections", []):
            for b in s_.get("blocks", []) or []:
                for it in b.get("items", []) or []:
                    u = it.get("url")
                    if not u:
                        continue
                    urls.add(url_key(u))
                    head = heads.get(u)
                    if head:
                        if title_key(head):
                            titles.add(title_key(head))
                        recent.append({"day": m.group(2), "title": _clean(head)})
    # The curator only needs the last two days for its same-story check; older
    # stories are already caught by link and headline above.
    days = sorted({r["day"] for r in recent})[-2:]
    recent = [r for r in recent if r["day"] in days]
    seen, dedup = set(), []
    for r in recent:
        if r["title"] not in seen:
            seen.add(r["title"])
            dedup.append(r)
    return urls, titles, dedup


def drop_shipped(items: list, urls: set, titles: set) -> tuple[list, list]:
    keep, gone = [], []
    for i in items:
        if url_key(i["url"]) in urls or (title_key(i["title"]) or "\0") in titles:
            gone.append(i)
        else:
            keep.append(i)
    return keep, gone


def dedup(items: list) -> tuple[list, int]:
    """One copy per story within this run. Only the exact URL used to be checked,
    so a tracking-parameter variant or a wire story carried by PBS, WFAA and the
    AP sitemap took a slot each and invited the same story twice in one issue. The
    first copy wins, in FEEDS order, which is why TechCrunch's main feed is last."""
    urls, titles, keep = set(), set(), []
    for i in items:
        u, t = url_key(i["url"]), title_key(i["title"])
        if u in urls or (t and t in titles):
            continue
        urls.add(u)
        if t:
            titles.add(t)
        keep.append(i)
    return keep, len(items) - len(keep)


def _published(i: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(i["published"])


def cap_sections(items: list) -> list:
    """Cap each feed at PER_FEED and each section at SECTION_CAP, sharing a
    section's slots out round-robin across its feeds, newest first within each.

    The cap used to be the section's newest N items. On 2026-10-09 and 10-14
    sports, read, ipo, world, dallas and uva all sat exactly at their caps: ten
    sports feeds returned 250 items for 25 slots, so the 3-day lookback kept only
    the last hours of the busiest NFL feeds, and seven WORTH READING feeds filled
    20 slots with roughly the last day, cutting the weekly essays the 6-day
    lookback exists for. Round robin gives every feed a share before any feed gets
    a second helping. Feeds are grouped by URL, not source tag: CBS and Yahoo each
    have several feeds.
    """
    feeds: dict[str, dict[str, list]] = {}
    for i in items:
        feed = i.get("_feed") or i.get("src", "")
        feeds.setdefault(i["section"], {}).setdefault(feed, []).append(i)
    out = []
    for sec in sorted(feeds):
        queues = [sorted(q, key=_published, reverse=True)[:PER_FEED]
                  for q in feeds[sec].values()]
        cap, took, rnd = SECTION_CAP.get(sec, 25), [], 0
        while len(took) < cap and any(len(q) > rnd for q in queues):
            row = sorted((q[rnd] for q in queues if len(q) > rnd),
                         key=_published, reverse=True)
            took += row[:cap - len(took)]
            rnd += 1
        pool = [i for q in feeds[sec].values() for i in q]
        print(f"  {sec}: {len(pool)} in window from {len(queues)} feeds -> "
              f"{len(took)} kept (cap {cap}){_watch(sec, pool, took)}")
        out += took
    out.sort(key=_published, reverse=True)
    return out


def _watch(sec: str, before: list, after: list) -> str:
    """For a section feeding a title-matched drought block (MAVERICKS), how many
    of its candidates survive the cap: the supply sections.py --prep will see."""
    try:
        import sections
        parts = []
        for b in sections.MATCH:
            if sec in sections.TRACKED.get(b, ()):
                n0 = sum(1 for i in before if sections.counts_for(b, i))
                n1 = sum(1 for i in after if sections.counts_for(b, i))
                parts.append(f"{b} {n0} -> {n1}")
        return f"; {', '.join(parts)}" if parts else ""
    except Exception:
        return ""


def load_this_morning(prior_dir: str, today: str) -> list[dict]:
    """What this morning's edition actually shipped, for the afternoon curator.

    The PM edition is "what changed since this morning" (owner decision,
    2026-10-14). The curator cannot do that blind, and the morning's items are
    deliberately left in the PM pool -- a follow-up needs the original in reach --
    so this list is how it knows which candidates already ran. The workflow drops
    today's AM artifact into prior/ for PM runs. Headlines come from that
    edition's candidates.json, because the issue holds the curator's rewritten
    text. Never raises: without the artifact the list is empty and the afternoon
    is curated without it.
    """
    paths = glob.glob(os.path.join(prior_dir, f"compass-am-{today}-*", "issue.json"))
    if not paths:
        print(f"  no AM edition for {today} in {prior_dir}/ -- this_morning is empty")
        return []

    def artifact_id(p: str) -> int:
        tail = os.path.basename(os.path.dirname(p)).rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else -1

    path = max(paths, key=artifact_id)      # a re-run's artifact is the newer one
    heads = {}
    try:
        cands = json.load(open(os.path.join(os.path.dirname(path), "candidates.json")))
        heads = {c["url"]: c["title"] for c in cands.get("items") or []
                 if isinstance(c, dict) and c.get("url") and c.get("title")}
    except Exception as e:
        # Only the original headlines are lost; the curator's text stands in.
        print(f"  this morning's candidates unreadable ({type(e).__name__})")
    try:
        issue = json.load(open(path))
        out = []
        # A fallback's front matter is boilerplate about the failed curation, not
        # news the reader got; its items did reach the reader, so they stay.
        fm = issue.get("front_matter")
        if isinstance(fm, str) and fm.strip() and not issue.get("_fallback"):
            out.append({"section": "FRONT MATTER", "title": html.unescape(
                TAGSTRIP.sub("", fm)).strip(), "url": None})
        for s in issue.get("sections") or []:
            if not isinstance(s, dict):
                continue
            for b in s.get("blocks") or []:
                for it in (b.get("items") or []) if isinstance(b, dict) else []:
                    if not isinstance(it, dict):
                        continue
                    url = it.get("url") or None
                    title = heads.get(url) or html.unescape(
                        TAGSTRIP.sub("", it.get("text") or "")).strip()
                    if title:
                        out.append({"section": s.get("title") or "",
                                    "title": _clean(title), "url": url})
    except Exception as e:
        print(f"  this morning ({path}): unreadable ({type(e).__name__}) -- skipped")
        return []
    print(f"  {len(out)} items from this morning's edition ({os.path.basename(os.path.dirname(path))})")
    return out


# candidates.json layout. The file runs past the 2,000 lines the curator's Read
# tool returns by default: written with indent=1, the 2026-10-14 PM pool put
# `attention` at about line 2,530, after every item. So the short control lists
# come first, in this order, and every item sits on one line. Nothing that reads
# the file depends on key order or whitespace.
HEAD = ("edition", "generated_at", "window_start", "by_section", "attention",
        "recently_shipped", "this_morning")

# The fields an item carries in the file. _feed and categories are working state
# for the caps and the satire check and must not reach the curator.
ITEM_FIELDS = ("title", "url", "src", "section", "published",
               "satire", "satire_reason", "dek")


def write_candidates(path: str, payload: dict) -> None:
    """Write candidates.json in the layout above. sections.py --prep reuses this,
    so `attention` lands near the top rather than after the items."""
    keys = [k for k in HEAD if k in payload]
    keys += [k for k in payload if k not in HEAD and k != "items"] + ["items"]
    parts = []
    for k in keys:
        v = payload[k] if k != "items" else [
            {f: i[f] for f in ITEM_FIELDS if f in i} for i in payload.get("items") or []]
        if isinstance(v, list) and v:
            rows = ",\n".join(json.dumps(x) for x in v)
            parts.append(f"{json.dumps(k)}: [\n{rows}\n]")
        else:
            parts.append(f"{json.dumps(k)}: {json.dumps(v)}")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("{\n" + ",\n".join(parts) + "\n}\n")
    os.replace(tmp, path)   # never leave a half-written file for the next stage

# ---- SATIRE -----------------------------------------------------------------
# Humor sections and satire outlets write headlines that read exactly like news.
# The curator sees only the headline, so it cannot tell them apart; this must.
# Every check below is cheap and independent, and any one of them flags the item.
# None of them can drop an item or fail the run -- satire is labelled, not banned.

SATIRE_WORDS = ("humor", "humour", "satire", "satirical", "parody", "spoof")

# Outlets that are satire end to end. None is in FEEDS today; this is the guard
# for the day one is added, or a wire story links through to one.
SATIRE_DOMAINS = {"theonion.com", "babylonbee.com", "clickhole.com",
                  "thehardtimes.net", "reductress.com", "newsthump.com",
                  "thebeaverton.com", "waterfordwhispersnews.com",
                  "thedailymash.co.uk", "duffelblog.com", "gomerblog.com"}

# Section sitemaps whose every URL is humor. The Cavalier Daily publishes one
# sitemap per section (sports.xml is already read above); humor.xml is its
# satire desk. A fetch failure only loses this one signal -- the page probe below
# still reads the article itself.
SATIRE_SITEMAPS = [
    "https://www.cavalierdaily.com/sitemap/section/humor.xml",
]

# Sources whose candidates are fetched and read before the curator sees them.
# The Cavalier Daily runs straight news and a humor desk under one masthead and
# one URL scheme, and its sitemap carries no headline at all, so the article page
# is the only place the difference shows. Kept to the sources that need it: one
# request per item, and these are the only ones that mix the two.
PROBE_SOURCES = {"Cavalier Daily"}

_SATIRE_PATH = re.compile(r"/(?:humou?r|satire|satirical|parody)(?:/|$)", re.I)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_LD_SECTION = re.compile(r'"articleSection"\s*:\s*"([^"]+)"', re.I)
# The disclaimer itself: "This article is satire", "The following is a work of
# satire", "is a satirical piece", "This is a humor column". Deliberately narrow --
# a news report ABOUT a satirist must not trip it.
_DISCLAIMER = re.compile(
    r"\b(?:this|the following)\s+(?:article|piece|story|column|post|is)\b[^.<]{0,40}?"
    r"\b(?:satire|satirical|humou?r\s+(?:piece|column|article))\b", re.I)


def satire_sitemap_urls() -> set:
    """Every link in the humor sitemaps, whatever its date: membership is the
    signal, so nothing here is windowed or capped."""
    keys = set()
    for sm in SATIRE_SITEMAPS:
        root = _download("satire", "satire-index", sm, HDRS)
        if root is None:
            continue
        locs = [(e.findtext("s:loc", namespaces=SM_NS) or "").strip()
                for e in root.findall("s:url", SM_NS)]
        keys |= {url_key(u) for u in locs if u}
        print(f"  satire/satire-index: {len(locs)} links")
    return keys


def _metas(page: str) -> dict:
    """<meta> name/property -> content, whatever order the attributes come in."""
    out = {}
    for tag in _META_TAG.findall(page):
        a = {k.lower(): v or q for k, v, q in _META_ATTR.findall(tag)}
        key = (a.get("property") or a.get("name") or "").lower()
        if key and "content" in a:
            out.setdefault(key, html.unescape(a["content"]))
    return out


def probe(item: dict) -> tuple[str | None, str | None]:
    """Read the article page: its declared section, and its opening line. Returns
    (reason if satire else None, description). Never raises."""
    try:
        req = urllib.request.Request(item["url"], headers=HDRS)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = _decompress(r.read(400_000),
                              (r.headers.get("Content-Encoding") or "").lower())
        page = raw.decode("utf-8", "ignore")
    except Exception:
        return None, None
    meta = _metas(page)
    desc = _clean(meta.get("og:description") or meta.get("description") or "") or None
    for sec_ in [meta.get("article:section", "")] + _LD_SECTION.findall(page):
        if any(w in sec_.lower() for w in SATIRE_WORDS):
            return f"page section '{sec_}'", desc
    text = html.unescape(TAGSTRIP.sub(" ", page))
    m = _DISCLAIMER.search(text)
    if m:
        return f"page says '{' '.join(m.group(0).split())}'", desc
    return None, desc


def flag_satire(items: list) -> None:
    """Mark every satirical candidate in place with satire=True and the reason."""
    index = satire_sitemap_urls()

    def mark(i, why):
        if not i.get("satire"):
            i["satire"], i["satire_reason"] = True, why

    for i in items:
        path = urllib.parse.urlsplit(i["url"]).path
        if _host(i["url"]) in SATIRE_DOMAINS:
            mark(i, "satire outlet")
        elif _SATIRE_PATH.search(path):
            mark(i, "humor/satire URL")
        elif any(w in c.lower() for c in i.get("categories", []) for w in SATIRE_WORDS):
            mark(i, "feed category")
        elif url_key(i["url"]) in index:
            mark(i, "humor section sitemap")

    todo = [i for i in items if i["src"] in PROBE_SOURCES]
    with futures.ThreadPoolExecutor(max_workers=8) as ex:
        for i, (why, desc) in zip(todo, ex.map(probe, todo)):
            if desc:
                # The article's own opening line. For headline-less sitemaps it is
                # the only text the curator gets beyond the slug.
                i["dek"] = desc[:300]
            if why:
                mark(i, why)

    for i in items:
        i.pop("categories", None)   # detection only; the curator does not need them
    flagged = [i for i in items if i.get("satire")]
    print(f"\n{len(flagged)} satire candidates flagged "
          f"({len(index)} humor-index links, {len(todo)} pages probed)")
    for i in flagged:
        print(f"  SATIRE ({i['satire_reason']}): {i['url']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], required=True)
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--prior", default="prior",
                    help="directory of earlier shipped editions (see load_shipped)")
    args = ap.parse_args()

    now = dt.datetime.now(CT)
    today = f"{now:%Y-%m-%d}"
    cutoff = window_start(args.edition, now)
    print(f"collecting {args.edition.upper()} candidates published since "
          f"{cutoff:%Y-%m-%d %H:%M %Z}")

    batches = collect_all(now)

    def floor(section: str) -> dt.datetime:
        return min(cutoff, now - SLOW[section]) if section in SLOW else cutoff

    kept = [i for b in batches for i in b if _published(i) >= floor(i["section"])]
    kept, twins = dedup(kept)
    print(f"\n{twins} within-run duplicates dropped (same link or same headline)")

    shipped_urls, shipped_titles, recent = load_shipped(args.prior, today)
    kept, gone = drop_shipped(kept, shipped_urls, shipped_titles)
    print(f"{len(gone)} candidates already shipped on an earlier day — dropped "
          f"(checked against {len(shipped_urls)} prior links)")

    print("\nper section, in window -> kept:")
    items = cap_sections(kept)
    try:
        flag_satire(items)
    except Exception as e:
        # Losing the flags must not lose the edition; the curator's own satire
        # rules still apply to what it can see.
        print(f"satire check failed ({type(e).__name__}: {e}) — no items flagged")
    by_sec: dict[str, int] = {}
    for i in sorted(items, key=lambda i: i["section"]):
        by_sec[i["section"]] = by_sec.get(i["section"], 0) + 1
    print(f"\n{len(items)} candidates: " +
          ", ".join(f"{k} {v}" for k, v in by_sec.items()))

    payload = {"edition": args.edition, "generated_at": now.isoformat(),
               "window_start": cutoff.isoformat(), "by_section": by_sec,
               "recently_shipped": recent, "items": items}
    if args.edition == "pm":
        payload["this_morning"] = load_this_morning(args.prior, today)
    write_candidates(args.out, payload)


if __name__ == "__main__":
    main()
