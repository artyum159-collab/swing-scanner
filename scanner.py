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
TOUCH_TOL = 0.005             # "נגיעה מלמטה" = השיא היומי הגיע עד 0.5% מהממוצע העליון (או מעליו) והסגירה מתחת
TOUCH_PRIOR_DAYS = 5          # ...אחרי לפחות 5 סגירות רצופות מתחת לממוצע העליון

SECTORS = ["Technology", "Communication Services", "Consumer Cyclical", "Consumer Defensive",
           "Financial Services", "Healthcare", "Industrials", "Energy", "Basic Materials",
           "Real Estate", "Utilities"]
SECTOR_HE = {
    "Technology": "טכנולוגיה", "Communication Services": "תקשורת ומדיה",
    "Consumer Cyclical": "צריכה מחזורית", "Consumer Defensive": "צריכה בסיסית",
    "Financial Services": "פיננסים", "Healthcare": "בריאות", "Industrials": "תעשייה",
    "Energy": "אנרגיה", "Basic Materials": "חומרי גלם", "Real Estate": "נדל\"ן",
    "Utilities": "תשתיות", "Other": "אחר",
}

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
    """מניות ארה"ב, נאסד"ק/NYSE, שווי שוק מעל 2B$, רווח נקי 12 חודשים חיובי — לפי סקטור."""
    out = {}
    for sec in SECTORS:
        query = Q("and", [
            Q("eq", ["region", "us"]),
            Q("is-in", ["exchange"] + NASDAQ + NYSE),
            Q("eq", ["sector", sec]),
            Q("gt", ["intradaymarketcap", MIN_MCAP_DISCOVERY]),
            Q("gt", ["netincomeis.lasttwelvemonths", 0]),
        ])
        try:
            quotes = _screen_all(query, limit=MAX_DISCOVERY)
        except Exception as e:
            log(f"סקטור {sec} נכשל: {e}")
            continue
        for q in quotes:
            if _is_common_stock(q):
                out[q["symbol"]] = {"name": q.get("shortName") or q["symbol"],
                                    "mcap": q.get("marketCap") or 0, "sector": sec}
    return out


def fill_sectors(top, disc):
    """משלים סקטור למניות Top שלא הופיעו ביקום הגילוי (למשל חברה ללא רווח)."""
    for t in top:
        if t["symbol"] in disc:
            t["sector"] = disc[t["symbol"]]["sector"]
            continue
        try:
            t["sector"] = yf.Ticker(t["symbol"]).info.get("sector") or "Other"
        except Exception:
            t["sector"] = "Other"
        if t["sector"] not in SECTOR_HE:
            t["sector"] = "Other"


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
        "pct_top_ma": 0.0,
    }
    res["risk_pct"] = (res["close"] - res["stop"]) / res["close"] * 100

    hi = float(top_ma.iloc[last])
    res["pct_top_ma"] = (res["close"] / hi - 1) * 100
    if not above.iloc[last]:
        # מתחת לאחד הממוצעים לפחות
        res["status"] = "below_both" if c.iloc[last] < min(s_a.iloc[last], s_b.iloc[last]) else "between"
        prior = (c.iloc[last - TOUCH_PRIOR_DAYS:last] < top_ma.iloc[last - TOUCH_PRIOR_DAYS:last]).all()
        if prior and float(df["High"].iloc[last]) >= hi * (1 - TOUCH_TOL):
            res["status"] = "touch"          # נגעה מלמטה בממוצע העליון ונסגרה מתחתיו
            res["touch_level"] = "SMA150" if s_a.iloc[last] >= s_b.iloc[last] else "SMA200"
            res["high"] = float(df["High"].iloc[last])
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
    "setup": "✅ פרצה והחזיקה",
    "breakout_today": "👀 פרצה היום",
    "touch": "🎯 נגעה מלמטה",
    "above": "מעל שניהם (טרנד)",
    "between": "בין הממוצעים",
    "below_both": "מתחת לשניהם",
    "below": "מתחת",
}
GREEN, RED, AMBER = "#2e8b57", "#c0392b", "#d4a017"


def fmt_mcap(x):
    if not x:
        return "—"
    return f"{x / 1e12:.2f}T" if x >= 1e12 else f"{x / 1e9:.0f}B"


def f2(x, suffix=""):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.2f}{suffix}"


def pc(x):
    cls = "pos" if x >= 0 else "neg"
    return f'<td class="num {cls}">{x:+.1f}%</td>'


def donut(pct_above, size=46):
    """עיגול: ירוק = % מעל שני הממוצעים, אדום = השאר."""
    r = 15.9155  # היקף = 100
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 42 42">'
            f'<circle cx="21" cy="21" r="{r}" fill="none" stroke="{RED}" stroke-width="7"/>'
            f'<circle cx="21" cy="21" r="{r}" fill="none" stroke="{GREEN}" stroke-width="7" '
            f'stroke-dasharray="{pct_above:.1f} {100 - pct_above:.1f}" stroke-dashoffset="25"/>'
            f'<text x="21" y="24" text-anchor="middle" font-size="9" font-weight="700" fill="#1b2330">'
            f'{pct_above:.0f}%</text></svg>')


def chk(ok):
    return f'<span class="{"ok" if ok else "no"}">{"✔" if ok else "✖"}</span>'


def build_html(top, disc_meta, results, entered, exited, errors):
    today = dt.date.today()
    data_date = max((r["date"] for r in results.values()), default=today)
    top_meta = {t["symbol"]: t for t in top}
    meta = dict(disc_meta)
    meta.update(top_meta)
    sector_of = lambda s: meta.get(s, {}).get("sector", "Other")

    # --- סטטיסטיקה לפי סקטור ---
    secs = {}
    for s, r in results.items():
        d = secs.setdefault(sector_of(s), {"n": 0, "above": 0, "between": 0, "setup": [], "today": [], "touch": []})
        d["n"] += 1
        if r["status"] in ("setup", "breakout_today", "above"):
            d["above"] += 1
        if r["status"] in ("between", "touch"):
            d["between"] += 1
        if r["status"] == "setup":
            d["setup"].append(s)
        elif r["status"] == "breakout_today":
            d["today"].append(s)
        elif r["status"] == "touch":
            d["touch"].append(s)
    order = sorted(secs, key=lambda k: -secs[k]["above"] / max(secs[k]["n"], 1))
    tot_n = sum(d["n"] for d in secs.values())
    tot_above = sum(d["above"] for d in secs.values())
    n_setup = sum(len(d["setup"]) for d in secs.values())
    n_today = sum(len(d["today"]) for d in secs.values())
    n_touch = sum(len(d["touch"]) for d in secs.values())

    html = [f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>סריקת סווינג {today}</title><style>
@page {{ size: A4; margin: 11mm; }}
body {{ font-family: 'Segoe UI', 'Noto Sans Hebrew', 'DejaVu Sans', Arial, sans-serif; color:#1b2330; font-size:10.5px; margin:0; }}
h1 {{ font-size:20px; margin:0; }}
h2 {{ font-size:14px; margin:16px 0 6px; border-bottom:2px solid #1f4e79; padding-bottom:3px; color:#1f4e79; }}
h3 {{ font-size:12.5px; margin:12px 0 4px; display:flex; align-items:center; gap:8px; }}
h3 .bar {{ flex:1; height:6px; border-radius:3px; background:{RED}; overflow:hidden; max-width:140px; }}
h3 .bar i {{ display:block; height:100%; background:{GREEN}; }}
h3 small {{ color:#5a6678; font-weight:400; }}
.head {{ background:#1f4e79; color:#fff; padding:12px 14px; border-radius:6px; }}
.head small {{ opacity:.85; }}
.sum {{ display:flex; gap:6px; margin:10px 0; }}
.box {{ flex:1; border:1px solid #d5dce6; border-radius:6px; padding:6px; text-align:center; }}
.box b {{ display:block; font-size:19px; color:#1f4e79; }}
table {{ border-collapse:collapse; width:100%; }}
.grid th {{ background:#eef2f7; padding:4px 3px; font-weight:600; border-bottom:1px solid #cfd8e3; font-size:10px; }}
.grid td {{ padding:3px; border-bottom:1px solid #eef1f5; text-align:center; }}
.grid td.l {{ text-align:right; }}
.num {{ direction:ltr; unicode-bidi:embed; }}
.pos {{ color:#1e6b3a; }} .neg {{ color:#b42318; }}
.ok {{ color:#1e6b3a; font-weight:700; }} .no {{ color:#b0b7c3; }}
.strong td {{ background:#eef8f1; }}
.stop {{ color:#b42318; font-weight:700; }}
.sym {{ font-weight:700; direction:ltr; unicode-bidi:embed; }}
.nm {{ color:#5a6678; font-size:9px; }}
.sec td {{ vertical-align:middle; }}
.g {{ color:{GREEN}; font-weight:700; }} .r {{ color:{RED}; font-weight:700; }}
.tags span {{ display:inline-block; direction:ltr; background:#f1f4f8; border-radius:8px; padding:0 5px; margin:1px; font-size:9.5px; }}
.st-setup {{ background:#e8f3ec; font-weight:700; }} .st-breakout_today {{ background:#fff6e0; }} .st-touch {{ background:#eaf1fb; }}
.note {{ color:#5a6678; font-size:9.5px; margin-top:14px; line-height:1.5; }}
.empty {{ color:#5a6678; padding:3px 0; }}
.sector {{ page-break-inside:avoid; }}
</style></head><body>
<div class="head"><h1>סריקת סווינג יומית</h1>
<small>נתוני סגירה של {data_date} · הופק {dt.datetime.now():%d/%m/%Y %H:%M} UTC · כלל ראשי: פריצה מעל SMA150 ו-SMA200 שהחזיקה לפחות יום אחד</small></div>
<div class="sum">
 <div class="box"><b>{n_setup}</b>✅ פרצו והחזיקו</div>
 <div class="box"><b>{n_today}</b>👀 פרצו היום</div>
 <div class="box"><b>{n_touch}</b>🎯 נגעו מלמטה</div>
 <div class="box"><b>{tot_above / max(tot_n, 1) * 100:.0f}%</b>מהשוק מעל שני הממוצעים</div>
 <div class="box"><b>{tot_n}</b>מניות נסרקו</div>
</div>"""]

    if entered or exited:
        html.append(f'<div class="box" style="text-align:right">שינוי ברשימת Top {TOP_N}: '
                    f'נכנסו <b style="display:inline;font-size:12px">{", ".join(entered) or "—"}</b> · '
                    f'יצאו <b style="display:inline;font-size:12px">{", ".join(exited) or "—"}</b></div>')

    # --- טבלת סקטורים ---
    html.append("<h2>מפת סקטורים — כמה מהמניות מעל שני הממוצעים</h2>")
    html.append('<table class="grid sec"><tr><th></th><th>סקטור</th><th>מעל שניהם</th><th>מתחת</th>'
                '<th>מתוכן בין הממוצעים</th><th>מניות</th><th>✅</th><th>👀</th><th>🎯</th></tr>')
    for k in order:
        d = secs[k]
        pa = d["above"] / d["n"] * 100
        html.append(f'<tr><td>{donut(pa)}</td><td class="l"><b>{SECTOR_HE.get(k, k)}</b></td>'
                    f'<td class="g">{pa:.0f}%</td><td class="r">{100 - pa:.0f}%</td>'
                    f'<td>{d["between"] / d["n"] * 100:.0f}%</td><td>{d["n"]}</td>'
                    f'<td>{len(d["setup"]) or "—"}</td><td>{len(d["today"]) or "—"}</td><td>{len(d["touch"]) or "—"}</td></tr>')
    html.append("</table>")

    # --- Top 28 ---
    html.append(f"<h2>Top {TOP_N} לפי שווי שוק</h2>")
    html.append('<table class="grid"><tr><th>#</th><th>מניה</th><th>סקטור</th><th>שווי</th><th>סגירה</th>'
                '<th>מ-SMA150</th><th>מ-SMA200</th><th>RSI</th><th>מחזור יחסי</th><th>סטופ</th><th>מצב</th></tr>')
    for i, t in enumerate(top, 1):
        s = t["symbol"]
        r = results.get(s)
        if not r:
            html.append(f'<tr><td>{i}</td><td class="sym">{s}</td><td colspan="9">אין מספיק נתונים</td></tr>')
            continue
        html.append(f'<tr class="st-{r["status"]}"><td>{i}</td><td class="sym">{s}</td>'
                    f'<td>{SECTOR_HE.get(t.get("sector", "Other"), "")}</td><td class="num">{fmt_mcap(t["mcap"])}</td>'
                    f'<td class="num">{f2(r["close"])}</td>{pc(r["pct150"])}{pc(r["pct200"])}'
                    f'<td class="num">{r["rsi"]:.0f}</td><td class="num">{f2(r["relvol"], "×")}</td>'
                    f'<td class="num">{f2(r["stop"])}</td><td>{STATUS_HE.get(r["status"], r["status"])}</td></tr>')
    html.append("</table>")

    # --- פירוט לפי סקטור ---
    html.append("<h2>סיגנלים לפי סקטור</h2>")
    tag = lambda s: ' <span class="nm">★Top</span>' if s in top_meta else ""
    any_sig = False
    for k in order:
        d = secs[k]
        if not (d["setup"] or d["today"] or d["touch"]):
            continue
        any_sig = True
        pa = d["above"] / d["n"] * 100
        html.append(f'<div class="sector"><h3>{SECTOR_HE.get(k, k)} <small>{pa:.0f}% מעל הממוצעים</small>'
                    f'<span class="bar"><i style="width:{pa:.0f}%"></i></span></h3>')
        if d["setup"]:
            rows = sorted(d["setup"], key=lambda s: (-results[s]["score"], -meta.get(s, {}).get("mcap", 0)))
            html.append('<table class="grid"><tr><th>✅ פרצו והחזיקו</th><th>סגירה</th><th>יום פריצה</th><th>ימים</th>'
                        '<th>מ-150</th><th>מ-200</th><th>מחזור×2</th><th>התנגדות</th><th>RSI ממכ"י</th>'
                        '<th>RSI</th><th>סטופ 1.5ATR</th><th>סיכון</th></tr>')
            for s in rows:
                r = results[s]
                html.append(f'<tr class="{"strong" if r["score"] >= 2 else ""}"><td class="l"><span class="sym">{s}</span>{tag(s)} '
                            f'<span class="nm">{meta.get(s, {}).get("name", "")[:22]}</span></td>'
                            f'<td class="num">{f2(r["close"])}</td><td class="num">{r["breakout_date"]:%d/%m}</td>'
                            f'<td>{r["days_held"]}</td>{pc(r["pct150"])}{pc(r["pct200"])}'
                            f'<td>{chk(r["vol_confirm"])} <span class="num nm">{f2(r["bo_relvol"], "×") if r["bo_relvol"] else ""}</span></td>'
                            f'<td>{chk(r["res_break"])}</td><td>{chk(r["rsi_oversold_exit"])}</td>'
                            f'<td class="num">{r["rsi"]:.0f}</td><td class="num stop">{f2(r["stop"])}</td>'
                            f'<td class="num">{r["risk_pct"]:.1f}%</td></tr>')
            html.append("</table>")
        if d["today"]:
            html.append('<div class="tags">👀 <b>פרצו היום, מחכות ליום אישור:</b> ' + "".join(
                f'<span>{s} {f2(results[s]["relvol"], "×")}</span>'
                for s in sorted(d["today"], key=lambda s: -(results[s]["relvol"] if results[s]["relvol"] == results[s]["relvol"] else 0))) + "</div>")
        if d["touch"]:
            html.append('<div class="tags">🎯 <b>נגעו מלמטה בממוצע ונסגרו מתחתיו:</b> ' + "".join(
                f'<span>{s} {results[s]["touch_level"]} {results[s]["pct_top_ma"]:+.1f}%</span>'
                for s in sorted(d["touch"], key=lambda s: -results[s]["pct_top_ma"])) + "</div>")
        html.append("</div>")
    if not any_sig:
        html.append('<div class="empty">אין היום סיגנלים.</div>')

    html.append(f"""<div class="note">
<b>הגדרות:</b> ✅ = סגירה מעל SMA150 וגם SMA200 אחרי שהייתה מתחת לאחד מהם, והרצף החזיק {HOLD_DAYS_MIN}–{BREAKOUT_LOOKBACK} ימי מסחר אחרי יום הפריצה.
👀 = הפריצה התרחשה ביום המסחר האחרון, צריך עוד יום. 🎯 = אחרי {TOUCH_PRIOR_DAYS}+ סגירות מתחת, השיא היומי הגיע לממוצע העליון מבין השניים (עד {TOUCH_TOL*100:g}% ממנו) אבל הסגירה נשארה מתחתיו; האחוז = מרחק הסגירה מהממוצע.
מחזור×2 = מחזור ≥ פי {VOL_MULT:g} מממוצע 20 יום באחד מימי הפריצה. התנגדות = סגירה מעל שיא {RES_LOOKBACK} הימים שלפני הפריצה.
RSI ממכ"י = RSI14 חצה את {RSI_OVERSOLD} כלפי מעלה ב-{RSI_LOOKBACK} הימים האחרונים. שורה ירוקה = לפחות 2 אישורים. סטופ = סגירה פחות {ATR_MULT:g}×ATR14.
מפת סקטורים: "מעל" = סגירה מעל שני הממוצעים; "מתחת" = מתחת לאחד מהם לפחות. היקום: Top {TOP_N} + מניות ארה"ב מעל 2B$ עם רווח נקי חיובי.
נתונים: Yahoo Finance. כלי סינון טכני בלבד, לא ייעוץ השקעות.
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
    fill_sectors(top, disc)

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
    tc = list(dict.fromkeys(pick("touch", top_syms) + pick("touch", list(disc))))
    lines = [f"סריקת סווינג {stamp}",
             f"סט-אפים Top {TOP_N}: " + (", ".join(ts) or "אין"),
             f"סט-אפים גילוי: " + (", ".join(ds[:15]) or "אין") + (f" (+{len(ds)-15})" if len(ds) > 15 else ""),
             f"פרצו היום (מעקב): " + (", ".join(wt[:15]) or "אין"),
             f"נגעו מלמטה בממוצע: " + (", ".join(tc[:15]) or "אין") + (f" (+{len(tc)-15})" if len(tc) > 15 else "")]
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
