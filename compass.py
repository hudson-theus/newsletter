RAW = "https://raw.githubusercontent.com/hudson-theus/newsletter/main"

TH = {
 "am": dict(accent="#eeff8c", tint="#fbffe8", label="MORNING EDITION",
            img=f"{RAW}/compass_am.png", banner=f"{RAW}/banner_am.gif"),
 "pm": dict(accent="#ff6a00", tint="#fff1e5", label="AFTERNOON EDITION",
            img=f"{RAW}/compass_pm.png", banner=f"{RAW}/banner_pm.gif"),
}
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

def head(ed, date, stamp):
    t = TH[ed]
    return f'''<tr><td style="padding:0;font-size:0;line-height:0;border-bottom:2px solid #111111;">
<img src="{t['banner']}" width="616" alt="" style="display:block;width:100%;max-width:616px;height:auto;border:0;">
</td></tr>
<tr><td style="padding:26px 22px 0 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td valign="middle" width="70"><img src="{t['img']}" width="62" height="62" alt="" style="display:block;border:0;"></td>
<td valign="middle" style="padding-left:18px;font-family:{MONO};font-size:40px;line-height:1;letter-spacing:5px;color:#111111;font-weight:700;">COMPASS</td>
</tr></table></td></tr>
<tr><td style="padding:16px 22px 0 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="150" height="7" style="background:{t['accent']};font-size:0;line-height:0;">&nbsp;</td>
<td height="7" style="background:#111111;font-size:0;line-height:0;">&nbsp;</td></tr></table>
<div style="font-family:{MONO};font-size:11px;line-height:1.5;color:#8a8a8a;padding-top:10px;letter-spacing:1.5px;">{date} &nbsp;/&nbsp; <span style="color:#111111;background:{t['accent']};padding:2px 7px;font-weight:700;">{t['label']}</span> &nbsp;/&nbsp; {stamp}</div>
</td></tr>'''

def sec(ed, title):
    a = TH[ed]["accent"]
    return f'''<tr><td style="padding:26px 22px 0 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="11" valign="middle" style="font-size:0;line-height:0;"><div style="width:11px;height:11px;background:{a};"></div></td>
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
        s = (f'<span style="font-family:{MONO};font-size:10px;color:#111111;'
             f'background:{a};padding:1px 5px;white-space:nowrap;">{src}</span>')
        tag = f' <a href="{link}" style="text-decoration:none;">{s}</a>' if link else f' {s}'
    b = "border-bottom:1px solid #eeeeea;" if rule else ""
    return (f'<tr><td style="padding:0 22px 9px 22px;"><div style="font-family:{SANS};font-size:14px;'
            f'line-height:1.6;color:#2a2a2a;padding-bottom:9px;{b}">{text}{tag}</div></td></tr>')

def note(ed, text):
    t = TH[ed]
    return (f'<tr><td style="padding:2px 22px 12px 22px;"><div style="font-family:{MONO};font-size:12px;'
            f'line-height:1.6;color:#4a4a4a;background:{t["tint"]};border-left:4px solid {t["accent"]};'
            f'padding:8px 11px;">// {text}</div></td></tr>')

def lead(text):
    return (f'<tr><td style="padding:15px 22px 0 22px;"><div style="font-family:{SANS};font-size:15px;'
            f'line-height:1.65;color:#2a2a2a;">{text}</div></td></tr>')

def foot(ed, text):
    t = TH[ed]
    return f'''<tr><td style="padding:26px 22px 22px 22px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="150" height="7" style="background:{t['accent']};font-size:0;line-height:0;">&nbsp;</td>
<td height="7" style="background:#111111;font-size:0;line-height:0;">&nbsp;</td></tr></table>
<div style="font-family:{MONO};font-size:10px;line-height:1.8;color:#8a8a8a;padding-top:12px;letter-spacing:0.5px;">{text}</div>
</td></tr>'''

def wrap(rows):
    return ('<div style="margin:0;padding:0;background:#ffffff;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:#ffffff;padding:14px 6px;"><tr><td align="center">'
            '<table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" '
            'style="width:620px;max-width:100%;background:#ffffff;border:2px solid #111111;">'
            + "".join(rows) + '</table></td></tr></table></div>')
