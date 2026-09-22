#!/usr/bin/env python3
"""Stage 1b of 3: hard numbers for the CRE snapshot. Deterministic, no model.

The reader asked for CRE framed as market health and direction rather than as a
feed of individual acquisitions. Headlines alone cannot do that: on a quiet day
the CRE wires carry three shopping-center trades and nothing about the state of
the market. Numbers are always available and always say something.

Everything here is free and keyless:
  - Nasdaq's public quote API for SPY: today's minute-by-minute tape, which draws
    the cover chart, plus a year of daily closes for the weekly move and realised
    vol. Yahoo's chart API was the obvious choice and
    answers 429 to scripted clients; Stooq now serves a JS challenge. Verified
    2026-09-22.
  - Treasury.gov daily yield curve XML. The 10Y is the number that sets what the
    reader's clients' buyers can borrow at, so it leads. Verified 2026-08-27.
  - Freddie Mac PMMS weekly mortgage survey, as the housing-side read.
  - FRED CSV for the slower series (CRE loan delinquency, unemployment, core
    inflation). FRED was unreachable from the authoring sandbox, so every call is
    individually guarded: a blocked or slow FRED costs those lines, not the run.

Never raises. A snapshot with three of five lines is fine; a snapshot that takes
the edition down with it is not.

Writes market.json for the curation stage.
"""

import concurrent.futures as futures
import zoneinfo
import csv
import datetime as dt
import io
import json
import statistics
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom",
      "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def treasury() -> dict:
    """Daily constant-maturity yields. Returns latest plus a week-ago comparison."""
    year = dt.date.today().year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/"
           "interest-rates/pages/xml?data=daily_treasury_yield_curve"
           f"&field_tdr_date_value={year}")
    ents = ET.fromstring(_get(url, 30)).findall("a:entry", NS)
    if not ents:
        return {}

    def vals(e):
        p = e.find(".//m:properties", NS)
        return {c.tag.split("}")[1]: c.text for c in p}

    cur = vals(ents[-1])
    # Five business days back is a week of trading; fall back to whatever exists.
    prev = vals(ents[-6]) if len(ents) >= 6 else vals(ents[0])

    def f(d, k):
        try:
            return float(d[k])
        except (KeyError, TypeError, ValueError):
            return None

    out = {"as_of": (cur.get("NEW_DATE") or "")[:10]}
    for key, field in (("y2", "BC_2YEAR"), ("y10", "BC_10YEAR"), ("y30", "BC_30YEAR")):
        now, was = f(cur, field), f(prev, field)
        if now is None:
            continue
        out[key] = now
        if was is not None:
            out[key + "_wk_bps"] = round((now - was) * 100)
    if out.get("y10") is not None and out.get("y2") is not None:
        out["curve_2s10s_bps"] = round((out["y10"] - out["y2"]) * 100)

    # The whole year of daily closes is already parsed and in hand. art.py draws
    # the cover chart straight from it, so keep it rather than fetching it twice.
    hist = [[(v.get("NEW_DATE") or "")[:10], f(v, "BC_10YEAR")]
            for v in (vals(e) for e in ents)]
    hist = [[d, y] for d, y in hist if y is not None]
    if hist:
        out["y10_series"] = hist
    return out


def freddie() -> dict:
    """30-year fixed mortgage, weekly. Housing-side read on the same rate path."""
    rows = list(csv.reader(io.StringIO(
        _get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv", 25).decode())))
    vals = []
    for r in rows:
        if len(r) < 2:
            continue
        try:
            vals.append((r[0], float(r[1])))
        except ValueError:
            continue
    if not vals:
        return {}
    out = {"mortgage30": vals[-1][1], "as_of": vals[-1][0]}
    if len(vals) >= 2:
        out["mortgage30_wk_bps"] = round((vals[-1][1] - vals[-2][1]) * 100)
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
    today = dt.date.today()
    hist = _nasdaq(f"historical?assetclass=etf&fromdate={today.replace(year=today.year - 1)}"
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
    minute buckets are plenty for a 550px chart and keep market.json small, which
    matters because the curator reads the whole file. publish.py calls this again
    just before the send so each edition's chart runs as late as it can: the
    morning one is the pre-market and the open, the afternoon one most of the day.
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
    """
    daily = out.get("spy_series") or []
    sess = out.get("spy_session")
    if sess:
        prior = [v for d, v in daily if d < sess]
        if prior:
            out["spy_prev_close"] = prior[-1]
    if out.get("spy") is not None and out.get("spy_prev_close"):
        out["spy_day_pct"] = round((out["spy"] / out["spy_prev_close"] - 1) * 100, 2)


def fred(series: str) -> tuple[str, dict]:
    """One FRED series, latest observation plus the prior one."""
    try:
        body = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", 15)
        rows = [r for r in csv.reader(io.StringIO(body.decode()))][1:]
        obs = [(r[0], float(r[1])) for r in rows
               if len(r) > 1 and r[1] not in (".", "")]
        if not obs:
            return series, {}
        out = {"value": obs[-1][1], "as_of": obs[-1][0]}
        if len(obs) >= 2:
            out["prev"] = obs[-2][1]
            out["prev_as_of"] = obs[-2][0]
        return series, out
    except Exception as e:
        print(f"  FRED {series}: unavailable ({type(e).__name__})")
        return series, {}


FRED_SERIES = {
    "DRCRELEXFACBS": "cre_delinquency_pct",   # CRE loans past due at US banks
    "UNRATE":        "unemployment_pct",
    "CPIAUCSL":      "cpi_index",
    "BAMLH0A0HYM2":  "hy_spread_pct",         # risk appetite, moves before CRE does
}


def main() -> None:
    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    for name, fn in (("treasury", treasury), ("freddie", freddie), ("spy", spy),
                     ("spy intraday", spy_intraday)):
        try:
            out.update(fn())
            print(f"  {name}: ok")
        except Exception as e:
            print(f"  {name}: unavailable ({type(e).__name__})")

    with futures.ThreadPoolExecutor(max_workers=len(FRED_SERIES)) as ex:
        for sid, data in ex.map(fred, FRED_SERIES):
            if data:
                out[FRED_SERIES[sid]] = data

    settle_spy(out)
    have = [k for k in ("y10", "mortgage30", "spy", "cre_delinquency_pct") if k in out]
    print(f"\nmarket.json: {len(out) - 1} fields, key series present: {have or 'NONE'}")
    with open("market.json", "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
