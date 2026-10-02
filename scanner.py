# -*- coding: utf-8 -*-
"""
סורק סווינג יומי — כ"ח 28
-------------------------
מה הוא עושה בכל הרצה:
1. מושך את 28 המניות הגדולות בנאסד"ק + NYSE לפי שווי שוק (מתעדכן אוטומטית בכל ריצה).
2. מושך יקום גילוי: מניות ארה"ב בשווי שוק מעל 2 מיליארד $ עם רווח נקי חיובי (12 חודשים).
3. מוריד נרות יומיים לשנה וחצי ומחשב SMA150, SMA200, RSI14, ATR14, מחזור יחסי, התנגדות.
4. מחפש את הסט-אפ הראשי: פריצה מעל SMA150 וגם SMA200 שהחזיקה לפחות יום מסחר אחד.
5. מוסיף אישורים: מחזור כפול ביום הפריצה, פריצת התנגדות, RSI שיצא ממכירת יתר.
6. מחשב סטופ = סגירה פחות 1.5 ATR, ומפיק דוח PDF בעברית לתיקיית Reports.

שינוי פרמטרים: בבלוק SETTINGS למטה.
"""
import os
import sys
import json
import math
import datetime as dt
import subprocess
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from yfinance import EquityQuery as Q

# ============================ SETTINGS ============================
TOP_N = 28                    # כמה מניות גדולות לעקוב
MIN_MCAP_DISCOVERY = 2e9      # סף שווי שוק לגילוי מניות חדשות
MAX_DISCOVERY = 1500          # כמה מניות לכל היותר ביקום הגילוי
SMA_A, SMA_B = 150, 200
HOLD_DAYS_MIN = 1             # כמה ימים הפריצה צריכה להחזיק אחרי יום הפריצה
BREAKOUT_LOOKBACK = 5         # פריצה טרייה = התרחשה ב-5 ימי המסחר האחרונים לכל היותר
VOL_MULT = 2.0                # מחזור חריג = פי 2 מממוצע 20 יום
ATR_MULT = 1.5                # סטופ = סגירה - 1.5 * ATR14
RSI_OVERSOLD = 30
RSI_LOOKBACK = 10             # RSI יצא ממכירת יתר ב-10 הימים האחרונים
RES_LOOKBACK = 60             # התנגדות = שיא 60 ימי המסחר שלפני חלון הפריצה

NASDAQ = ["NMS", "NGM", "NCM"]
NYSE = ["NYQ"]
BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "Reports"
STATE_FILE = BASE_DIR / "top_list_state.json"
# ==================================================================


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------- universes ----------------------------
def _screen_all(query, sort_field="intradaymarketcap", limit=250):
    out, offset = [], 0
    while offset < limit:
        size = min(250, limit - offset)
        res = yf.screen(query, offset=offset, size=size, sortField=sort_field, sortAsc=False)
        quotes = res.get("quotes", []) if isinstance(res, dict) else []
        if not quotes:
            break
        out.extend(quotes)
        if len(quotes) < size:
            break
        offset += size
    return out


def _is_common_stock(q):
    sym = q.get("symbol", "")
    qt = q.get("quoteType", "EQUITY")
    # מסנן מניות בכורה / יחידות / וורנטים
    bad = ("-P" in sym) or sym.endswith((".U", ".W", "-WT", "-UN")) or ".PR" in sym
    return qt == "EQUITY" and not bad


def get_top_list():
    """28 הגדולות לפי שווי שוק. מנקה כפילויות של אותה חברה (למשל GOOG/GOOGL)."""
    query = Q("and", [
        Q("eq", ["region", "us"]),
        Q("is-in", ["exchange"] + NASDAQ + NYSE),
        Q("gt", ["intradaymarketcap", 5e10]),
    ])
    quotes = _screen_all(query, limit=80)
    seen_names, top = set(), []
    for q in quotes:
        if not _is_common_stock(q):
            continue
        name = (q.get("longName") or q.get("shortName") or q["symbol"]).lower()
        key = name.replace("inc.", "").replace("class a", "").replace("class c", "").strip()
        if key in seen_names:
            continue
        seen_names.add(key)
        top.append({
            "symbol": q["symbol"],
            "name": q.get("shortName") or q.get("longName") or q["symbol"],
            "mcap": q.get("marketCap") or 0,
        })
        if len(top) >= TOP_N:
            break
    return top


def get_discovery_universe():
    """מניות ארה"ב, נאסד"ק/NYSE, שווי שוק מעל 2B$, רווח נקי 12 חודשים חיובי."""
    query = Q("and", [
        Q("eq", ["region", "us"]),
        Q("is-in", ["exchange"] + NASDAQ + NYSE),
        Q("gt", ["intradaymarketcap", MIN_MCAP_DISCOVERY]),
        Q("gt", ["netincomeis.lasttwelvemonths", 0]),
    ])
    quotes = _screen_all(query, limit=MAX_DISCOVERY)
    return {
        q["symbol"]: {"name": q.get("shortName") or q["symbol"], "mcap": q.get("marketCap") or 0}
        for q in quotes if _is_common_stock(q)
    }


def track_top_changes(top):
    """משווה לרשימה של הריצה הקודמת ומחזיר מי נכנס ומי יצא."""
    now = [t["symbol"] for t in top]
    prev = []
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("top", [])
        except Exception:
            prev = []
    STATE_FILE.write_text(json.dumps({"date": str(dt.date.today()), "top": now}), encoding="utf-8")
    if not prev:
        return [], []
    return [s for s in now if s not in prev], [s for s in prev if s not in now]


# ---------------------------- data ----------------------------
def download_history(symbols):
    frames = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        log(f"מוריד נתונים {i + 1}-{i + len(chunk)} מתוך {len(symbols)}")
        data = yf.download(chunk, period="18mo", interval="1d", auto_adjust=False,
                           group_by="ticker", threads=True, progress=False)
        for s in chunk:
            try:
                df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=["Close"])
                if len(df) >= SMA_B + 10:
                    frames[s] = df
            except Exception:
                pass
    return frames


# ---------------------------- indicators ----------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def analyze(df):
    c, v = df["Close"], df["Volume"]
    s_a = c.rolling(SMA_A).mean()
    s_b = c.rolling(SMA_B).mean()
    top_ma = pd.concat([s_a, s_b], axis=1).max(axis=1)
    above = c > top_ma                      # מעל שני הממוצעים
    r = rsi(c)
    a = atr(df)
    vavg20 = v.rolling(20).mean().shift(1)  # ממוצע 20 יום לפני היום הנבדק

    n = len(c)
    last = n - 1
    res = {
        "date": c.index[last].date(),
        "close": float(c.iloc[last]),
        "sma150": float(s_a.iloc[last]),
        "sma200": float(s_b.iloc[last]),
        "pct150": float(c.iloc[last] / s_a.iloc[last] - 1) * 100,
        "pct200": float(c.iloc[last] / s_b.iloc[last] - 1) * 100,
        "rsi": float(r.iloc[last]),
        "atr": float(a.iloc[last]),
        "relvol": float(v.iloc[last] / vavg20.iloc[last]) if vavg20.iloc[last] else float("nan"),
        "stop": float(c.iloc[last] - ATR_MULT * a.iloc[last]),
        "status": "below",
        "breakout_date": None,
        "days_held": None,
        "vol_confirm": False,
        "bo_relvol": None,
        "res_break": False,
        "resistance": None,
        "rsi_oversold_exit": False,
        "score": 0,
    }
    res["risk_pct"] = (res["close"] - res["stop"]) / res["close"] * 100

    if not above.iloc[last]:
        # מתחת לאחד הממוצעים לפחות
        res["status"] = "below_both" if c.iloc[last] < min(s_a.iloc[last], s_b.iloc[last]) else "between"
        return res

    # מצא את יום הפריצה: היום הראשון ברצף הנוכחי של סגירות מעל שני הממוצעים
    j = last
    while j > 0 and above.iloc[j - 1]:
        j -= 1
    bo = j
    days_held = last - bo                   # 0 = הפריצה היא היום
    res["breakout_date"] = c.index[bo].date()
    res["days_held"] = days_held

    if days_held > BREAKOUT_LOOKBACK:
        res["status"] = "above"             # כבר מעל הממוצעים זמן רב — טרנד קיים
    elif days_held < HOLD_DAYS_MIN:
        res["status"] = "breakout_today"    # פרצה היום, עוד לא החזיקה — רשימת מעקב
    else:
        res["status"] = "setup"             # הסט-אפ הראשי

    # אישורים — נבדקים על חלון הפריצה
    win = range(bo, last + 1)
    bo_rv = [v.iloc[k] / vavg20.iloc[k] for k in win if vavg20.iloc[k]]
    res["bo_relvol"] = float(max(bo_rv)) if bo_rv else None
    res["vol_confirm"] = bool(res["bo_relvol"] and res["bo_relvol"] >= VOL_MULT)

    start = max(0, bo - RES_LOOKBACK)
    if bo - start >= 20:
        resistance = float(df["High"].iloc[start:bo].max())
        res["resistance"] = resistance
        res["res_break"] = bool(c.iloc[last] > resistance)

    lo = max(1, last - RSI_LOOKBACK)
    rr = r.iloc[lo - 1:last + 1]
    res["rsi_oversold_exit"] = bool(((rr.shift(1) < RSI_OVERSOLD) & (rr >= RSI_OVERSOLD)).any())

    res["score"] = int(res["vol_confirm"]) + int(res["res_break"]) + int(res["rsi_oversold_exit"])
    return res


# ---------------------------- report ----------------------------
STATUS_HE = {
    "setup": "✅ סט-אפ: פרצה והחזיקה",
    "breakout_today": "👀 פרצה היום — לבדוק מחר",
    "above": "מעל שני הממוצעים (טרנד קיים)",
    "between": "בין הממוצעים",
    "below_both": "מתחת לשני הממוצעים",
    "below": "מתחת",
}


def fmt_mcap(x):
    if not x:
        return "—"
    return f"{x / 1e12:.2f}T" if x >= 1e12 else f"{x / 1e9:.0f}B"


def f2(x, suffix=""):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.2f}{suffix}"


def checks(r):
    out = []
    out.append(("מחזור ×2", r["vol_confirm"], f2(r["bo_relvol"], "×") if r["bo_relvol"] else "—"))
    out.append(("פריצת התנגדות", r["res_break"], f2(r["resistance"]) if r["resistance"] else "—"))
    out.append(("RSI יצא ממכירת יתר", r["rsi_oversold_exit"], f2(r["rsi"])))
    return "".join(
        f'<span class="chk {"ok" if ok else "no"}">{"✔" if ok else "✖"} {name} <b>{val}</b></span>'
        for name, ok, val in out)


def setup_card(sym, meta, r, tag):
    return f"""
    <div class="card">
      <div class="card-h"><span class="sym">{sym}</span><span class="nm">{meta.get('name','')}</span>
        <span class="tag">{tag}</span><span class="score">אישורים {r['score']}/3</span></div>
      <table class="kv">
        <tr><td>סגירה</td><td>{f2(r['close'])}</td><td>יום פריצה</td><td>{r['breakout_date']}</td>
            <td>ימים מעל</td><td>{r['days_held']}</td></tr>
        <tr><td>SMA150</td><td>{f2(r['sma150'])} ({f2(r['pct150'],'%')})</td>
            <td>SMA200</td><td>{f2(r['sma200'])} ({f2(r['pct200'],'%')})</td>
            <td>ATR14</td><td>{f2(r['atr'])}</td></tr>
        <tr><td class="stop">סטופ 1.5 ATR</td><td class="stop">{f2(r['stop'])}</td>
            <td>סיכון לסטופ</td><td>{f2(r['risk_pct'],'%')}</td>
            <td>שווי שוק</td><td>{fmt_mcap(meta.get('mcap'))}</td></tr>
      </table>
      <div class="chks">{checks(r)}</div>
    </div>"""


def build_html(top, disc_meta, results, entered, exited, errors):
    today = dt.date.today()
    data_date = max((r["date"] for r in results.values()), default=today)
    top_syms = [t["symbol"] for t in top]
    top_meta = {t["symbol"]: t for t in top}

    def sort_key(s):
        r = results[s]
        return (-r["score"], r["days_held"] or 0)

    top_setups = sorted([s for s in top_syms if s in results and results[s]["status"] == "setup"], key=sort_key)
    disc_setups = sorted([s for s in disc_meta if s not in top_meta and s in results
                          and results[s]["status"] == "setup"], key=sort_key)
    watch = [s for s in list(top_syms) + list(disc_meta) if s in results
             and results[s]["status"] == "breakout_today"]
    watch = list(dict.fromkeys(watch))

    html = [f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>סריקת סווינג {today}</title><style>
@page {{ size: A4; margin: 12mm; }}
body {{ font-family: 'Segoe UI', 'Noto Sans Hebrew', 'DejaVu Sans', Arial, sans-serif; color:#1b2330; font-size:11px; margin:0; }}
h1 {{ font-size:20px; margin:0; }} h2 {{ font-size:14px; margin:18px 0 6px; border-bottom:2px solid #1f4e79; padding-bottom:3px; color:#1f4e79; }}
.head {{ background:#1f4e79; color:#fff; padding:12px 14px; border-radius:6px; }}
.head small {{ opacity:.85; }}
.sum {{ display:flex; gap:8px; margin:10px 0; }}
.box {{ flex:1; border:1px solid #d5dce6; border-radius:6px; padding:8px; text-align:center; }}
.box b {{ display:block; font-size:20px; color:#1f4e79; }}
.card {{ border:1px solid #cfd8e3; border-right:5px solid #2e8b57; border-radius:6px; padding:8px; margin:8px 0; page-break-inside:avoid; }}
.card-h {{ display:flex; align-items:center; gap:10px; margin-bottom:4px; }}
.sym {{ font-size:15px; font-weight:700; direction:ltr; }} .nm {{ color:#5a6678; }}
.tag {{ background:#e8f3ec; color:#2e6b45; padding:1px 7px; border-radius:10px; }}
.score {{ margin-inline-start:auto; font-weight:700; }}
table {{ border-collapse:collapse; width:100%; }}
.kv td {{ padding:2px 5px; }} .kv td:nth-child(odd) {{ color:#5a6678; }}
.stop {{ color:#b42318; font-weight:700; }}
.chks {{ margin-top:5px; display:flex; gap:6px; flex-wrap:wrap; }}
.chk {{ padding:2px 7px; border-radius:10px; }} .ok {{ background:#e8f3ec; color:#1e6b3a; }} .no {{ background:#f3f4f6; color:#7a8494; }}
.grid th {{ background:#eef2f7; padding:4px; font-weight:600; border-bottom:1px solid #cfd8e3; }}
.grid td {{ padding:3px 4px; border-bottom:1px solid #eef1f5; text-align:center; }}
.num {{ direction:ltr; unicode-bidi:embed; }}
.pos {{ color:#1e6b3a; }} .neg {{ color:#b42318; }}
.st-setup {{ background:#e8f3ec; font-weight:700; }} .st-breakout_today {{ background:#fff6e0; }}
.note {{ color:#5a6678; font-size:10px; margin-top:14px; line-height:1.5; }}
.empty {{ color:#5a6678; padding:6px 0; }}
</style></head><body>
<div class="head"><h1>סריקת סווינג יומית</h1>
<small>נתוני סגירה של {data_date} · הופק {dt.datetime.now():%d/%m/%Y %H:%M} · כלל ראשי: פריצה מעל SMA150 ו-SMA200 שהחזיקה לפחות יום אחד</small></div>
<div class="sum">
 <div class="box"><b>{len(top_setups)}</b>סט-אפים ב-Top {TOP_N}</div>
 <div class="box"><b>{len(disc_setups)}</b>סט-אפים בגילוי</div>
 <div class="box"><b>{len(watch)}</b>פרצו היום (מעקב)</div>
 <div class="box"><b>{len(results)}</b>מניות נסרקו</div>
</div>"""]

    if entered or exited:
        html.append(f'<div class="box" style="text-align:right">שינוי ברשימת Top {TOP_N}: '
                    f'נכנסו <b style="display:inline;font-size:12px">{", ".join(entered) or "—"}</b> · '
                    f'יצאו <b style="display:inline;font-size:12px">{", ".join(exited) or "—"}</b></div>')

    html.append(f"<h2>סט-אפים — Top {TOP_N}</h2>")
    html.append("".join(setup_card(s, top_meta[s], results[s], "Top " + str(TOP_N)) for s in top_setups)
                or '<div class="empty">אין היום סט-אפ חדש ברשימה הקבועה.</div>')

    html.append("<h2>סט-אפים — מניות חדשות (שווי שוק &gt; 2B$, רווחיות חיובית)</h2>")
    html.append("".join(setup_card(s, disc_meta[s], results[s], "גילוי") for s in disc_setups[:25])
                or '<div class="empty">אין היום סט-אפ במניות הגילוי.</div>')
    if len(disc_setups) > 25:
        html.append(f'<div class="empty">ועוד {len(disc_setups) - 25}: {", ".join(disc_setups[25:])}</div>')

    html.append("<h2>רשימת מעקב — פרצו היום, צריך אישור של יום נוסף</h2>")
    if watch:
        html.append('<table class="grid"><tr><th>מניה</th><th>סגירה</th><th>% מעל 150</th>'
                    '<th>% מעל 200</th><th>מחזור יחסי</th><th>RSI</th><th>סטופ</th></tr>')
        for s in watch:
            r = results[s]
            html.append(f'<tr><td class="num"><b>{s}</b></td><td class="num">{f2(r["close"])}</td>'
                        f'<td class="num">{f2(r["pct150"],"%")}</td><td class="num">{f2(r["pct200"],"%")}</td>'
                        f'<td class="num">{f2(r["relvol"],"×")}</td><td class="num">{f2(r["rsi"])}</td>'
                        f'<td class="num">{f2(r["stop"])}</td></tr>')
        html.append("</table>")
    else:
        html.append('<div class="empty">אין.</div>')

    html.append(f"<h2>מצב כל {TOP_N} הגדולות</h2>")
    html.append('<table class="grid"><tr><th>#</th><th>מניה</th><th>שווי שוק</th><th>סגירה</th>'
                '<th>% מ-SMA150</th><th>% מ-SMA200</th><th>RSI</th><th>מחזור יחסי</th>'
                '<th>סטופ 1.5ATR</th><th>מצב</th></tr>')
    for i, t in enumerate(top, 1):
        s = t["symbol"]
        r = results.get(s)
        if not r:
            html.append(f'<tr><td>{i}</td><td class="num"><b>{s}</b></td><td>{fmt_mcap(t["mcap"])}</td>'
                        f'<td colspan="7">אין מספיק נתונים</td></tr>')
            continue
        cls = lambda x: "pos" if x >= 0 else "neg"
        html.append(
            f'<tr class="st-{r["status"]}"><td>{i}</td><td class="num"><b>{s}</b></td>'
            f'<td class="num">{fmt_mcap(t["mcap"])}</td><td class="num">{f2(r["close"])}</td>'
            f'<td class="num {cls(r["pct150"])}">{f2(r["pct150"],"%")}</td>'
            f'<td class="num {cls(r["pct200"])}">{f2(r["pct200"],"%")}</td>'
            f'<td class="num">{f2(r["rsi"])}</td><td class="num">{f2(r["relvol"],"×")}</td>'
            f'<td class="num">{f2(r["stop"])}</td><td>{STATUS_HE.get(r["status"], r["status"])}</td></tr>')
    html.append("</table>")

    html.append(f"""<div class="note">
<b>הגדרות:</b> סט-אפ = סגירה מעל SMA150 וגם מעל SMA200, אחרי שבסגירה הקודמת לרצף הייתה מתחת לאחד מהם,
והרצף החזיק {HOLD_DAYS_MIN}–{BREAKOUT_LOOKBACK} ימי מסחר אחרי יום הפריצה.
מחזור ×2 = מחזור באחד מימי הפריצה ≥ פי {VOL_MULT:g} מממוצע 20 הימים שלפניו.
פריצת התנגדות = סגירה מעל השיא של {RES_LOOKBACK} ימי המסחר שלפני הפריצה.
RSI יצא ממכירת יתר = RSI14 חצה את {RSI_OVERSOLD} כלפי מעלה ב-{RSI_LOOKBACK} הימים האחרונים.
סטופ = סגירה אחרונה פחות {ATR_MULT:g}×ATR14. נתונים: Yahoo Finance (עשויים להכיל עיכובים או טעויות).
הדוח הוא כלי סינון טכני ואינו ייעוץ השקעות.
{('<br><b>שגיאות:</b> ' + '; '.join(errors)) if errors else ''}
</div></body></html>""")
    return "\n".join(html)


def html_to_pdf(html_path, pdf_path):
    """ממיר ל-PDF עם Microsoft Edge שמותקן בכל Windows (ללא התקנות נוספות)."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/usr/bin/chromium", "/usr/bin/google-chrome",
    ]
    for exe in candidates:
        if os.path.exists(exe):
            subprocess.run([exe, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
                           check=False, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(pdf_path):
                return True
    return False


# ---------------------------- main ----------------------------
def main():
    REPORT_DIR.mkdir(exist_ok=True)
    errors = []

    log("מושך את רשימת Top 28 לפי שווי שוק...")
    try:
        top = get_top_list()
    except Exception as e:
        errors.append(f"Top list: {e}")
        top = []
    if not top and STATE_FILE.exists():
        prev = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("top", [])
        top = [{"symbol": s, "name": s, "mcap": 0} for s in prev]
        errors.append("משתמש ברשימת Top מהריצה הקודמת")
    entered, exited = track_top_changes(top) if top else ([], [])
    log(f"Top {len(top)}: {', '.join(t['symbol'] for t in top)}")

    log("מושך יקום גילוי...")
    try:
        disc = get_discovery_universe()
    except Exception as e:
        errors.append(f"Discovery: {e}")
        disc = {}
    log(f"יקום גילוי: {len(disc)} מניות")

    frames = download_history([t["symbol"] for t in top] + list(disc))
    log(f"התקבלו נתונים ל-{len(frames)} מניות. מנתח...")

    results = {}
    for s, df in frames.items():
        try:
            results[s] = analyze(df)
        except Exception as e:
            errors.append(f"{s}: {e}")

    html = build_html(top, disc, results, entered, exited, errors[:10])
    stamp = dt.date.today().isoformat()
    html_path = REPORT_DIR / f"scan_{stamp}.html"
    pdf_path = REPORT_DIR / f"scan_{stamp}.pdf"
    html_path.write_text(html, encoding="utf-8")

    if html_to_pdf(html_path, str(pdf_path)):
        log(f"PDF מוכן: {pdf_path}")
        final = pdf_path
    else:
        log(f"לא נמצא Edge להמרה — הדוח נשמר כ-HTML: {html_path}")
        final = html_path

    # סיכום קצר להודעת טלגרם / מייל
    top_syms = [t["symbol"] for t in top]
    def pick(status, pool):
        return [s for s in pool if s in results and results[s]["status"] == status]
    ts = pick("setup", top_syms)
    ds = pick("setup", [s for s in disc if s not in top_syms])
    wt = list(dict.fromkeys(pick("breakout_today", top_syms) + pick("breakout_today", list(disc))))
    lines = [f"סריקת סווינג {stamp}",
             f"סט-אפים Top {TOP_N}: " + (", ".join(ts) or "אין"),
             f"סט-אפים גילוי: " + (", ".join(ds[:15]) or "אין") + (f" (+{len(ds)-15})" if len(ds) > 15 else ""),
             f"פרצו היום (מעקב): " + (", ".join(wt[:15]) or "אין")]
    if entered or exited:
        lines.append(f"שינוי ב-Top: נכנסו {', '.join(entered) or '—'} | יצאו {', '.join(exited) or '—'}")
    (REPORT_DIR / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    (REPORT_DIR / "latest_path.txt").write_text(str(final), encoding="utf-8")

    if "--open" in sys.argv and hasattr(os, "startfile"):
        os.startfile(final)
    return final


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        (BASE_DIR / "last_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        sys.exit(1)
