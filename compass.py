"""The design system. Every component returns one table row.

Layout is tables with inline styles because that is what survives Gmail. The
<style> block added in wrap() carries only enhancements — hover, and the
dark-mode hints — never anything the brief depends on, so a client that drops it
still renders a complete issue.

All motion arrives as GIFs built by art.py and attached to the message under a
content-id. A missing asset is not an error: every component that takes a cid
falls back to the static treatment it had before, so the issue ships whole even
when the whole art step failed.
"""

MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

TH = {
 "am": dict(accent="#eeff8c", tint="#fbffe8", label="MORNING EDITION"),
 "pm": dict(accent="#ff6a00", tint="#fff1e5", label="AFTERNOON EDITION"),
}


def head(ed, date, stamp, issue_no, cover=None, ticker=None, y10=None):
    """The cover, then the crawl, then the dateline rail."""
    t = TH[ed]
    rows = []
    if cover:
        # Alt text carries the masthead for anyone with images off — it is the
        # only place the wordmark exists once the cover is an image.
        alt = f"COMPASS — {t['label']} No {issue_no}"
        if y10 is not None:
            alt += f" — US 10Y {y10:.2f}%"
        rows.append(
            f'<tr><td style="padding:0;font-size:0;line-height:0;background:#111111;">'
            f'<img src="cid:cover" width="616" alt="{alt}" '
            f'style="display:block;width:100%;max-width:616px;height:auto;border:0;"></td></tr>')
    else:
        rows.append(
            f'<tr><td style="padding:22px 22px 0 22px;font-family:{MONO};font-size:40px;'
            f'line-height:1;letter-spacing:5px;color:#111111;font-weight:700;">COMPASS</td></tr>')
    if ticker:
        rows.append(
            '<tr><td style="padding:0;font-size:0;line-height:0;background:#111111;'
            'border-bottom:2px solid #111111;">'
            '<img src="cid:ticker" width="616" alt="" '
            'style="display:block;width:100%;max-width:616px;height:auto;border:0;"></td></tr>')
    rows.append(
        f'<tr><td style="padding:14px 22px 0 22px;">'
        f'<div style="font-family:{MONO};font-size:11px;line-height:1.5;color:#8a8a8a;'
        f'letter-spacing:1.5px;">{date} &nbsp;/&nbsp; '
        f'<span style="color:#111111;background:{t["accent"]};padding:2px 7px;font-weight:700;">'
        f'{t["label"]}</span> &nbsp;/&nbsp; {stamp} &nbsp;/&nbsp; NO {issue_no}</div></td></tr>')
    return "".join(rows)


def greet(text, card=None):
    """The kinetic greeting card. The sentence below it stays live text."""
    if card:
        return ('<tr><td style="padding:18px 22px 0 22px;font-size:0;line-height:0;">'
                f'<img src="cid:greet" width="420" alt="{text}" '
                'style="display:block;width:100%;max-width:420px;height:auto;border:0;">'
                '</td></tr>')
    return (f'<tr><td style="padding:18px 22px 0 22px;"><div style="font-family:{SANS};'
            f'font-size:26px;font-weight:700;line-height:1.2;color:#111111;">{text}</div></td></tr>')


def sec(ed, title, marker=None):
    a = TH[ed]["accent"]
    box = (f'<img src="cid:marker" width="11" height="11" alt="" '
           f'style="display:block;border:0;">' if marker
           else f'<div style="width:11px;height:11px;background:{a};"></div>')
    return f'''<tr><td style="padding:26px 22px 0 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="11" valign="middle" style="font-size:0;line-height:0;">{box}</td>
<td style="padding-left:9px;font-family:{MONO};font-size:14px;font-weight:700;letter-spacing:3.5px;color:#111111;white-space:nowrap;">{title}</td>
<td style="padding-left:12px;"><div style="border-bottom:2px solid #111111;height:1px;font-size:0;">&nbsp;</div></td>
</tr></table></td></tr>'''


def sub(label):
    return (f'<tr><td style="padding:15px 22px 8px 22px;font-family:{MONO};font-size:10px;'
            f'font-weight:700;letter-spacing:2px;color:#8a8a8a;">{label}</td></tr>')


def item(ed, text, src=None, link=None, rule=True):
    # Source tag carries the edition accent. It is a dark-text-on-accent chip rather
    # than accent-colored text: #eeff8c as text on white is 1.09:1 contrast, i.e.
    # invisible. Same treatment as the edition label in head().
    a = TH[ed]["accent"]
    tag = ""
    if src:
        s = (f'<span class="chip" style="font-family:{MONO};font-size:10px;color:#111111;'
             f'background:{a};padding:1px 5px;white-space:nowrap;">{src}</span>')
        tag = f' <a href="{link}" style="text-decoration:none;">{s}</a>' if link else f' {s}'
    b = "border-bottom:1px solid #eeeeea;" if rule else ""
    return (f'<tr><td style="padding:0 22px 9px 22px;"><div style="font-family:{SANS};font-size:14px;'
            f'line-height:1.6;color:#2a2a2a;padding-bottom:9px;{b}">{text}{tag}</div></td></tr>')


def note(ed, text, spark=None):
    """The standing numbers. Tabular figures so columns of rates line up, and the
    10Y sparkline rides along when art.py produced one."""
    t = TH[ed]
    chip = ('<td width="104" valign="middle" style="padding-left:11px;font-size:0;">'
            '<img src="cid:spark" width="104" height="22" alt="" '
            'style="display:block;border:0;"></td>') if spark else ""
    return (f'<tr><td style="padding:2px 22px 12px 22px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{t["tint"]};border-left:4px solid {t["accent"]};"><tr>'
            f'<td style="padding:8px 11px;font-family:{MONO};font-size:12px;line-height:1.6;'
            f'color:#4a4a4a;font-variant-numeric:tabular-nums;'
            f'font-feature-settings:\'tnum\' 1;">// {text}</td>{chip}</tr></table></td></tr>')


def lead(text):
    return (f'<tr><td style="padding:10px 22px 0 22px;"><div style="font-family:{SANS};font-size:15px;'
            f'line-height:1.65;color:#2a2a2a;">{text}</div></td></tr>')


def foot(ed, text):
    t = TH[ed]
    return f'''<tr><td style="padding:26px 22px 22px 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="150" height="7" style="background:{t['accent']};font-size:0;line-height:0;">&nbsp;</td>
<td height="7" style="background:#111111;font-size:0;line-height:0;">&nbsp;</td></tr></table>
<div style="font-family:{MONO};font-size:10px;line-height:1.8;color:#8a8a8a;padding-top:12px;letter-spacing:0.5px;">{text}</div>
</td></tr>'''


# Enhancement only. Gmail keeps <style>, but nothing structural lives here — a
# client that strips it still gets the fully inlined issue above.
STYLE = """
:root{color-scheme:light dark;supported-color-schemes:light dark;}
a{color:inherit;}
.chip{border-radius:1px;}
a:hover .chip{filter:brightness(1.06);}
@media (max-width:480px){
  .pad{padding-left:14px !important;padding-right:14px !important;}
}
"""


def wrap(rows):
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f'<style>{STYLE}</style></head>'
            '<body style="margin:0;padding:0;background:#ffffff;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:#ffffff;padding:14px 6px;"><tr><td align="center">'
            '<table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" '
            'style="width:620px;max-width:100%;background:#ffffff;border:2px solid #111111;">'
            + "".join(rows) + '</table></td></tr></table></body></html>')
