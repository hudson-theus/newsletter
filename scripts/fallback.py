#!/usr/bin/env python3
"""Last-resort issue builder. Deterministic, no model.

The curation step is the only part of this pipeline that can fail for reasons
outside the repo's control. On 2026-08-27 it did: the afternoon edition died in
423ms with an auth rejection, and because nothing stood behind it, that edition
simply never existed. One transient blip, one missing brief.

This is what stands behind it. Given the same candidates.json the curator would
have read, it writes a plain issue.json — real headlines, real links, grouped into
the standing sections, no commentary and no ranking beyond recency. It is visibly
plainer than a curated issue and says so in its front matter, which is the point:
the reader can tell at a glance that the model stage did not run, and still knows
what happened today.

The layout follows the curated one, labels included (prompts/curate.md). The
2026-10-09 fallback had one unlabelled DALLAS / TEXAS block and a SPORTS block of
the five newest league-wide items, so it shipped no food, going-out or UVA item
at all, ran other teams' news under SPORTS, and stated no rate path.

A plain brief beats no brief. Never let this file raise.
"""

import datetime as dt
import html
import json
import re
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def _host(c: dict) -> str:
    return (urlsplit(c.get("url") or "").hostname or "").lower()


# The sports feeds are league-wide (Yahoo, CBS: all of the NFL, NBA, college),
# so a team block takes only items that are about the team: the team's SB Nation
# site, or a title that names it. "Cowboys" alone would also catch Oklahoma State
# and Wyoming in the college feeds. A team with nothing today gets no block —
# never generic items under its name.
_COWBOYS = re.compile(r"(?<!Oklahoma State )(?<!OSU )(?<!Wyoming )(?<!McNeese )"
                      r"\bCowboys\b", re.I)
_MAVS = re.compile(r"\bMav(ericks|s)\b", re.I)   # same match as sections.py
# The uva feeds are the Cavalier Daily, mostly campus news (and its humor
# section), plus Streaking the Lawn. VIRGINIA is the sports block, so a campus
# story only qualifies when its headline or slug is plainly about athletics.
_ATHLETICS = re.compile(
    r"\b(football|basketball|baseball|softball|soccer|lacrosse|volleyball|"
    r"wrestling|tennis|golf|rowing|swimming|field hockey|track and field|"
    r"cross country|athletics|Cavaliers|Wahoos|ACC|NCAA|head coach|quarterback|"
    r"bowl game|postseason|transfer portal)\b", re.I)


def cowboys(c: dict) -> bool:
    return _host(c).endswith("bloggingtheboys.com") or bool(_COWBOYS.search(c.get("title", "")))


def mavericks(c: dict) -> bool:
    return _host(c).endswith("mavsmoneyball.com") or bool(_MAVS.search(c.get("title", "")))


def uva_athletics(c: dict) -> bool:
    if _host(c).endswith("streakingthelawn.com"):
        return True
    slug = urlsplit(c.get("url") or "").path.replace("-", " ")
    return bool(_ATHLETICS.search(c.get("title", "")) or _ATHLETICS.search(slug))


# (section title, block label or None, candidate section keys, max items, filter)
# Titles and labels are the canonical ones sections.py credits (C12).
LAYOUT = [
    ("THE ECONOMY",        None,                       ["economy"],   6, None),
    ("CRE SNAPSHOT",       None,                       ["cre"],       2, None),
    ("CRE DESK",           "RETAIL",                   ["retail"],    2, None),
    ("DEAL FLOW",          "VC",                       ["vc"],       12, None),
    ("DEAL FLOW",          "IPOS &amp; LISTINGS",      ["ipo"],       6, None),
    ("DEAL FLOW",          "M&amp;A / STRATEGIC",      ["deals"],     8, None),
    ("AI &amp; ENTERPRISE", None,                      ["ai"],        6, None),
    ("UNITED STATES",      None,                       ["us"],        6, None),
    ("THE WORLD",          None,                       ["world"],     7, None),
    ("DALLAS / TEXAS",     "NEWS",                     ["dallas"],    5, None),
    ("DALLAS / TEXAS",     "FOOD &amp; DRINK",         ["food"],      2, None),
    ("DALLAS / TEXAS",     "GOING OUT",                ["goingout"],  2, None),
    ("SPORTS",             "COWBOYS &amp; NFL",        ["sports"],    2, cowboys),
    ("SPORTS",             "MAVERICKS",                ["sports"],    2, mavericks),
    ("SPORTS",             "VIRGINIA",                 ["uva"],       2, uva_athletics),
    ("WORTH READING",      None,                       ["read"],      4, None),
]

# Only well-formed references are decoded. Feeds that double-encode leave
# "&#8217;" in a title, which a plain escape would print literally; a full
# html.unescape would also turn the "&not" in "Section 8 &not enough" into a
# negation sign.
_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def headline(title: str) -> str:
    """A candidate title (plain text) as safe HTML. Unescaped, a title like
    "Yields <hit 5% as Fed holds" opened a tag that swallowed the source link."""
    return html.escape(_ENTITY.sub(lambda m: html.unescape(m.group(0)), title),
                       quote=False)


# --- the numbers line ---------------------------------------------------------
# The spec states the rate path every issue, and market.json is collected without
# the model, so the fallback can state it too. Each figure carries its own
# source's date and is left out when that date is missing or stale: the
# 2026-10-14 email printed a 10Y dated Oct. 8 as if it were current.

def _date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _fresh(d: dt.date | None, today: dt.date, days: int) -> bool:
    return d is not None and 0 <= (today - d).days <= days


def _day(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


def _bps(v) -> str:
    return f"{int(round(float(v))):+d}bp"


def _fred(m: dict, key: str, today: dt.date, days: int):
    o = m.get(key)
    if not isinstance(o, dict) or o.get("value") is None:
        return None, None
    d = _date(o.get("as_of"))
    return (float(o["value"]), d) if _fresh(d, today, days) else (None, None)


def _treasury(m: dict, today: dt.date) -> str | None:
    t = _date(m.get("treasury_as_of"))
    if m.get("y10") is None or not _fresh(t, today, 7):
        return None
    wk = f"{_bps(m['y10_wk_bps'])} wk; " if m.get("y10_wk_bps") is not None else ""
    s = f"10Y {float(m['y10']):.2f}% ({wk}Treasury, {_day(t)})"
    if m.get("curve_2s10s_bps") is not None:
        s += f", 2s10s {_bps(m['curve_2s10s_bps'])}"
    return s


def _mortgage(m: dict, today: dt.date) -> str | None:
    d = _date(m.get("mortgage30_as_of"))
    if m.get("mortgage30") is None or not _fresh(d, today, 14):
        return None
    wk = f"{_bps(m['mortgage30_wk_bps'])} wk; " if m.get("mortgage30_wk_bps") is not None else ""
    return f"30Y fixed {float(m['mortgage30']):.2f}% ({wk}Freddie Mac, {_day(d)})"


# Monthly series are dated the first of the month they describe and arrive five
# to eight weeks later; the quarterly CRE series about two months after the
# quarter ends.
def _cpi(m, today):
    v, d = _fred(m, "cpi_core_yoy_pct", today, 80)
    return None if v is None else f"core CPI {v:.1f}% y/y ({d:%b} data)"


def _jobs(m, today):
    v, d = _fred(m, "unemployment_pct", today, 80)
    return None if v is None else f"unemployment {v:.1f}% ({d:%b} data)"


def _hy(m, today):
    v, d = _fred(m, "hy_spread_pct", today, 7)
    return None if v is None else f"high-yield spread {v:.2f}% ({_day(d)})"


def _cre(m, today):
    v, d = _fred(m, "cre_delinquency_pct", today, 250)
    return None if v is None else (f"CRE loans past due at US banks {v:.2f}% "
                                   f"(Q{(d.month - 1) // 3 + 1} {d.year}, FRED)")


LINES = {"THE ECONOMY": [_treasury, _mortgage, _cpi, _jobs, _hy],
         "CRE SNAPSHOT": [_cre]}


def numbers(path: str, today: dt.date) -> dict[str, str]:
    """{section title: numbers-line text}, from whichever keys market.json has.
    Each figure is guarded on its own, so one malformed key costs that figure."""
    try:
        with open(path) as f:
            m = json.load(f)
        if not isinstance(m, dict):
            raise ValueError(f"top level is {type(m).__name__}")
    except Exception as e:
        print(f"no numbers line ({type(e).__name__}: {e})")
        return {}
    out = {}
    for title, parts in LINES.items():
        got = []
        for fn in parts:
            try:
                s = fn(m, today)
            except Exception as e:
                print(f"numbers line: {fn.__name__[1:]} skipped ({type(e).__name__}: {e})")
                continue
            if s:
                got.append(s)
        if got:
            out[title] = " / ".join(got)
    return out


def main() -> None:
    cands = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "candidates.json"))
    market = sys.argv[2] if len(sys.argv) > 2 else "market.json"
    pool: dict[str, list] = {}
    for i in cands.get("items", []):
        # A satire item never counts as news for any section (curate.md), and
        # nothing here could add the context that would make it fair to run.
        if (isinstance(i, dict) and isinstance(i.get("title"), str)
                and isinstance(i.get("url"), str) and not i.get("satire")):
            pool.setdefault(i.get("section", ""), []).append(i)

    notes = numbers(market, dt.datetime.now(CT).date())
    sections, used = [], set()
    for title, label, keys, cap, keep in LAYOUT:
        items = []
        for k in keys:
            for c in pool.get(k, []):
                if c["url"] in used or len(items) >= cap or (keep and not keep(c)):
                    continue
                used.add(c["url"])
                items.append({"text": headline(c["title"]), "src": c.get("src"),
                              "url": c["url"]})
        if title in notes:
            # The section's first block carries it, as the curator's would.
            items.insert(0, {"text": notes.pop(title), "note": True})
        if not items:
            continue
        existing = next((s for s in sections if s["title"] == title), None)
        if existing is None:
            sections.append({"title": title, "blocks": [{"label": label,
                                                         "items": items}]})
        else:
            existing["blocks"].append({"label": label, "items": items})

    total = sum(len(b["items"]) for s in sections for b in s["blocks"])
    issue = {
        # sections.py --record skips an issue carrying this, so an unedited
        # edition neither credits nor penalises the drought tracker.
        "_fallback": True,
        # No greeting here: publish.py prepends it, so this line stays a
        # sentence and the reader is addressed by name even on a fallback issue.
        "front_matter": ("The curation step did not complete, so this is an "
                         "unedited edition: real headlines from today's sources, "
                         "grouped but not ranked or written up."),
        "sections": [{"title": "FRONT MATTER", "blocks": []}] + sections,
    }
    with open("issue.json", "w") as f:
        json.dump(issue, f, indent=1)
    print(f"fallback issue written — {total} items across {len(sections)} sections")


if __name__ == "__main__":
    main()
