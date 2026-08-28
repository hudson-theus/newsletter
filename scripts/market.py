#!/usr/bin/env python3
"""Stage 1b of 3: hard numbers for the CRE snapshot. Deterministic, no model.

The reader asked for CRE framed as market health and direction rather than as a
feed of individual acquisitions. Headlines alone cannot do that: on a quiet day
the CRE wires carry three shopping-center trades and nothing about the state of
the market. Numbers are always available and always say something.

Everything here is free and keyless:
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
import csv
import datetime as dt
import io
import json
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

    for name, fn in (("treasury", treasury), ("freddie", freddie)):
        try:
            out.update(fn())
            print(f"  {name}: ok")
        except Exception as e:
            print(f"  {name}: unavailable ({type(e).__name__})")

    with futures.ThreadPoolExecutor(max_workers=len(FRED_SERIES)) as ex:
        for sid, data in ex.map(fred, FRED_SERIES):
            if data:
                out[FRED_SERIES[sid]] = data

    have = [k for k in ("y10", "mortgage30", "cre_delinquency_pct") if k in out]
    print(f"\nmarket.json: {len(out) - 1} fields, key series present: {have or 'NONE'}")
    with open("market.json", "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
