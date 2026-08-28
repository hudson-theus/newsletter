#!/usr/bin/env python3
"""Stage 2b of 3: the motion. Deterministic, no model.

This brief is read in Gmail, and Gmail strips @keyframes, transition and
transform, and blocks SVG outright. Animated GIF is the only motion channel that
survives the trip, so everything in COMPASS that moves is rendered here, at build
time, from the day's real numbers.

Two rules govern every asset:

  1. Frame 1 is a complete still. It is what Gmail paints while the rest of the
     file streams, and the only frame a client that will not animate ever shows.
     Nothing here builds up from an empty canvas; motion is a light sweep over a
     composition that is already finished on frame 1.
  2. Never raise. A failed render costs its own image and nothing else — the
     caller drops that asset and ships the issue around it. Same contract as
     fallback.py: a plain brief beats no brief.

The cover is not decoration. Its backdrop is bound to the day's numbers — the
2s10s slope sets the angle of the field, realised vol sets its density, and the
week's move in the 10Y warms or cools the accent. Two issues never look alike,
and the difference is the market.
"""

import io
import math
import statistics

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                                            # pragma: no cover
    Image = None

SCALE = 2                       # render at 2x, present at 1x, for retina mail
COVER = (620, 300)
BUDGET = 3_000_000              # total bytes of imagery per issue

TH = {
    "am": dict(accent=(238, 255, 140), label="MORNING EDITION", greet="Good morning"),
    "pm": dict(accent=(255, 106, 0),   label="AFTERNOON EDITION", greet="Good afternoon"),
}
INK, BG, DIM, HAIR = (255, 255, 255), (17, 17, 17), (122, 122, 122), (38, 38, 38)

_FONTS = [
    ("/System/Library/Fonts/Menlo.ttc", 0, 1),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", None, None),
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", None, None),
]
_BOLD = {
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf":
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf":
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
}
_cache: dict = {}


def font(size: int, bold: bool = False):
    """First mono that exists. Menlo locally, DejaVu on the runner."""
    key = (size, bold)
    if key in _cache:
        return _cache[key]
    for path, reg, bld in _FONTS:
        try:
            if reg is not None:                              # .ttc collection
                f = ImageFont.truetype(path, size, index=(bld if bold else reg))
            else:
                f = ImageFont.truetype(_BOLD[path] if bold else path, size)
            _cache[key] = f
            return f
        except Exception:
            continue
    f = ImageFont.load_default(size)
    _cache[key] = f
    return f


def _mix(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _encode(frames, ms) -> bytes:
    """One shared palette across frames — per-frame adaptive palettes flicker."""
    ref = frames[0].quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    seq = [f.quantize(palette=ref, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    seq[0].save(buf, format="GIF", save_all=True, append_images=seq[1:],
                duration=ms, loop=0, optimize=True, disposal=1)
    return buf.getvalue()


def _weather(market: dict) -> dict:
    """Read the day's numbers into drawing parameters. Missing data is flat."""
    ser = [v for _, v in market.get("y10_series") or []]
    curve = market.get("curve_2s10s_bps")
    move = market.get("y10_wk_bps")
    vol = statistics.pstdev(ser[-30:]) if len(ser) >= 8 else 0.06
    return {
        "series": ser,
        # steeper curve tilts the field up; inversion tilts it down
        "angle": math.radians(max(-9.0, min(23.0, ((curve if curve is not None else 40) + 50) / 200 * 32 - 9))),
        # more realised vol packs the hairlines tighter
        "gap": int(max(11, min(27, 27 - vol * 170))),
        # rates up runs the accent warm, rates down runs it cool
        "warm": max(-1.0, min(1.0, (move or 0) / 22.0)),
    }


def _field(d, w, h, wx, accent, phase):
    """The data-bound backdrop: a drifting hairline field."""
    gap, ang = wx["gap"] * SCALE, wx["angle"]
    dx = math.tan(ang) * h
    span = int(w + abs(dx)) + gap * 2
    drift = (phase * gap * 2) % (gap * 2)
    x = -abs(dx) - gap + drift
    i = 0
    while x < span:
        bright = i % 7 == 0
        col = _mix(HAIR, accent, 0.30) if bright else HAIR
        d.line([(x, h), (x + dx, 0)], fill=col, width=1)
        x += gap
        i += 1


def cover(edition: str, market: dict, date: str, stamp: str,
          issue_no: int, name: str) -> bytes | None:
    """The masthead. Complete on frame 1; a light sweep re-draws the curve."""
    t = TH[edition]
    wx = _weather(market)
    accent = _mix(t["accent"], (255, 176, 64) if wx["warm"] > 0 else (150, 240, 220),
                  min(0.34, abs(wx["warm"]) * 0.34))
    W, H, N = COVER[0] * SCALE, COVER[1] * SCALE, 26
    s = SCALE
    ser = wx["series"] or []
    lo, hi = (min(ser), max(ser)) if len(ser) > 1 else (0.0, 1.0)
    if hi - lo < 1e-6:
        hi = lo + 1.0
    gx, gy, gw, gh = 34 * s, 128 * s, W - 68 * s, 96 * s

    def pt(i, v):
        return (gx + gw * i / max(1, len(ser) - 1), gy + gh - (v - lo) / (hi - lo) * gh)

    frames = []
    for f in range(N):
        ph = f / N
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        _field(d, W, H, wx, accent, ph)

        # wordmark — present on every frame, brightened as the sweep crosses it
        fz = 46 * s
        fo = font(fz, True)
        adv = fz * 0.72
        sweep = ph * (W + 260 * s) - 130 * s
        for i, ch in enumerate("COMPASS"):
            cx = 34 * s + i * adv
            near = max(0.0, 1.0 - abs(cx + adv / 2 - sweep) / (150 * s))
            d.text((cx, 34 * s), ch, font=fo, fill=_mix(INK, accent, near * 0.85))

        d.rectangle([34 * s, 96 * s, W - 34 * s, 101 * s], fill=_mix(accent, BG, 0.30))
        d.rectangle([34 * s, 96 * s, min(W - 34 * s, max(34 * s, sweep)), 101 * s],
                    fill=accent)

        if len(ser) > 1:
            wk = ser[-6] if len(ser) >= 6 else ser[0]
            yw = pt(0, wk)[1]
            d.line([(gx, yw), (gx + gw, yw)], fill=(52, 52, 52), width=1)
            pts = [pt(i, v) for i, v in enumerate(ser)]
            d.line(pts, fill=_mix(accent, BG, 0.28), width=2 * s, joint="curve")
            lit = [p for p in pts if p[0] <= sweep]
            if len(lit) > 1:
                d.line(lit, fill=accent, width=2 * s, joint="curve")
            hx, hy = pts[-1]
            r = (3.4 + 1.5 * math.sin(ph * math.tau)) * s
            d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=INK)

        # standing rail — the numbers, the name, the issue
        d.text((34 * s, 232 * s), "US 10Y", font=font(11 * s, True), fill=DIM)
        cur = market.get("y10")
        d.text((34 * s, 248 * s), f"{cur:.2f}%" if cur is not None else "—",
               font=font(30 * s, True), fill=INK)
        bps = market.get("y10_wk_bps")
        if bps is not None:
            d.text((132 * s, 258 * s), f"{bps:+d}bp / wk", font=font(12 * s), fill=accent)
        rail = f"{t['label']}  ·  NO {issue_no}"
        fr = font(11 * s, True)
        d.text((W - 34 * s - d.textlength(rail, font=fr), 232 * s), rail, font=fr, fill=accent)
        who = f"PREPARED FOR {name.upper()}"
        fw = font(11 * s, True)
        d.text((W - 34 * s - d.textlength(who, font=fw), 252 * s), who, font=fw, fill=DIM)
        frames.append(im)
    return _encode(frames, 70)


def ticker(edition: str, market: dict) -> bytes | None:
    """A seamless crawl of the day's real numbers."""
    t = TH[edition]
    s = SCALE
    W, H, N = COVER[0] * s, 26 * s, 30
    cells = []
    for key, lab, fmt in (("y2", "2Y", "{:.2f}%"), ("y10", "10Y", "{:.2f}%"),
                          ("y30", "30Y", "{:.2f}%"),
                          ("curve_2s10s_bps", "2s10s", "{:+d}bp"),
                          ("mortgage30", "30Y FIX", "{:.2f}%")):
        v = market.get(key)
        if v is not None:
            cells.append((lab, fmt.format(v)))
    if not cells:
        return None
    fo, fv = font(11 * s, True), font(11 * s)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    widths, seg = [], 0.0
    for lab, val in cells:
        w = probe.textlength(lab, font=fo) + 8 * s + probe.textlength(val, font=fv) + 34 * s
        widths.append(w)
        seg += w

    frames = []
    for f in range(N):
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        off = -(f / N) * seg
        while off < W:
            x = off
            for (lab, val), w in zip(cells, widths):
                if -w < x < W:
                    d.text((x, 6 * s), lab, font=fo, fill=t["accent"])
                    d.text((x + probe.textlength(lab, font=fo) + 8 * s, 6 * s),
                           val, font=fv, fill=INK)
                x += w
            off += seg
        frames.append(im)
    return _encode(frames, 60)


def marker(edition: str) -> bytes | None:
    """The square beside every section head. One asset, reused eleven times."""
    t = TH[edition]
    s = SCALE
    S, N = 11 * s, 22
    frames = []
    for f in range(N):
        p = 0.5 - 0.5 * math.cos(f / N * math.tau)
        im = Image.new("RGB", (S, S), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, S, S], fill=_mix((255, 255, 255), t["accent"], 0.30 + 0.70 * p))
        inset = int(S * 0.30 * (1 - p))
        if inset:
            d.rectangle([inset, inset, S - inset, S - inset], fill=t["accent"])
        frames.append(im)
    return _encode(frames, 70)


def spark(edition: str, market: dict) -> bytes | None:
    """A chip-sized 10Y sparkline that draws itself, for the numbers callout."""
    ser = [v for _, v in market.get("y10_series") or []][-90:]
    if len(ser) < 8:
        return None
    t = TH[edition]
    s = SCALE
    W, H, N = 104 * s, 22 * s, 24
    lo, hi = min(ser), max(ser)
    if hi - lo < 1e-6:
        hi = lo + 1.0
    pts = [(2 * s + (W - 4 * s) * i / (len(ser) - 1),
            2 * s + (H - 4 * s) - (v - lo) / (hi - lo) * (H - 4 * s))
           for i, v in enumerate(ser)]
    frames = []
    for f in range(N):
        im = Image.new("RGB", (W, H), (251, 255, 232) if edition == "am" else (255, 241, 229))
        d = ImageDraw.Draw(im)
        d.line(pts, fill=_mix(t["accent"], (17, 17, 17), 0.35), width=1 * s, joint="curve")
        n = max(2, int(len(pts) * min(1.0, (f / N) * 1.6)))
        d.line(pts[:n], fill=(17, 17, 17), width=2, joint="curve")
        frames.append(im)
    return _encode(frames, 60)


def greeting(edition: str, name: str) -> bytes | None:
    """Kinetic front matter. The greeting is the card; the sentence stays live
    HTML underneath it, so the editorial content never becomes an image."""
    t = TH[edition]
    s = SCALE
    W, H, N = COVER[0] * s, 46 * s, 28
    text = f"{t['greet']}, {name}."
    fo = font(27 * s, True)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    tw = probe.textlength(text, font=fo)
    frames = []
    for f in range(N):
        ph = f / N
        im = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.text((0, 6 * s), text, font=fo, fill=(17, 17, 17))
        # an accent bar wipes across the baseline and settles under the name
        x = (0.5 - 0.5 * math.cos(ph * math.tau)) * tw
        d.rectangle([0, 38 * s, x, 42 * s], fill=t["accent"])
        frames.append(im)
    return _encode(frames, 55)


def build(edition: str, market: dict, date: str, stamp: str,
          issue_no: int, name: str) -> dict:
    """Every asset for one issue, keyed by content-id. Never raises."""
    jobs = {
        "cover":  lambda: cover(edition, market, date, stamp, issue_no, name),
        "greet":  lambda: greeting(edition, name),
        "ticker": lambda: ticker(edition, market),
        "marker": lambda: marker(edition),
        "spark":  lambda: spark(edition, market),
    }
    out, total = {}, 0
    for key, fn in jobs.items():
        if Image is None:
            print(f"  art: Pillow unavailable — shipping without {key}")
            continue
        try:
            data = fn()
        except Exception as e:
            print(f"  art: {key} failed ({type(e).__name__}: {e}) — shipping without it")
            continue
        if not data:
            print(f"  art: {key} had no data — skipped")
            continue
        if total + len(data) > BUDGET:
            print(f"  art: {key} would exceed the {BUDGET//1000}kB image budget — skipped")
            continue
        total += len(data)
        out[key] = data
        print(f"  art: {key} {len(data)/1024:.0f} kB")
    print(f"  art: {len(out)} assets, {total/1024:.0f} kB total")
    return out
