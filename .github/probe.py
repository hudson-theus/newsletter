import gzip, re, time, urllib.request, urllib.error, zlib, json, subprocess
import xml.etree.ElementTree as ET
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
FEEDS = [
 "https://therealdeal.com/national/feed/", "https://therealdeal.com/feed/", "https://therealdeal.com/texas/feed/",
 "https://venturebeat.com/feed/", "https://feeds.feedburner.com/venturebeat/SZYF", "https://venturebeat.com/category/ai/feed/",
 "https://www.globenewswire.com/RssFeed/subjectcode/22-Mergers%20And%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20And%20Acquisitions",
 "https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20and%20Acquisitions",
 "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
 "https://www.prnewswire.com/rss/financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss",
 "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
 "https://dailyprogress.com/search/?f=rss&t=article&c=sports*&l=50",
 "https://dailyprogress.com/search/?f=rss&t=article&c=sports/college/uva*&l=50",
 "https://dailyprogress.com/search/?f=rss&t=article&l=50",
 "https://rss.dw.com/rdf/rss-en-all", "https://rss.dw.com/xml/rss-en-all", "https://rss.dw.com/rdf/rss-en-world",
 "https://techcrunch.com/category/artificial-intelligence/feed/",
 "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
 "https://arstechnica.com/ai/feed/",
 "https://www.technologyreview.com/topic/artificial-intelligence/feed",
 "https://www.dallasnews.com/sitemap/news/ap.xml", "https://www.dallasnews.com/sitemap/news/local.xml",
 "https://www.texasmonthly.com/feed/", "https://hnrss.org/best?points=300",
 "https://www.mavsmoneyball.com/rss/index.xml", "https://www.bloggingtheboys.com/rss/index.xml",
 "https://www.streakingthelawn.com/rss/index.xml",
 "https://richmond.com/search/?f=rss&t=article&c=sports/college/uva*&l=50",
 "https://www.cavalierdaily.com/sitemap/recent.xml",
]
def get(url, ua=CHROME, timeout=30):
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(); enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
            elif enc == "deflate":
                try: raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except Exception: raw = zlib.decompress(raw)
            return r.status, r.headers.get("Content-Type"), raw, time.time() - t0, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()[:300], time.time() - t0, url
    except Exception as e:
        return f"{type(e).__name__}: {e}", None, b"", time.time() - t0, url
def summarize(raw):
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        return f"PARSE {type(e).__name__}: {str(e)[:80]}"
    items = [e for e in root.iter() if e.tag.split('}')[-1] in ("item", "entry", "url")]
    dates = []
    for it in items:
        for c in it.iter():
            if c.tag.split('}')[-1] in ("pubDate", "date", "updated", "published", "publication_date", "lastmod") and c.text:
                dates.append(c.text.strip()); break
    titles = []
    for it in items[:3]:
        for c in it:
            if c.tag.split('}')[-1] == "title" and c.text: titles.append(c.text.strip()[:70]); break
    return f"{len(items)} items, first dates {dates[:2]}, titles {titles}"
print("=== FEEDS")
for u in FEEDS:
    st, ct, raw, dt_, final = get(u)
    line = f"[{st}] {dt_:.1f}s {ct} {len(raw)}B {u}"
    if final != u: line += f" -> {final}"
    print(line)
    if st == 200: print("     ", summarize(raw))
    else: print("      body:", raw[:160])
print("\n=== FRED direct")
fg = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"
for name, ua in (("chrome-ua", CHROME), ("python-ua", "Python-urllib/3.12"), ("compass-ua", "compass-newsletter/1.0")):
    st, ct, raw, dt_, _ = get(fg, ua, timeout=40)
    print(f"fredgraph {name}: [{st}] {dt_:.1f}s {raw[:80]!r}")
st, ct, raw, dt_, _ = get("https://api.stlouisfed.org/fred/series?series_id=UNRATE&file_type=json", "compass-newsletter/1.0", timeout=40)
print(f"api.stlouisfed.org (no key): [{st}] {dt_:.1f}s {raw[:120]!r}")
print("\n=== Worker")
W = "https://compass-trigger.hudson-394.workers.dev"
for path in ("/", "/fred?series=UNRATE", "/fred?series=DRCRELEXFACBS", "/fred?series=BAMLH0A0HYM2", "/fred?series=CPILFESL", "/fred?series=CPIAUCSL", "/fred?series=GDP"):
    st, ct, raw, dt_, _ = get(W + path, "compass-newsletter/1.0", timeout=40)
    body = raw.decode("utf-8", "replace")
    if st == 200 and path.startswith("/fred"):
        d = json.loads(body); ob = d.get("observations", [])
        body = f"{len(ob)} obs, newest {ob[:2]}"
    print(f"{path}: [{st}] {dt_:.1f}s {body[:300]}")
