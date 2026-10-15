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
    2026-09-22.
  - Treasury.gov daily yield curve XML. The 10Y is the number that sets what the
    reader's clients' buyers can borrow at, so it leads. Verified 2026-08-27.
  - Freddie Mac PMMS weekly mortgage survey, as the housing-side read.
  - FRED for the slower series: CRE loan delinquency (quarterly), unemployment,
    core CPI (CPILFESL, reported as a year-over-year rate) and the high-yield
    spread. Read through the compass-trigger Worker's /fred route, which keeps the
    FRED API key as a Worker secret, with fredgraph.csv as the fallback. Through
    2026-10-14 this read fredgraph.csv with a browser User-Agent and every logged
    run timed out: FRED answers Python's default User-Agent at once and stalls
    ~40s on a browser or custom one (runner probe, 2026-10-14). FRED dates an
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
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}
CT = zoneinfo.ZoneInfo("America/Chicago")

# Chart data. Written to market_series.json, never to market.json.
SERIES_KEYS = ("y10_series", "spy_series", "spy_intraday")


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


def _nasdaq(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.nasdaq.com/api/quote/SPY/{path}",
        headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)["data"]


def _num(s) -> float:
    return float(str(s).replace("$", "").replace(",", "").replace("%", ""))


def spy() -> dict:
    """A year of SPY daily closes, with the live quote as the newest point.

    The historical table lags a session — at 5pm ET it still ends at yesterday's
    close — so the live quote is appended whenever it is newer. Both editions run
    while the market is open, so the last point is the price as the reader wakes
    up to it, not yesterday's.
    """
    today = _today()
    # 366 days rather than replace(year=-1), which raises on Feb 29.
    hist = _nasdaq(f"historical?assetclass=etf&fromdate={today - dt.timedelta(days=366)}"
                   f"&todate={today}&limit=400")
    rows = hist["tradesTable"]["rows"]
    ser = sorted(
        [dt.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(), _num(r["close"])]
        for r in rows)
    if not ser:
        return {}
    try:
        q = _nasdaq("info?assetclass=etf")["primaryData"]
        when = dt.datetime.strptime(q["lastTradeTimestamp"].split(" ET")[0].strip(),
                                    "%b %d, %Y %I:%M %p").date().isoformat()
        px = _num(q["lastSalePrice"])
        if when > ser[-1][0]:
            ser.append([when, px])
        elif when == ser[-1][0]:
            ser[-1][1] = px
    except Exception as e:
        print(f"  spy live quote: unavailable ({type(e).__name__}) — using last close")

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


NY = zoneinfo.ZoneInfo("America/New_York")


def spy_intraday() -> dict:
    """Today's SPY tape — or the last session's, on a weekend or holiday.

    Nasdaq returns one point a minute from the 4:00am ET pre-market onward. Five-
    minute buckets are plenty for a 550px chart and keep the series file small.
    publish.py calls this again just before the send so each edition's chart runs
    as late as it can: the morning one is the pre-market and the open, the
    afternoon one most of the day.
    """
    d = _nasdaq("chart?assetclass=etf")
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
        prior = [v for d, v in daily if d < sess]
        if prior:
            out["spy_prev_close"] = prior[-1]
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
    body = json.loads(_get_plain(f"{FRED_URL}?series={series}", 30))
    if body.get("series") != series:
        raise ValueError(f"Worker answered for {body.get('series')!r}")
    return _obs((o.get("date"), o.get("value")) for o in body.get("observations") or [])


def _fred_csv(series: str) -> list[tuple[str, float]]:
    body = _get_plain(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", 30)
    rows = list(csv.reader(io.StringIO(body.decode())))[1:]
    return _obs((r[0], r[1]) for r in rows if len(r) > 1)


def _why(e: Exception) -> str:
    return f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__


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


def main() -> None:
    today = _today()
    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    for name, fn, replaces in SOURCES:
        try:
            data = fn()
        except Exception as e:
            print(f"::warning::market: {name} unavailable ({type(e).__name__}: "
                  f"{str(e)[:120]})".replace("\n", " "))
            continue
        if not data:
            print(f"::warning::market: {name} returned nothing")
            continue
        data = _fresh(name, data, today)
        if data:
            _merge(out, data, name, replaces)
            print(f"  {name}: {_summary(name, data)}")

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
          + (", ".join(f"{k} ({len(v)} points)" for k, v in series.items()) or "no charts"))
    with open("market.json", "w") as f:
        json.dump(snap, f, indent=1)
    # Written even when empty, so load_market() can never merge an earlier run's
    # charts into today's numbers.
    with open("market_series.json", "w") as f:
        json.dump(series, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
