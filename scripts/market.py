#!/usr/bin/env python3
"""Stage 1b of 3: hard numbers for the CRE snapshot. Deterministic, no model.

The reader asked for CRE framed as market health and direction rather than as a
feed of individual acquisitions. Headlines alone cannot do that: on a quiet day
the CRE wires carry three shopping-center trades and nothing about the state of
the market. Numbers say something even then, when they arrive. Any source can be
down or stale on a given run, and then its lines are absent, not guessed.

Everything here is free and needs no secret in this repo:
  - Nasdaq's public quote API for SPY: today's minute-by-minute tape, which draws
    the cover chart, plus a year of daily closes for the weekly move and realised
    vol. Yahoo's chart API was the obvious choice and
    answers 429 to scripted clients; Stooq now serves a JS challenge. Verified
    2026-09-22. Since 2026-11-10 Nasdaq answers 403 to GitHub's runners, so it is
    read through the Worker's /spy route first, then direct, then Tiingo (keyed,
    as a Worker secret). When all three fail, state/spy.json (the last shipped
    run's closes) still charts SPY on the cover. See the SPY section below.
  - Treasury.gov daily yield curve XML. The 10Y is the number that sets what the
    reader's clients' buyers can borrow at, so it leads. Verified 2026-08-27.
  - Freddie Mac PMMS weekly mortgage survey, as the housing-side read.
  - FRED for the slower series: CRE loan delinquency (quarterly), unemployment,
    core CPI (CPILFESL, reported as a year-over-year rate) and the high-yield
    spread. Read through the compass-trigger Worker's /fred route, which keeps the
    FRED API key as a Worker secret, with fredgraph.csv as the fallback. Through
    2026-10-14 this read fredgraph.csv with a browser User-Agent and every logged
    run timed out: FRED answers Python's default User-Agent at once and stalls
    ~40s on a browser or custom one (runner probe, 2026-10-15). FRED dates an
    observation by the first day of its period, so a FRED as_of of 2026-09-01 is
    September's figure and 2026-04-01 on the delinquency rate is the second
    quarter's.

Writes two files:
  market.json         scalars only, for the curator, who reads the whole file.
                      Each source dates itself under its own key —
                      treasury_as_of, mortgage30_as_of, spy_as_of and spy_session
                      (the trading session spy and spy_day_pct describe) — and
                      each FRED key is {"value", "as_of"}. There is no bare
                      "as_of": on 2026-10-14 the PMMS week overwrote the Treasury
                      date and the brief dated the 10Y "Oct. 8".
  market_series.json  the chart arrays (y10_series, spy_series, spy_intraday)
                      for art.py. They were ~95% of market.json and pushed the
                      FRED keys past the curator's 2,000-line read.
load_market() puts the two back together for art.py and publish.py.

Never raises. A snapshot with three of five lines is fine; a snapshot that takes
the edition down with it is not.
"""

import concurrent.futures as futures
import zoneinfo
import csv
import datetime as dt
import io
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}
CT = zoneinfo.ZoneInfo("America/Chicago")

# Chart data. Written to market_series.json, never to market.json.
SERIES_KEYS = ("y10_series", "spy_series", "spy_intraday",
               # set only when spy_series came from state/spy.json; art.py dates
               # the cover caption by it. Kept out of the curator's market.json.
               "spy_cache_as_of")


def load_market(path: str = "market.json") -> dict:
    """market.json with its chart arrays merged back in, for art.py and publish.py.

    Raises like json.load when market.json itself is missing or unreadable; callers
    already treat that as "no market data". The series file beside it is optional:
    without it the cover falls back to a plainer chart, it never costs the numbers.
    """
    with open(path) as f:
        out = json.load(f)
    if not isinstance(out, dict):
        raise ValueError(f"{path} is not a JSON object")
    side = os.path.join(os.path.dirname(path), "market_series.json")
    try:
        with open(side) as f:
            series = json.load(f)
    except FileNotFoundError:
        return out
    except Exception as e:
        print(f"  {side}: unreadable ({type(e).__name__}) — charts fall back")
        return out
    if isinstance(series, dict):
        for k, v in series.items():
            out.setdefault(k, v)
    return out


def _today() -> dt.date:
    """The edition's date. Editions are dated in Chicago; the runner's clock is UTC."""
    return dt.datetime.now(CT).date()


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_plain(url: str, timeout: int = 30) -> bytes:
    """No User-Agent of our own, so urllib sends its default "Python-urllib/3.x" —
    the one FRED answers. A browser or custom UA is held ~40s and then dropped."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _treasury_year(year: int) -> list[dict]:
    """One calendar year of the daily yield-curve feed, one dict per print."""
    url = ("https://home.treasury.gov/resource-center/data-chart-center/"
           "interest-rates/pages/xml?data=daily_treasury_yield_curve"
           f"&field_tdr_date_value={year}")
    rows = []
    for e in ET.fromstring(_get(url, 30)).findall("a:entry", NS):
        p = e.find(".//m:properties", NS)
        if p is not None:
            rows.append({c.tag.split("}")[1]: c.text for c in p})
    return rows


def treasury() -> dict:
    """Daily constant-maturity yields. Returns latest plus a week-ago comparison."""
    today = _today()
    rows = _treasury_year(today.year)
    if len(rows) < 90:
        # The feed is one calendar year. On Jan 1 it is empty, and through the first
        # week it has fewer than the six prints a weekly move needs (one print read
        # as "+0bp / wk"); the sparkline wants 90. Last year fills in, in its own
        # try so a failure there never costs this year's prints.
        try:
            rows = _treasury_year(today.year - 1) + rows
        except Exception as e:
            print(f"  treasury {today.year - 1}: unavailable ({type(e).__name__})"
                  " — this year's prints only")
    # Keyed and sorted by date rather than trusting feed order; a print that is in
    # both years' feeds counts once.
    by_date = {}
    for v in rows:
        d = (v.get("NEW_DATE") or "")[:10]
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            continue
        by_date[d] = v
    dates = sorted(by_date)
    if not dates:
        return {}

    def f(d, k):
        try:
            return float(d[k])
        except (KeyError, TypeError, ValueError):
            return None

    cur = by_date[dates[-1]]
    # Five business days back is a week of trading. With fewer prints than that
    # there is no honest weekly move, so none is reported.
    prev = by_date[dates[-6]] if len(dates) >= 6 else {}

    out = {"treasury_as_of": dates[-1]}
    for key, field in (("y2", "BC_2YEAR"), ("y10", "BC_10YEAR"), ("y30", "BC_30YEAR")):
        now, was = f(cur, field), f(prev, field)
        if now is None:
            continue
        out[key] = now
        if was is not None:
            out[key + "_wk_bps"] = round((now - was) * 100)
    if out.get("y10") is not None and out.get("y2") is not None:
        out["curve_2s10s_bps"] = round((out["y10"] - out["y2"]) * 100)

    # The daily closes are already parsed and in hand. art.py draws the sparkline
    # and the cover's last-resort chart straight from them, so keep them rather
    # than fetching twice — a year at most, also when last year was merged in.
    cutoff = (today - dt.timedelta(days=366)).isoformat()
    hist = [[d, f(by_date[d], "BC_10YEAR")] for d in dates if d >= cutoff]
    hist = [[d, y] for d, y in hist if y is not None]
    if hist:
        out["y10_series"] = hist
    return out


def _iso(s: str) -> str | None:
    """PMMS dates are M/D/YYYY; every date this file writes is ISO."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def freddie() -> dict:
    """30-year fixed mortgage, weekly. Housing-side read on the same rate path."""
    rows = list(csv.reader(io.StringIO(
        _get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv", 25).decode())))
    vals = {}
    for r in rows:
        if len(r) < 2:
            continue
        d = _iso(r[0])
        try:
            v = float(r[1])
        except ValueError:
            continue
        if d:
            vals[d] = v
    vals = sorted(vals.items())
    if not vals:
        return {}
    out = {"mortgage30": vals[-1][1]}
    if len(vals) >= 2:
        out["mortgage30_wk_bps"] = round((vals[-1][1] - vals[-2][1]) * 100)
    out["mortgage30_as_of"] = vals[-1][0]
    return out


# --- SPY ---------------------------------------------------------------------
#
# From 2026-11-10 api.nasdaq.com answered 403 to every GitHub runner while still
# answering a laptop, and every edition for a week charted the 10Y instead of SPY
# with only a ::warning:: in the log. So SPY now has three ways in, tried in order:
#   1. Nasdaq through the compass-trigger Worker's /spy route (Cloudflare's IPs);
#   2. Nasdaq direct, as before (still works from a laptop, and may recover);
#   3. Tiingo through the Worker, keyed by its TIINGO_TOKEN secret.
# Each answers in its own shape and is normalised here, so spy() and
# spy_intraday() return exactly what they always have. Overridable like FRED_URL.
SPY_URL = (os.environ.get("COMPASS_SPY_URL")
           or "https://compass-trigger.hudson-394.workers.dev/spy")
NY = zoneinfo.ZoneInfo("America/New_York")


def _worker_get(params: dict, timeout: int = 30):
    """One /spy call. A named User-Agent, for the same reason as _fred_worker."""
    url = f"{SPY_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "compass-newsletter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # The Worker says why (e.g. "nasdaq answered 403"); keep it in the reason.
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = None
        raise RuntimeError(f"HTTP {e.code}" + (f": {msg}" if msg else "")) from None


def _nasdaq_data(body) -> dict:
    """Nasdaq's envelope is {"data": ..., "status": {...}}. A blocked or unknown
    request can come back 200 with data null, which used to surface as a bare
    TypeError three calls later."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise ValueError("no data in Nasdaq answer")
    return body["data"]


def _nasdaq(kind: str, **q) -> dict:
    """One Nasdaq quote call for SPY: the Worker first, then direct.

    kind is info, chart or historical; historical takes from= and to= (ISO dates).
    Raises with both reasons if neither way in answers.
    """
    why = []
    try:
        return _nasdaq_data(_worker_get({"src": "nasdaq", "kind": kind, **q}))
    except Exception as e:
        why.append(f"via worker {_reason(e)}")
    path = f"{kind}?assetclass=etf"
    if kind == "historical":
        path += f"&fromdate={q['from']}&todate={q['to']}&limit=400"
    try:
        req = urllib.request.Request(
            f"https://api.nasdaq.com/api/quote/SPY/{path}",
            headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return _nasdaq_data(json.load(r))
    except Exception as e:
        why.append(f"direct {_reason(e)}")
    raise RuntimeError(", ".join(why))


def _num(s) -> float:
    return float(str(s).replace("$", "").replace(",", "").replace("%", ""))


def _daily_stats(ser: list) -> dict:
    """spy, spy_as_of and the moves art.py and the curator read, from daily closes."""
    closes = [v for _, v in ser]
    out = {"spy": round(closes[-1], 2), "spy_as_of": ser[-1][0], "spy_series": ser}
    if len(closes) >= 2:
        out["spy_day_pct"] = round((closes[-1] / closes[-2] - 1) * 100, 2)
    if len(closes) >= 6:
        # Five sessions back is a week of trading, same convention as the 10Y.
        out["spy_wk_pct"] = round((closes[-1] / closes[-6] - 1) * 100, 2)
    rets = [(b / a - 1) * 100 for a, b in zip(closes[-31:], closes[-30:])]
    if len(rets) >= 8:
        out["spy_vol_pct"] = round(statistics.pstdev(rets), 3)
    return out


def _with_last(ser: list, when: str, px: float) -> None:
    """Fold a live quote into the daily closes: newer appends, same day replaces."""
    if when > ser[-1][0]:
        ser.append([when, px])
    elif when == ser[-1][0]:
        ser[-1][1] = px


def _spy_nasdaq() -> dict:
    today = _today()
    # 366 days rather than replace(year=-1), which raises on Feb 29.
    hist = _nasdaq("historical", **{"from": str(today - dt.timedelta(days=366)),
                                    "to": str(today)})
    rows = hist["tradesTable"]["rows"]
    ser = sorted(
        [dt.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(), _num(r["close"])]
        for r in rows)
    if not ser:
        return {}
    try:
        q = _nasdaq("info")["primaryData"]
        when = dt.datetime.strptime(q["lastTradeTimestamp"].split(" ET")[0].strip(),
                                    "%b %d, %Y %I:%M %p").date().isoformat()
        _with_last(ser, when, _num(q["lastSalePrice"]))
    except Exception as e:
        print(f"  spy live quote: unavailable ({type(e).__name__}) — using last close")
    return _daily_stats(ser)


def _et_wall_ms(stamp: str) -> int:
    """An ISO timestamp as ET wall-clock milliseconds read as UTC — the convention
    spy_intraday() stores and art.py reads."""
    t = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    wall = t.astimezone(NY).replace(tzinfo=dt.timezone.utc)
    return int(wall.timestamp() * 1000)


def _spy_tiingo() -> dict:
    today = _today()
    rows = _worker_get({"src": "tiingo", "kind": "daily",
                        "from": str(today - dt.timedelta(days=366))})
    ser = sorted([str(r["date"])[:10], float(r["close"])]
                 for r in rows or [] if r.get("date") and r.get("close") is not None)
    if not ser:
        return {}
    try:
        q = (_worker_get({"src": "tiingo", "kind": "quote"}) or [{}])[0]
        px = q.get("tngoLast") or q.get("last")
        if px is not None and q.get("timestamp"):
            when = dt.datetime.fromtimestamp(_et_wall_ms(q["timestamp"]) / 1000,
                                             dt.timezone.utc).date().isoformat()
            _with_last(ser, when, float(px))
    except Exception as e:
        print(f"  spy live quote (tiingo): unavailable ({type(e).__name__}) — using last close")
    return _daily_stats(ser)


def _spy_intraday_tiingo() -> dict:
    # Five days back so a weekend or holiday still finds the last session; only
    # that session's points are kept.
    rows = _worker_get({"src": "tiingo", "kind": "iex",
                        "from": str(_today() - dt.timedelta(days=5))})
    pts = sorted((_et_wall_ms(r["date"]), float(r["close"]))
                 for r in rows or [] if r.get("date") and r.get("close") is not None)
    if len(pts) < 2:
        return {}
    day = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()  # noqa: E731
    last = day(pts[-1][0])
    ser = [[x, round(y, 3)] for x, y in pts if day(x) == last]
    if len(ser) < 2:
        return {}
    return {"spy_intraday": ser, "spy_session": last.isoformat(), "spy": ser[-1][1]}


def _first(name: str, tries) -> dict:
    """The first source in `tries` that answers with data. Raises with every reason
    when none does, so main() logs one line that says why each way in failed."""
    why = []
    for src, fn in tries:
        try:
            data = fn()
        except Exception as e:
            why.append(f"{src}: {_reason(e)[:160]}")
            continue
        if data:
            print(f"  {name} via {src}")
            return data
        why.append(f"{src}: no data")
    raise RuntimeError("; ".join(why))


def spy() -> dict:
    """A year of SPY daily closes, with the live quote as the newest point.

    The historical table lags a session — at 5pm ET it still ends at yesterday's
    close — so the live quote is appended whenever it is newer. Both editions run
    while the market is open, so the last point is the price as the reader wakes
    up to it, not yesterday's.
    """
    return _first("spy", (("nasdaq", _spy_nasdaq), ("tiingo", _spy_tiingo)))


def spy_intraday() -> dict:
    """Today's SPY tape — or the last session's, on a weekend or holiday.

    Five-minute buckets are plenty for a 550px chart and keep the series file
    small. publish.py calls this again just before the send so each edition's
    chart runs as late as it can: the morning one is the pre-market and the open,
    the afternoon one most of the day.
    """
    return _first("spy intraday", (("nasdaq", _spy_intraday_nasdaq),
                                   ("tiingo", _spy_intraday_tiingo)))


def _spy_intraday_nasdaq() -> dict:
    """Nasdaq returns one point a minute from the 4:00am ET pre-market onward."""
    d = _nasdaq("chart")
    pts = [(int(p["x"]), float(p["y"])) for p in d.get("chart") or []
           if p.get("x") is not None and p.get("y") is not None]
    if len(pts) < 2:
        return {}
    # Points are stored as milliseconds of ET wall-clock time, read as if UTC, so
    # art.py can place 09:30 without a timezone library. Nasdaq's x has been seen
    # both ways, so check a point against its own label rather than trusting it.
    lab = (d["chart"][0].get("z") or {}).get("dateTime", "")
    naive = dt.datetime.fromtimestamp(pts[0][0] / 1000, dt.timezone.utc)
    shift = 0
    if lab and naive.strftime("%-I:%M %p") != lab.replace(" ET", ""):
        shift = int(naive.astimezone(NY).utcoffset().total_seconds() * 1000)
    buckets: dict[int, tuple[int, float]] = {}
    for x, y in pts:
        x += shift
        buckets[x // 300_000] = (x, y)           # last print in each 5 minutes
    ser = [[x, round(y, 3)] for x, y in sorted(buckets.values())]
    out = {"spy_intraday": ser,
           "spy_session": dt.datetime.fromtimestamp(ser[-1][0] / 1000,
                                                    dt.timezone.utc).date().isoformat()}
    out["spy"] = ser[-1][1]
    return out


def settle_spy(out: dict) -> None:
    """Derive the day's move once both SPY reads are in.

    The chart endpoint's own previousClose is not trustworthy — on 2026-09-22 it
    reported 761.69 against a real prior close of 773.50, which would have printed
    a flat day as +1.5%. The prior close comes from the daily history instead:
    the last close dated before the session being charted.

    spy_session always names the session spy and spy_day_pct describe: the tape's
    date, or without a tape the daily table's last date. On a weekend or a market
    holiday that is the last trading day, not today, and art.py labels it so.
    """
    daily = out.get("spy_series") or []
    sess = out.get("spy_session") or out.get("spy_as_of")
    if sess:
        out["spy_session"] = sess
        prior = [(d, v) for d, v in daily if d < sess]
        # Closes from state/spy.json can end sessions before this one; then the
        # last of them is not the prior close and the day's move would be wrong.
        cached = bool(out.get("spy_cache_as_of"))
        if prior and not (cached and _bdays(dt.date.fromisoformat(prior[-1][0]),
                                            dt.date.fromisoformat(sess)) > 1):
            out["spy_prev_close"] = prior[-1][1]
    if out.get("spy") is not None and out.get("spy_prev_close"):
        out["spy_day_pct"] = round((out["spy"] / out["spy_prev_close"] - 1) * 100, 2)


# The Worker serves exactly these five series (worker/src/index.js) and 404s the
# rest. Overridable so a fork or a local run can point at its own.
FRED_URL = (os.environ.get("COMPASS_FRED_URL")
            or "https://compass-trigger.hudson-394.workers.dev/fred")

FRED_SERIES = {
    "DRCRELEXFACBS": "cre_delinquency_pct",   # CRE loans past due at US banks; quarterly
    "UNRATE":        "unemployment_pct",
    "CPILFESL":      "cpi_core_yoy_pct",      # core CPI index, stated as a y/y rate
    "BAMLH0A0HYM2":  "hy_spread_pct",         # risk appetite, moves before CRE does
}


def _obs(pairs) -> list[tuple[str, float]]:
    """(date, value) pairs, newest first. FRED's "." for a missing value, and
    anything undated, is dropped."""
    out = {}
    for d, v in pairs:
        try:
            day, x = dt.date.fromisoformat(str(d)).isoformat(), float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            out[day] = x
    return sorted(out.items(), reverse=True)


def _fred_worker(series: str) -> list[tuple[str, float]]:
    # Not _get_plain: Cloudflare's bot filter turns away urllib's default
    # "Python-urllib" agent on workers.dev (the 2026-10-15 dry run fell through to
    # fredgraph.csv on every series), while a named agent passes.
    req = urllib.request.Request(f"{FRED_URL}?series={series}",
                                 headers={"User-Agent": "compass-newsletter/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    if body.get("series") != series:
        raise ValueError(f"Worker answered for {body.get('series')!r}")
    return _obs((o.get("date"), o.get("value")) for o in body.get("observations") or [])


def _fred_csv(series: str) -> list[tuple[str, float]]:
    body = _get_plain(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", 30)
    rows = list(csv.reader(io.StringIO(body.decode())))[1:]
    return _obs((r[0], r[1]) for r in rows if len(r) > 1)


def _why(e: Exception) -> str:
    return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__


def _reason(e: Exception) -> str:
    """_why, but keeping the message of an error this file raised itself."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return str(e) if isinstance(e, (RuntimeError, ValueError)) and str(e) else type(e).__name__


def fred(series: str) -> tuple[str, list, str]:
    """One FRED series, newest observation first. Never raises.

    The Worker first, with one retry; fredgraph.csv if it still fails (a Worker
    without its FRED key answers 503). Returns (series, observations, where they
    came from) — or (series, [], why not).
    """
    why = []
    for attempt in (1, 2):
        try:
            obs = _fred_worker(series)
            if obs:
                return series, obs, "worker"
            why.append("worker: no observations")
            break
        except Exception as e:
            why.append(f"worker {_why(e)}")
            if attempt == 1:
                time.sleep(2)
    try:
        obs = _fred_csv(series)
        if obs:
            return series, obs, "fredgraph.csv"
        why.append("csv: no observations")
    except Exception as e:
        why.append(f"csv {_why(e)}")
    return series, [], ", ".join(why)


def fred_entry(series: str, obs: list) -> dict | None:
    """market.json's entry for one series: {"value", "as_of"}, or None."""
    d, v = obs[0]
    if series == "CPILFESL":
        # An index level (~330) says nothing to a reader; its change on a year ago
        # is the inflation rate. Matched by date, not position: missing months are
        # dropped, so obs[12] is not always a year back. FRED's CPILFESL is
        # seasonally adjusted and BLS's headline 12-month change is not, so this can
        # differ from the published figure by a tenth.
        day = dt.date.fromisoformat(d)
        try:
            was = dict(obs).get(day.replace(year=day.year - 1).isoformat())
        except ValueError:                       # Feb 29; FRED dates months by the 1st
            was = None
        if not was:
            return None
        v = round((v / was - 1) * 100, 1)
    return {"value": v, "as_of": d}


def _merge(out: dict, new: dict, src: str, replaces: tuple = ()) -> None:
    """Fold one source into the snapshot, loudly if it clobbers another's key.

    This was a plain dict.update, and treasury() and freddie() both wrote a bare
    "as_of": the PMMS week replaced the Treasury date without a word, and the
    2026-10-14 PM brief dated the 10Y "Oct. 8" when the print was Oct. 13.
    """
    for k, v in new.items():
        if k in out and k not in replaces:
            print(f"::warning::market: {src} overwrote {k} "
                  f"({str(out[k])[:40]} -> {str(v)[:40]})")
        out[k] = v


def _bdays(since: dt.date, today: dt.date) -> int:
    """Weekdays after `since` up to and including `today`. Holidays count as
    business days here, which can only make the guard trip sooner."""
    n, d = 0, since
    while d < today:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n


def _fresh(name: str, data: dict, today: dt.date) -> dict:
    """Drop a source whose newest print is too old to state as current.

    The spec says never carry a stale figure forward, and with a date per source
    that is checkable here rather than left to the curator. Both limits leave room
    for holidays: Treasury prints every business day, the PMMS every Thursday.
    Dropped loudly, because the rate path is meant to be in every issue.
    """
    try:
        if name == "treasury":
            when = data["treasury_as_of"]
            age, limit, unit = _bdays(dt.date.fromisoformat(when), today), 5, "business days"
        elif name == "freddie":
            when = data["mortgage30_as_of"]
            age, limit, unit = (today - dt.date.fromisoformat(when)).days, 10, "days"
        else:
            return data
    except (KeyError, TypeError, ValueError):
        print(f"::warning::market: {name} is undated — dropped, not stated as current")
        return {}
    if age > limit:
        print(f"::warning::market: {name} print is {age} {unit} old (as of {when}) "
              "— dropped, not stated as current")
        return {}
    return data


def _bp(v, unit: str = "bp") -> str:
    return f"{v:+d}{unit}" if isinstance(v, int) else "n/a"


def _summary(name: str, d: dict) -> str:
    """One log line per source: the headline value and the date it is as of."""
    if name == "treasury":
        return (f"10Y {d.get('y10')}% ({_bp(d.get('y10_wk_bps'), 'bp/wk')}), "
                f"2Y {d.get('y2')}%, 30Y {d.get('y30')}%, "
                f"2s10s {_bp(d.get('curve_2s10s_bps'))}, "
                f"as of {d.get('treasury_as_of')} "
                f"({len(d.get('y10_series') or [])} daily prints)")
    if name == "freddie":
        return (f"30Y fixed {d.get('mortgage30')}% "
                f"({_bp(d.get('mortgage30_wk_bps'), 'bp/wk')}), "
                f"as of {d.get('mortgage30_as_of')}")
    if name == "spy":
        return (f"{d.get('spy')} as of {d.get('spy_as_of')} "
                f"({len(d.get('spy_series') or [])} daily closes)")
    if name == "spy intraday":
        ser = d.get("spy_intraday") or [[0, None]]
        hhmm = dt.datetime.fromtimestamp(ser[-1][0] / 1000, dt.timezone.utc)
        return (f"{d.get('spy')} at {hhmm:%H:%M} ET on the {d.get('spy_session')} "
                f"session ({len(ser)} points)")
    return "ok"


SOURCES = (
    ("treasury", treasury, ()),
    ("freddie", freddie, ()),
    ("spy", spy, ()),
    # The live tape's last print replaces the daily table's on purpose: it is newer.
    ("spy intraday", spy_intraday, ("spy",)),
)


# The last good run's completed SPY closes. Committed by the workflow's "Record
# section coverage" step (market.py --keep-spy) so that when every SPY source is
# down, the cover still charts SPY — dated in its caption — rather than the 10Y.
# Older than this many business days, it is not used: a chart that old is not the
# day's market, and the 10Y (which comes from Treasury, not Nasdaq) is.
SPY_CACHE = "state/spy.json"
SPY_CACHE_MAX_BDAYS = 5


def _spy_cache(today: dt.date) -> dict | None:
    try:
        with open(SPY_CACHE) as f:
            c = json.load(f)
        ser, as_of = c["spy_series"], c["as_of"]
        age = _bdays(dt.date.fromisoformat(as_of), today)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"  {SPY_CACHE}: unreadable ({type(e).__name__}) — not used")
        return None
    if age > SPY_CACHE_MAX_BDAYS or len(ser) < 20:
        print(f"  {SPY_CACHE}: as of {as_of}, {age} business days old — too old to chart")
        return None
    return {"spy_series": ser, "spy_cache_as_of": as_of}


def keep_spy(market_path: str = "market.json") -> int:
    """After a shipped edition: save this run's live SPY closes to state/spy.json.

    Only completed sessions are kept. Both editions run while the market is open,
    so a point dated today is a mid-session price, not a close, and would read as
    the prior close in tomorrow's day move. A run whose closes came from the cache
    itself writes nothing. Exits 0 either way: this must never fail the record step.
    """
    try:
        m = load_market(market_path)
    except Exception as e:
        print(f"keep-spy: no market data ({type(e).__name__}) — {SPY_CACHE} unchanged")
        return 0
    if m.get("spy_cache_as_of"):
        print(f"keep-spy: this run charted cached closes — {SPY_CACHE} unchanged")
        return 0
    today = _today().isoformat()
    ser = [p for p in m.get("spy_series") or [] if p[0] < today]
    if len(ser) < 20:
        print(f"keep-spy: no live SPY closes this run — {SPY_CACHE} unchanged")
        return 0
    try:
        with open(SPY_CACHE) as f:
            old = json.load(f).get("as_of", "")
    except Exception:
        old = ""
    if old >= ser[-1][0]:
        print(f"keep-spy: {SPY_CACHE} already as of {old}")
        return 0
    os.makedirs(os.path.dirname(SPY_CACHE), exist_ok=True)
    with open(SPY_CACHE, "w") as f:
        json.dump({"as_of": ser[-1][0], "spy_series": ser[-260:]}, f,
                  separators=(",", ":"))
        f.write("\n")
    print(f"keep-spy: {SPY_CACHE} now as of {ser[-1][0]} ({len(ser[-260:])} closes)")
    return 0


def _alert(subject: str, body: str) -> None:
    """The run's own alert channel (scripts/alert.py): an annotation and a step
    summary section. The newsletter is the only email this pipeline sends."""
    try:
        import alert
        alert.alert(subject, body)
    except Exception as e:
        print(f"::warning::{subject} (alert.py unavailable: {type(e).__name__})")


def main() -> None:
    today = _today()
    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    failed = {}
    for name, fn, replaces in SOURCES:
        try:
            data = fn()
        except Exception as e:
            failed[name] = _reason(e)[:300].replace("\n", " ")
            print(f"::warning::market: {name} unavailable ({failed[name][:200]})")
            continue
        if not data:
            failed[name] = "returned nothing"
            print(f"::warning::market: {name} returned nothing")
            continue
        data = _fresh(name, data, today)
        if data:
            _merge(out, data, name, replaces)
            print(f"  {name}: {_summary(name, data)}")
        else:
            failed[name] = "stale, dropped"

    # A week of editions charted the 10Y in place of SPY before anyone noticed:
    # the only trace was a ::warning:: in a step nobody opens. Say it in the run's
    # summary, and chart the last good closes rather than nothing.
    live = "spy_series" in out or "spy_intraday" in out
    chart = "live SPY"
    if "spy_series" not in out:
        cached = _spy_cache(today)
        if cached:
            out.update(cached)
            chart = f"SPY closes cached to {cached['spy_cache_as_of']}"
            print(f"  spy: charting {len(cached['spy_series'])} cached closes "
                  f"to {cached['spy_cache_as_of']} ({SPY_CACHE})")
        elif not live:
            chart = "the US 10Y (no SPY data at all)"
    if failed:
        lines = [f"{k}: {v}" for k, v in failed.items()]
        spy_down = not live
        subject = (f"COMPASS: SPY unavailable — the cover charts {chart}" if spy_down
                   else f"COMPASS: market source missing ({', '.join(failed)})")
        _alert(subject, "\n".join(lines + [
            "", "The edition still ships; these numbers are absent from it.",
            "SPY sources and their fixes: README.md, 'The cover charts the 10Y'."]))

    settle_spy(out)
    if out.get("spy_day_pct") is not None:
        print(f"  spy day: {out['spy_day_pct']:+.2f}% on the {out.get('spy_session')} "
              f"session, against a prior close of {out.get('spy_prev_close')}")

    missing = []
    with futures.ThreadPoolExecutor(max_workers=len(FRED_SERIES)) as ex:
        for sid, obs, how in ex.map(fred, FRED_SERIES):
            entry = None
            if obs:
                try:
                    entry = fred_entry(sid, obs)
                    if entry is None:
                        how = "no observation a year back for y/y"
                except Exception as e:
                    how = f"{type(e).__name__} deriving the value"
            if entry is None:
                missing.append(f"{sid} ({how})")
                print(f"  FRED {sid}: unavailable ({how})")
                continue
            _merge(out, {FRED_SERIES[sid]: entry}, f"FRED {sid}")
            print(f"  FRED {sid}: {FRED_SERIES[sid]} {entry['value']} "
                  f"as of {entry['as_of']} (via {how})")
    if missing:
        print(f"::warning::FRED: {len(missing)}/{len(FRED_SERIES)} series missing — "
              + "; ".join(missing))

    series = {k: out.pop(k) for k in SERIES_KEYS if k in out}
    # Scalars first and the FRED objects after them: the curator reads top down.
    snap = {k: v for k, v in out.items() if not isinstance(v, dict)}
    snap.update((k, v) for k, v in out.items() if isinstance(v, dict))
    have = [k for k in ("y10", "mortgage30", "spy", *FRED_SERIES.values()) if k in snap]
    print(f"\nmarket.json: {len(snap) - 1} fields, key series present: {have or 'NONE'}")
    print("market_series.json: "
          + (", ".join(f"{k} ({len(v)} points)" if isinstance(v, list) else f"{k} {v}"
                       for k, v in series.items()) or "no charts"))
    with open("market.json", "w") as f:
        json.dump(snap, f, indent=1)
    # Written even when empty, so load_market() can never merge an earlier run's
    # charts into today's numbers.
    with open("market_series.json", "w") as f:
        json.dump(series, f, separators=(",", ":"))


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--keep-spy"]:
        sys.exit(keep_spy())
    main()
