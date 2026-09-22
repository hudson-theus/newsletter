#!/usr/bin/env python3
"""Stage 1 of 3: collect candidate items from RSS. Deterministic, no model.

Why RSS rather than fetching index pages:
  - Index pages lie about dates. Commercial Observer stamps every row with the
    current date; on 2026-07-30 the real publish dates spanned 22-30 Jul. RSS
    <pubDate> is authoritative and matched scraped ground truth exactly.
  - Several sources that hard-block a normal fetch (The Real Deal, Connect CRE,
    GlobeSt) serve RSS without complaint.
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

Writes candidates.json for the curation stage.
"""

import argparse
import concurrent.futures as futures
import datetime as dt
import glob
import gzip
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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
NS_FIX = {
    "media":   "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "atom":    "http://www.w3.org/2005/Atom",
    "wfw":     "http://wellformedweb.org/CommentAPI/",
    "slash":   "http://purl.org/rss/1.0/modules/slash/",
    "sy":      "http://purl.org/rss/1.0/modules/syndication/",
    "georss":  "http://www.georss.org/georss/",
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
    m = re.search(r"<rss\b[^>]*|<feed\b[^>]*", txt)
    if not m:
        return raw
    ins = " ".join(f'xmlns:{p}="{NS_FIX[p]}"' for p in missing)
    return (txt[:m.end()] + " " + ins + txt[m.end():]).encode("utf-8")

# (section, source tag, feed url). Tags become the [XX] label in the rendered issue.
# CNBC, Axios, The Real Deal and Connect CRE all hard-403 a normal page fetch but
# serve RSS without complaint. Reuters retired its public feeds and has no working
# RSS at all, so it stays out of reach.
#
# Do not add: feeds.a.dj.com (WSJ Markets) is frozen, still serving January items.
# ESPN's RSS returns an empty body, Renaissance Capital / Nasdaq-IPO / Axios-local
# / Dallas Morning News / Dallas Business Journal / KERA all 404, and StrictlyVC
# and KUT are abandoned (newest items from 2020 and 2021).
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
    ("world",     "DW",      "https://rss.dw.com/rdf/rss-en-all"),
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
    ("sports",    "Daily Progress",
                           "https://dailyprogress.com/search/?f=rss&t=article&c=sports*&l=50"),
    ("sports",    "Richmond Times-Dispatch",
                           "https://richmond.com/search/?f=rss&t=article&c=sports*&l=50"),

    # ---- CRE ----------------------------------------------------------------
    ("cre",       "Bisnow",  "https://www.bisnow.com/national/rss"),
    ("cre",       "Bisnow",  "https://www.bisnow.com/dallas-ft-worth/rss"),
    ("cre",       "Commercial Observer",      "https://commercialobserver.com/feed/"),
    ("cre",       "The Real Deal",     "https://therealdeal.com/national/feed/"),
    ("cre",       "Connect", "https://www.connectcre.com/feed/"),
    ("cre",       "Trepp",   "https://www.trepp.com/trepptalk/rss.xml"),
    ("retail",    "Retail Dive",      "https://www.retaildive.com/feeds/news/"),

    # ---- VC / STARTUPS / IPO ------------------------------------------------
    # IPOS & LISTINGS used to come up empty every issue because there was no IPO
    # source of any kind in this list.
    ("vc",        "TechCrunch",      "https://techcrunch.com/feed/"),
    ("vc",        "TechCrunch",      "https://techcrunch.com/category/venture/feed/"),
    ("vc",        "TechCrunch",      "https://techcrunch.com/category/startups/feed/"),
    ("vc",        "Crunchbase",      "https://news.crunchbase.com/feed/"),
    ("vc",        "VentureBeat",      "https://venturebeat.com/feed/"),
    ("vc",        "Sifted",  "https://sifted.eu/feed"),
    ("vc",        "EU-Startups",     "https://www.eu-startups.com/feed/"),
    ("vc",        "Fortune", "https://fortune.com/feed/fortune-feeds/?id=3230629"),
    ("ipo",       "IPO Scoop",    "https://www.iposcoop.com/feed/"),
    ("ipo",       "BusinessWire",      "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRVQ=="),
    ("ipo",       "Nasdaq",  "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    ("deals",     "GlobeNewswire",     "https://www.globenewswire.com/RssFeed/subjectcode/22-Mergers%20And%20"
                           "Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20And%20Acquisitions"),
    ("deals",     "PR Newswire",     "https://www.prnewswire.com/rss/financial-services-latest-news/"
                           "financial-services-latest-news-list.rss"),

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


def fetch_sitemap(entry: tuple[str, str, str]) -> list[dict]:
    section, tag, url = entry
    req = urllib.request.Request(url, headers=HDRS)
    root = None
    for attempt, timeout in ((1, 25), (2, 40), (3, 60)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            root = ET.fromstring(body)
            break
        except Exception as e:
            if attempt == 3:
                print(f"  {section}/{tag}: FAILED ({type(e).__name__})")
                return []

    out = []
    for e in root.findall("s:url", SM_NS):
        loc = e.findtext("s:loc", namespaces=SM_NS)
        raw = (e.findtext(".//n:publication_date", namespaces=SM_NS)
               or e.findtext("s:lastmod", namespaces=SM_NS))
        if not (loc and raw):
            continue
        try:
            when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        title = e.findtext(".//n:title", namespaces=SM_NS) or _slug_title(loc)
        out.append({"title": _clean(title), "url": loc.strip(), "src": tag,
                    "section": section,
                    "published": when.astimezone(CT).isoformat()})
    out.sort(key=lambda i: i["published"], reverse=True)
    out = out[:PER_FEED]
    print(f"  {section}/{tag}: {len(out)} items")
    return out


# Per-feed cap. Axios returns 100 items and some aggregators return 300; without a
# cap one chatty feed crowds the whole candidate list and starves every section
# below it. Feeds are read newest-first, so the cap keeps the freshest items.
PER_FEED = 25

TAGSTRIP = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return TAGSTRIP.sub("", s).replace("&amp;", "&").strip()


def _parsed(raw: str):
    """RSS pubDate, Atom updated, or dc:date — feeds are inconsistent about which."""
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None


def fetch(entry: tuple[str, str, str]) -> list[dict]:
    section, tag, url = entry
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    root = None
    # Three attempts with a widening timeout. Yesterday's afternoon edition lost
    # GlobeNewswire to a timeout and The Real Deal to a truncated body; both are
    # transient, and a feed that drops out silently shrinks a section. The M&A
    # wire in particular is slow enough to miss a 25s read on a bad day.
    for attempt, timeout in ((1, 25), (2, 40), (3, 60)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = _decompress(r.read(),
                                  (r.headers.get("Content-Encoding") or "").lower())
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                root = ET.fromstring(_repair_ns(raw))
            break
        except Exception as e:
            if attempt == 3:
                print(f"  {section}/{tag}: FAILED ({type(e).__name__})")
                return []

    out = []
    for it in root.iter():
        if not (it.tag.endswith("item") or it.tag.endswith("entry")):
            continue
        title = it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title")
        link = it.findtext("link") or ""
        if not link:  # Atom puts the URL in an attribute
            a = it.find("{http://www.w3.org/2005/Atom}link")
            link = a.get("href", "") if a is not None else ""
        pub = (it.findtext("pubDate")
               or it.findtext("{http://purl.org/dc/elements/1.1/}date")
               or it.findtext("{http://www.w3.org/2005/Atom}updated")
               or it.findtext("{http://www.w3.org/2005/Atom}published"))
        if not (title and link and pub):
            continue
        when = _parsed(pub)
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        out.append({"title": _clean(title), "url": link.strip(), "src": tag,
                    "section": section,
                    "published": when.astimezone(CT).isoformat()})
        if len(out) >= PER_FEED:
            break
    print(f"  {section}/{tag}: {len(out)} items")
    return out


# Scheduled run times in CT. Windows are keyed to these, not to the actual start
# time, so a late run still covers the right span instead of a shifted one.
AM_RUN = (8, 30)
PM_RUN = (13, 45)

# Sections whose sources publish on a slower clock than the wires. A weekly essay
# or a Saturday UVA game would never survive a 5-hour window, so these look back
# far enough to have something real to choose from. The curator still has to pick
# only what is worth the reader's time.
SLOW = {"read": dt.timedelta(days=6), "sports": dt.timedelta(days=3),
        "dallas": dt.timedelta(days=2), "cre": dt.timedelta(days=2),
        "ipo": dt.timedelta(days=3), "retail": dt.timedelta(days=2),
        "uva": dt.timedelta(days=4), "food": dt.timedelta(days=4),
        "goingout": dt.timedelta(days=5)}

# Per-section cap, applied newest-first after the date filter. Uncapped, the
# lookback windows above produce ~580 candidates, three quarters of them sports,
# world and Dallas items competing for five slots each. That is not more choice,
# it is a diluted list that buries the CRE and deal items the issue is built on.
# Each cap is roughly 4-5x what the section actually ships.
SECTION_CAP = {"economy": 25, "us": 25, "world": 35, "dallas": 20, "sports": 25,
               "cre": 35, "retail": 15, "vc": 35, "ipo": 20, "deals": 25,
               "read": 20, "uva": 15, "food": 15, "goingout": 15}


def window_start(edition: str, now: dt.datetime) -> dt.datetime:
    """Start where the previous edition stopped, so coverage is continuous.

    The earlier fixed cutoffs (17:00 for AM, 11:00 for PM) left 5.75 hours of
    every weekday in no edition at all — 08:30-11:00 and 13:45-17:00. The second
    of those is after the US close, when a lot of deal news lands.
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
    u = urllib.parse.urlsplit(url.strip())
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
            continue            # today's morning edition is allowed to repeat
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], required=True)
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--prior", default="prior",
                    help="directory of earlier shipped editions (see load_shipped)")
    args = ap.parse_args()

    now = dt.datetime.now(CT)
    cutoff = window_start(args.edition, now)
    print(f"collecting {args.edition.upper()} candidates published since "
          f"{cutoff:%Y-%m-%d %H:%M %Z}")

    with futures.ThreadPoolExecutor(max_workers=32) as ex:
        batches = list(ex.map(fetch, FEEDS)) + list(ex.map(fetch_sitemap, SITEMAPS))

    seen, kept = set(), []
    for b in batches:
        for i in b:
            if i["url"] in seen:
                continue
            floor = min(cutoff, now - SLOW[i["section"]]) if i["section"] in SLOW else cutoff
            if dt.datetime.fromisoformat(i["published"]) < floor:
                continue
            seen.add(i["url"])
            kept.append(i)

    shipped_urls, shipped_titles, recent = load_shipped(args.prior, f"{now:%Y-%m-%d}")
    kept, gone = drop_shipped(kept, shipped_urls, shipped_titles)
    print(f"\n{len(gone)} candidates already shipped on an earlier day — dropped "
          f"(checked against {len(shipped_urls)} prior links)")

    # Newest-first, then cap each section so one chatty feed cannot crowd out a
    # section that only had a handful of candidates to begin with.
    kept.sort(key=lambda i: i["published"], reverse=True)
    taken: dict[str, int] = {}
    items = []
    for i in kept:
        s_ = i["section"]
        if taken.get(s_, 0) >= SECTION_CAP.get(s_, 25):
            continue
        taken[s_] = taken.get(s_, 0) + 1
        items.append(i)
    by_sec: dict[str, int] = {}
    for i in items:
        by_sec[i["section"]] = by_sec.get(i["section"], 0) + 1
    print(f"\n{len(items)} candidates: " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_sec.items())))

    payload = {"edition": args.edition, "generated_at": now.isoformat(),
               "window_start": cutoff.isoformat(), "by_section": by_sec,
               "items": items, "recently_shipped": recent}
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=1)


if __name__ == "__main__":
    main()
