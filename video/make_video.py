#!/usr/bin/env python3
"""
סרטון חדשות יומי מהסריקה.

קורא את Reports/video_data.json (נוצר ע"י scanner.py), כותב תסריט קריינות בעברית,
מקריא אותו בקול עברי (edge-tts), מנפיש את news.html עם Chromium ומחבר הכול ל-MP4 אנכי.
אם ההקראה נכשלת, הסרטון נבנה עם כתוביות בלבד, כדי שהדוח היומי לא ייפגע.

שימוש:  python video/make_video.py [--out out/news.mp4] [--data Reports/video_data.json]
"""
import argparse
import asyncio
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VOICE = "he-IL-AvriNeural"      # קול גברי. לקול נשי: he-IL-HilaNeural
RATE = "+6%"
FPS = 30
LEAD = 0.7                      # שניות לפני שהקריין מתחיל בכל מקטע
TAIL = 0.9                      # שניות אחרי שהקריין מסיים

MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]
CONF_SPOKEN = {"vol": "מחזור חריג", "res": "פריצת התנגדות", "rsi": "יציאה ממכירת יתר ב-RSI"}


def log(m):
    print(f"[video {dt.datetime.now():%H:%M:%S}] {m}", flush=True)


def short_name(name, sym):
    """Accenture plc -> Accenture. שם שהקריין יכול להגיד."""
    n = re.split(r",| Inc\b| Corp| Corporation| Incorporated| plc| Ltd| Limited| Holdings?| Group| Company| Co\.| N\.V\.| S\.A\.| SE\b| AG\b| LP\b| \(",
                 name or "")[0].strip(" .")
    return n if 2 <= len(n) <= 28 and not n.isupper() or n in ("IBM",) else sym


def he_list(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " ו" + ("-" if re.match(r"[A-Za-z0-9]", items[-1]) else "") + items[-1]


def pre(prefix, word):
    """ל + Accenture -> ל-Accenture"""
    return prefix + ("-" if re.match(r"[A-Za-z0-9]", word) else "") + word


def uniq(items):
    return list(dict.fromkeys(items))


def spoken_date(iso_or_he, d):
    days = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    return f"יום {days[d.weekday()]}, {d.day} ב{MONTHS[d.month - 1]}"


def x_(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- script
def build_script(D):
    names = dict(D["top"].get("names", {}))
    for grp in ("setups", "watch", "touch"):
        for x in D[grp]:
            names[x["sym"]] = x["name"]
    say = lambda s: short_name(names.get(s, s), s)

    trade = re.search(r"(\d\d)/(\d\d)/(\d{4})", D["tradeDateHe"])
    trade_d = dt.date(int(trade[3]), int(trade[2]), int(trade[1])) if trade else dt.date.fromisoformat(D["date"])
    c, pct = D["counts"], D["pctAbove"]
    scenes = []

    scenes.append(dict(kind="intro", text=[
        "בוקר טוב, וברוכים הבאים לחדשות הסורק של כח 28.",
        f"זה הסיכום של יום המסחר בוול סטריט, {spoken_date(None, trade_d)}."]))

    tone = "השוק חזק" if pct >= 60 else "השוק מעורב" if pct >= 45 else "השוק חלש"
    title = f"{tone}: {pct}% מעל שני הממוצעים"
    scenes.append(dict(kind="pulse", title=title, text=[
        f"נבדקו {D['checked']:,} מניות בנאסד\"ק ובבורסת ניו יורק.",
        f"{pct} אחוז מהן נסחרות מעל הממוצע של 150 יום וגם מעל הממוצע של 200 יום.",
        f"{c['setup']} מניות פרצו והחזיקו, {c['today']} פרצו היום, ו-{c['touch']} נגעו בממוצע מלמטה."]))

    S = D["sectors"]
    if len(S) >= 3:
        scenes.append(dict(kind="sectors", title=f"{S[0][0]} ו{S[1][0]} מובילים", text=[
            f"הסקטור החזק היום הוא {S[0][0]}, עם {S[0][1]} אחוז מהמניות מעל הממוצעים, ואחריו {S[1][0]} עם {S[1][1]} אחוז.",
            f"בתחתית: {S[-1][0]}, עם {S[-1][1]} אחוז בלבד."]))

    T = D["top"]
    tt = []
    if T["setup"]:
        title = f"{len(T['setup'])} ענקיות פרצו והחזיקו" if len(T["setup"]) > 1 else f"{T['setup'][0]} פרצה והחזיקה"
        tt.append(f"בין הענקיות, {he_list(say(s) for s in T['setup'])} פרצו מעל שני הממוצעים והחזיקו.")
    else:
        title = "אין סט-אפ חדש בענקיות"
        tt.append(f"בין {T['n']} הגדולות בשוק אין היום סט-אפ חדש.")
    if T["today"]:
        tt.append(f"{he_list(say(s) for s in T['today'])} פרצו היום, ומחכות ליום אישור.")
    for s, lvl, p in T["touch"][:2]:
        tt.append(f"{say(s)} נגעה בממוצע של {lvl[3:]} יום מלמטה, ונסגרה {x_(abs(p))} אחוז מתחתיו.")
    tt.append(f"{T['above']} מתוך {T['n']} נמצאות מעל שני הממוצעים.")
    scenes.append(dict(kind="top", title=title, text=tt))

    if D["setups"]:
        strong = [x for x in D["setups"] if x["score"] >= 2]
        lead = D["setups"][0]
        st = []
        if strong:
            st.append(f"{len(strong)} פריצות קיבלו שני אישורים או יותר." if len(strong) > 1 else "פריצה אחת קיבלה שני אישורים.")
            title = f"{len(strong)} פריצות עם שני אישורים" if len(strong) > 1 else "פריצה עם שני אישורים"
        else:
            st.append(f"{c['setup']} מניות פרצו והחזיקו, אבל אף אחת עוד לא קיבלה שני אישורים.")
            title = "הפריצות המובילות"
        conf = he_list(CONF_SPOKEN[k] for k in lead["conf"] if k != "vol")
        st.append(f"{say(lead['sym'])} בולטת, עם מחזור של פי {x_(lead['vol'])} מהממוצע" + (f", ו{conf}." if lead["conf"] else "."))
        st.append(f"הסטופ שלה ב-{lead['stop']:.2f}, סיכון של {x_(lead['risk'])} אחוז.")
        if len(D["setups"]) > 1:
            st.append(f"ברשימה גם {he_list(uniq(say(x['sym']) for x in D['setups'][1:]))}.")
        scenes.append(dict(kind="setups", title=title, text=st, n=len(D["setups"])))

    if D["watch"] or D["touch"]:
        rt = []
        w = D["watch"][:2]
        if w:
            rt.append(f"למחר שימו לב {pre('ל', he_list(say(x['sym']) for x in w))}, שפרצו היום במחזור של פי {he_list(x_(x['vol']) for x in w)}.")
            rt.append("אם יחזיקו יום נוסף מעל שני הממוצעים, הן יהפכו לסט-אפ מלא.")
        if D["touch"]:
            rt.append(f"{'ו' if w else ''}בין המניות שנגעו בממוצע מלמטה: {he_list(uniq(say(x['sym']) for x in D['touch'])[:3])}.")
        scenes.append(dict(kind="radar", text=rt))

    scenes.append(dict(kind="outro", text=[
        "הדוח המלא מחכה לכם ב-PDF.",
        "זהו כלי סינון טכני בלבד, ולא ייעוץ השקעות. מסחר מוצלח."]))
    return scenes


# ---------------------------------------------------------------- audio
async def _tts(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def synth(scenes, work):
    ok = True
    for i, s in enumerate(scenes):
        s["clips"] = []
        for j, sent in enumerate(s["text"]):
            p = work / f"s{i}_{j}.mp3"
            if ok:
                for attempt in range(3):
                    try:
                        asyncio.run(_tts(sent, p))
                        if duration(p) > 0.3:
                            break
                    except Exception as e:
                        log(f"TTS נכשל ({attempt + 1}/3): {e}")
                else:
                    ok = False
                    log("ממשיך בלי קול, עם כתוביות בלבד")
            d = duration(p) if ok and p.exists() else max(1.8, len(sent) / 13.0)
            s["clips"].append((p if ok else None, d))
    return ok


def layout(scenes):
    """מחשב זמני התחלה לכל מקטע ולכל משפט (כתובית + קול)."""
    t = 0.0
    gap = 0.25
    for i, s in enumerate(scenes):
        s["id"] = f"sc{i}"
        s["start"] = t
        cur, caps = LEAD, []
        for (p, d), txt in zip(s["clips"], s["text"]):
            caps.append([round(cur, 2), round(cur + d + 0.15, 2), txt, p])
            cur += d + gap
        minimum = {"intro": 5.0, "outro": 5.0, "setups": 3.5 + 1.1 * s.get("n", 1)}.get(s["kind"], 6.0)
        s["dur"] = max(minimum, cur - gap + TAIL)
        if s["kind"] == "setups":
            s["step"] = min(1.1, max(0.4, (s["dur"] - 3) / max(s.get("n", 1), 1)))
        if s["kind"] == "radar":
            s["split"] = caps[2][0] if len(caps) > 2 else 2.6
        s["caps"] = caps
        t += s["dur"]
    return t


def build_audio(scenes, total, out):
    inputs, filters, k = [], [], 0
    for s in scenes:
        for c0, _, _, p in s["caps"]:
            if p is None:
                continue
            inputs += ["-i", str(p)]
            ms = int((s["start"] + c0) * 1000)
            filters.append(f"[{k}:a]aresample=44100,adelay={ms}|{ms}[a{k}]")
            k += 1
    if not k:
        return None
    mix = "".join(f"[a{i}]" for i in range(k)) + f"amix=inputs={k}:normalize=0:dropout_transition=0,apad,atrim=0:{total:.2f}[out]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters + [mix]),
                    "-map", "[out]", "-c:a", "aac", "-b:a", "160k", str(out)], check=True)
    return out


# ---------------------------------------------------------------- video
def render_frames(D, scenes, total, out_silent):
    from playwright.sync_api import sync_playwright
    js_scenes = [{k: v for k, v in s.items() if k not in ("clips", "text")} for s in scenes]
    for s in js_scenes:
        s["caps"] = [c[:3] for c in s["caps"]]
    init = f"window.DATA={json.dumps(D, ensure_ascii=False)};window.SCENES={json.dumps(js_scenes, ensure_ascii=False)};window.TOTAL={total:.3f};"
    n = int(total * FPS)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1920})
        pg.add_init_script(init)
        pg.goto((HERE / "news.html").as_uri())
        pg.evaluate("document.fonts.ready")
        ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS),
                               "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "27",
                               "-preset", "medium", str(out_silent)], stdin=subprocess.PIPE)
        for i in range(n):
            pg.evaluate(f"render({i / FPS})")
            ff.stdin.write(pg.screenshot(type="jpeg", quality=90))
            if i % 300 == 0:
                log(f"פריים {i}/{n}")
        ff.stdin.close()
        ff.wait()
        b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "Reports" / "video_data.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-voice", action="store_true")
    a = ap.parse_args()

    D = json.loads(Path(a.data).read_text(encoding="utf-8"))
    out = Path(a.out or ROOT / "out" / f"news_{D['date']}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = ROOT / "out" / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    scenes = build_script(D)
    voiced = False
    if a.no_voice:
        for s in scenes:
            s["clips"] = [(None, max(1.8, len(t) / 13.0)) for t in s["text"]]
    else:
        voiced = synth(scenes, work)
    total = layout(scenes)
    log(f"{len(scenes)} מקטעים, {total:.1f} שניות, קול: {'כן' if voiced else 'לא'}")
    (work / "script.txt").write_text("\n\n".join(" ".join(s["text"]) for s in scenes), encoding="utf-8")

    silent = work / "silent.mp4"
    render_frames(D, scenes, total, silent)
    audio = build_audio(scenes, total, work / "voice.m4a") if voiced else None
    if audio:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(audio), "-c:v", "copy",
                        "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)], check=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    (ROOT / "out" / "latest_video.txt").write_text(str(out), encoding="utf-8")
    log(f"מוכן: {out} ({out.stat().st_size / 1e6:.1f}MB)")


if __name__ == "__main__":
    main()
