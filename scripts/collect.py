#!/usr/bin/env python3
"""Stage 1 of 3: collect candidate items from RSS. Deterministic, no model.

Why RSS rather than fetching index pages:
  - Index pages lie about dates. Commercial Observer stamps every row with the
    current date; on 2026-07-30 the real publish dates spanned 22-30 Jul. RSS
    <pubDate> is authoritative and matched scraped ground truth exactly.
  - Several sources that hard-block a normal fetch (The Real Deal, Connect CRE,
    GlobeSt) serve RSS without complaint.
  - One request per source instead of one per article, and ~5k tokens of candidates
    instead of ~235k tokens of index HTML. That matters when the model runs on a
    Claude subscription quota rather than metered API billing.

Writes candidates.json for the curation stage.
"""

import argparse
import concurrent.futures as futures
import datetime as dt
import json
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# (source tag, feed url). Tags become the [XX] label in the rendered issue.
# CNBC, Axios, The Real Deal and Connect CRE all hard-403 a normal page fetch but
# serve RSS without complaint — RSS is the only way to reach them. Reuters retired
# its public feeds and has no working RSS at all, so it stays out of reach.
# Do not add feeds.a.dj.com (WSJ Markets): it is frozen, still serving January items.
FEEDS = [
    # CRE
    ("Bisnow",  "https://www.bisnow.com/national/rss"),
    ("Bisnow",  "https://www.bisnow.com/dallas-ft-worth/rss"),
    ("CO",      "https://commercialobserver.com/feed/"),
    ("TRD",     "https://therealdeal.com/national/feed/"),
    ("Connect", "https://www.connectcre.com/feed/"),
    # Tech / VC
    ("TC",      "https://techcrunch.com/feed/"),
    ("TC",      "https://techcrunch.com/category/venture/feed/"),
    # Retail
    ("RD",      "https://www.retaildive.com/feeds/news/"),
    # Deal wires
    ("GNW",     "https://www.globenewswire.com/RssFeed/subjectcode/22-Mergers%20And%20"
                "Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20And%20Acquisitions"),
    ("PRN",     "https://www.prnewswire.com/rss/financial-services-latest-news/"
                "financial-services-latest-news-list.rss"),
    # Macro / general
    ("CNBC",    "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("CNBC",    "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                "?partnerId=wrss01&id=10000664"),
    ("Axios",   "https://www.axios.com/feeds/feed.rss"),
    ("MW",      "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("NPR",     "https://feeds.npr.org/1006/rss.xml"),
]


def fetch(entry: tuple[str, str]) -> list[dict]:
    tag, url = entry
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        print(f"  {tag}: FAILED ({type(e).__name__})")
        return []

    out = []
    for it in root.iter("item"):
        title, link, pub = (it.findtext("title"), it.findtext("link"),
                            it.findtext("pubDate"))
        if not (title and link and pub):
            continue
        try:
            when = parsedate_to_datetime(pub)
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        out.append({"title": title.strip(), "url": link.strip(),
                    "src": tag, "published": when.astimezone(CT).isoformat()})
    print(f"  {tag}: {len(out)} items")
    return out


def window_start(edition: str, now: dt.datetime) -> dt.datetime:
    """Morning covers overnight; afternoon covers only what the morning couldn't."""
    if edition == "am":
        return (now - dt.timedelta(days=1)).replace(hour=17, minute=0, second=0,
                                                    microsecond=0)
    return now.replace(hour=11, minute=0, second=0, microsecond=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], required=True)
    ap.add_argument("--out", default="candidates.json")
    args = ap.parse_args()

    now = dt.datetime.now(CT)
    cutoff = window_start(args.edition, now)
    print(f"collecting {args.edition.upper()} candidates published since {cutoff:%Y-%m-%d %H:%M %Z}")

    with futures.ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        batches = list(ex.map(fetch, FEEDS))

    seen, items = set(), []
    for b in batches:
        for i in b:
            if i["url"] in seen:
                continue
            if dt.datetime.fromisoformat(i["published"]) < cutoff:
                continue
            seen.add(i["url"])
            items.append(i)

    items.sort(key=lambda i: i["published"], reverse=True)
    payload = {"edition": args.edition, "generated_at": now.isoformat(),
               "window_start": cutoff.isoformat(), "items": items}
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=1)

    approx_tokens = len(json.dumps(items)) // 4
    print(f"\n{len(items)} candidates in window -> {args.out} (~{approx_tokens:,} tokens)")
    if not items:
        raise SystemExit("no candidates in window — refusing to build an empty issue")


if __name__ == "__main__":
    main()
