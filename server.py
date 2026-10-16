#!/usr/bin/env python3
"""A/L Dashboard cloud: one process = web dashboard (Telegram Mini App) + Telegram bot.
Storage: Supabase (table app_data). Hosting: Railway.
ENV: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY (service_role), PUBLIC_URL (https://xxx.up.railway.app)
optional: BOT_TZ (Asia/Colombo), ANTHROPIC_API_KEY, OWNER_IDS (comma separated telegram ids), PORT
"""
import os, io, re, json, hmac, hashlib, html, uuid, random, asyncio, logging, time as _t
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta, timezone
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo
import httpx, uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont
from telegram import (Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, KeyboardButton,
                      ReplyKeyboardMarkup, WebAppInfo, MenuButtonWebApp)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters)

TOKEN = os.environ.get("BOT_TOKEN", "")
SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY", "")
PUBLIC = os.environ.get("PUBLIC_URL", "").rstrip("/")
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Colombo"))
OWNER_LIST = [int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]
OWNERS = set(OWNER_LIST)
SHARE = os.environ.get("SHARE_DATA", "0") == "1"   # 1 = all OWNER_IDS accounts use the SAME data (first id is the main one)
def did(uid): return OWNER_LIST[0] if SHARE and uid in OWNERS else uid
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the bot token out of the logs
log = logging.getLogger("al"); H = ParseMode.HTML
HC: httpx.AsyncClient = None
HERE = os.path.dirname(os.path.abspath(__file__))
DEFSYL = json.load(open(os.path.join(HERE, "defsyl.json"), encoding="utf8"))
MK_DEF = {"Physics": {"DU Paper": [], "SR Paper": [], "Past Papers": []}, "Chemistry": {"CD Paper": [], "Past Papers": []},
          "Pure": {"RD Paper": [], "Aja Paper": [], "Past Papers": []}, "Applied": {"Aja Paper": [], "RD Paper": [], "Past Papers": []}}

# ============================================================ Supabase (REST)
def _h(extra=None): return {"apikey": KEY, "Authorization": f"Bearer {KEY}", **(extra or {})}
async def db_load(uid):
    r = await HC.get(f"{SB}/rest/v1/app_data", params={"tg_id": f"eq.{uid}", "select": "k,v"}, headers=_h())
    r.raise_for_status(); return {x["k"]: x["v"] for x in r.json()}
async def db_put(uid, items):
    if not items: return
    rows = [{"tg_id": uid, "k": k, "v": v, "updated_at": datetime.now(timezone.utc).isoformat()} for k, v in items.items()]
    r = await HC.post(f"{SB}/rest/v1/app_data", params={"on_conflict": "tg_id,k"}, content=json.dumps(rows),
                      headers=_h({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}))
    if r.status_code in (401, 403):
        raise RuntimeError("Supabase refused the save (%s). SUPABASE_KEY must be the service_role / secret key, NOT the anon / publishable key. %s" % (r.status_code, r.text[:200]))
    r.raise_for_status()
async def db_metas():
    r = await HC.get(f"{SB}/rest/v1/app_data", params={"k": "eq._meta", "select": "tg_id,v"}, headers=_h())
    r.raise_for_status(); return {x["tg_id"]: x["v"] for x in r.json()}


# ============================================================ AI provider (Gemini free tier or Anthropic)
def ai_on(): return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
async def llm(system, msgs):
    msgs = list(msgs)
    while msgs and msgs[0]["role"] != "user": msgs.pop(0)
    gk = os.environ.get("GEMINI_API_KEY")
    if gk:
        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        body = {"contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in msgs],
                "generationConfig": {"maxOutputTokens": 2000}}
        if system: body["systemInstruction"] = {"parts": [{"text": system}]}
        r = await HC.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", headers={"x-goog-api-key": gk}, json=body, timeout=60)
        if r.status_code >= 400: raise RuntimeError(f"gemini {r.status_code} {r.text[:300]}")
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        out = "".join(p.get("text", "") for p in parts)
        if not out: raise RuntimeError("gemini empty reply: " + r.text[:300])
        return out
    r = await HC.post("https://api.anthropic.com/v1/messages", timeout=60,
                      headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": AI_MODEL, "max_tokens": 1000, "system": system or "You are a helpful study tutor.", "messages": msgs})
    if r.status_code >= 400: raise RuntimeError(f"anthropic {r.status_code} {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

# ============================================================ Telegram Mini App auth
def verify(init: str) -> int:
    d = dict(parse_qsl(init, keep_blank_values=True)); h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h): raise ValueError("bad hash")
    if _t.time() - int(d.get("auth_date", "0")) > 3 * 86400: raise ValueError("expired")
    uid = int(json.loads(d["user"])["id"])
    if OWNERS and uid not in OWNERS: raise ValueError("not allowed")
    return did(uid)

# ============================================================ dashboard data (same keys as the web app)
SK, MK, PK = "ghostAppElite_V3_StandardBlue", "ghostMarks_V3", "ghostUltimateDb_V4"
SUBJ = {"Physics": "Physics", "Chemistry": "Chemistry", "Pure": "Pure Maths", "Applied": "Applied Maths", "General": "General"}
STUDY = ["Physics", "Chemistry", "Pure", "Applied"]
HAB_DEF = [["h1", "Wake up at 5:30 AM, no excuses"], ["h2", "1 timed past paper session"], ["h3", "1.5 hours of AI & Python skill work"],
           ["h4", "Zero dopamine and distractions"], ["h5", "English journaling at night"], ["h6", "Review yesterday's mistakes"]]
RM = [("Foundation", "Now"), ("Programming basics", "Next 90 days"), ("Math for ML", "Year 1-2"), ("University degree", "Year 2-5"),
      ("Data engineering", "Year 3-5"), ("Machine learning", "Year 4+"), ("AI scientist", "Goal")]
DR = [("🥇", "A/L 1st"), ("🎓", "Faculty of CSE"), ("✈️", "Australia"), ("🤖", "AI scientist"), ("⛓️", "Blockchain & investing"),
      ("💰", "Financial freedom"), ("🏠", "Luxury life")]
QUOTES = ["Discipline is choosing between what you want now and what you want most.", "Dream big, work hard, and stay focused. Success will follow.",
          "I never give up.", "Own every single day.", "දිනලා විතරක් ඉවර කරන්න.", "Small steps every day beat big plans never started."]
AI_SYS = ("You are a friendly study coach and tutor for a Sri Lankan A/L student (Combined Maths, Physics, Chemistry, plus Python/AI). "
          "ALWAYS reply in Sinhala (technical terms may stay in English). Be concise, explain step by step, encourage discipline.")

def now(): return datetime.now(TZ)
def today(): return now().date()
def ds(d): return d.isoformat()
def esc(x): return html.escape(str(x))
def uid8(): return uuid.uuid4().hex[:8]
def habs(D): return D.get("habits") or HAB_DEF
def hlog(D): return D.setdefault("hlog", {})
def day_pct(D, d):
    ids = [h[0] for h in habs(D)]
    return len([i for i in hlog(D).get(d, []) if i in ids]) / len(ids) if ids else 0
def streak(D):
    d, st = today(), 0
    if day_pct(D, ds(d)) < .7: d -= timedelta(1)
    while st < 400 and day_pct(D, ds(d)) >= .7: st += 1; d -= timedelta(1)
    return st
def study_on(D, d):
    day = (D.get(SK) or {}).get(f"{d.year}-{d.month - 1}", {}).get(str(d.day), {})
    return sum(sum((day.get(t) or {}).values()) for t in ("SelfStudy", "Classes"))
def month_hours(D):
    t = today(); mo = (D.get(SK) or {}).get(f"{t.year}-{t.month - 1}", {})
    return sum(sum(sum((v.get(x) or {}).values()) for x in ("SelfStudy", "Classes")) for v in mo.values())
def add_study(D, subj, h, kind="SelfStudy"):
    t = today(); sdb = D.setdefault(SK, {}); day = sdb.setdefault(f"{t.year}-{t.month - 1}", {}).setdefault(str(t.day), {})
    s = day.setdefault(kind, {}); s[subj] = round(s.get(subj, 0) + h, 2)
def best_mark(D):
    b = 0
    for ser in (D.get(MK) or {}).values():
        for rows in ser.values():
            for r in rows:
                try: b = max(b, float(r.get("final") or 0))
                except Exception: pass
    return b
def mdb(D):
    if MK not in D: D[MK] = json.loads(json.dumps(MK_DEF))
    return D[MK]
def pdb(D):
    if PK not in D: D[PK] = json.loads(json.dumps(DEFSYL))
    p = D[PK]
    for k in STUDY:
        e = p.setdefault(k, {"topics": [], "papers": {}})
        if not e.get("topics"): e["topics"] = json.loads(json.dumps(DEFSYL[k]["topics"]))
        if not e.get("papers"): e["papers"] = {str(y): [False] * 10 for y in range(2025, 1999, -1)}
    return p
def cnt(rounds): return sum(1 for r in rounds if r)
def dleft(s): return (date.fromisoformat(s) - today()).days
def when(s):
    if not s: return "no date"
    n = dleft(s); return f"overdue {-n}d" if n < 0 else "TODAY" if n == 0 else "tomorrow" if n == 1 else f"{n} days left"
def is_exam(t): return bool(re.search(r"exam|test|paper|mock|quiz", t, re.I))
def exam_dt(D):
    try: return datetime.combine(date.fromisoformat(D.get("exam", "2027-08-02")), time(8, 30), tzinfo=TZ)
    except Exception: return datetime(2027, 8, 2, 8, 30, tzinfo=TZ)
def pdate(s):
    s = s.strip().lower(); t = today()
    if s in ("", "-", "none"): return ""
    if s == "today": return ds(t)
    if s in ("tomorrow", "tmrw"): return ds(t + timedelta(1))
    if re.fullmatch(r"\+\d+", s): return ds(t + timedelta(int(s)))
    for f in ("%Y-%m-%d", "%m-%d", "%d/%m"):
        try:
            d = datetime.strptime(s, f).date()
            if f != "%Y-%m-%d":
                d = d.replace(year=t.year)
                if d < t: d = d.replace(year=t.year + 1)
            return ds(d)
        except ValueError: pass
    raise ValueError
def skey(s):
    s = s.strip().lower()
    for k, v in SUBJ.items():
        if s in (k.lower(), v.lower()) or v.lower().startswith(s): return k
    return "General"
def bar(p, n=10):
    f = max(0, min(n, round(p * n))); return "█" * f + "░" * (n - f)

# ============================================================ countdown banner (picture)
def banner(D):
    ex = exam_dt(D); sec = max(0, int((ex - now()).total_seconds())); days = sec // 86400
    vals = [("MONTHS", days // 30), ("WEEKS", days // 7), ("HOURS", sec // 3600 % 24), ("MINS", sec // 60 % 60)]
    prog = max(0, min(1, 1 - days / 365))
    W, Hh = 900, 420; im = Image.new("RGB", (W, Hh)); dr = ImageDraw.Draw(im)
    for y in range(Hh):
        t = y / Hh; dr.line([(0, y), (W, y)], fill=(int(16 + 34 * t), int(22 + 26 * t), int(54 + 14 * t)))
    F = lambda s: ImageFont.load_default(size=s)
    dr.text((40, 28), f"A/L {ex.year} COUNTDOWN", font=F(34), fill=(250, 204, 21))
    dr.text((40, 80), str(days), font=F(180), fill=(255, 255, 255))
    dr.text((60 + dr.textlength(str(days), font=F(180)), 190), "DAYS", font=F(56), fill=(148, 163, 184))
    for i, (lab, v) in enumerate(vals):
        x = 40 + i * 210
        dr.rounded_rectangle((x, 290, x + 190, 372), 16, fill=(30, 41, 75), outline=(99, 102, 241), width=2)
        dr.text((x + 95, 316), str(v), font=F(42), fill="white", anchor="mm"); dr.text((x + 95, 355), lab, font=F(18), fill=(148, 163, 184), anchor="mm")
    dr.rounded_rectangle((40, 394, 860, 406), 6, fill=(40, 50, 80)); dr.rounded_rectangle((40, 394, 40 + max(12, int(820 * prog)), 406), 6, fill=(250, 204, 21))
    b = io.BytesIO(); im.save(b, "PNG"); b.seek(0); return b, days, vals

# ============================================================ pages (text, keyboard)
def grid(rows, per=3): return [rows[i:i + per] for i in range(0, len(rows), per)]
def home_btn(): return [B("🏠 මුල් පිටුව", callback_data="go:home")]
def web_kb():
    return M(([[B("🌐 Open Web App", web_app=WebAppInfo(PUBLIC + "/"))]] if PUBLIC else []) + [home_btn()])
def menu_kb():
    items = [("✅ පුරුදු", "habits"), ("📅 සැලසුම", "planner"), ("📚 අධ්‍යයන", "study"), ("🧠 වැරදි", "mist"), ("⏱️ Focus", "focus"),
             ("🎯 ඉලක්ක", "targets"), ("📝 Papers", "papers"), ("📈 ලකුණු", "marks"), ("🗓️ දින 90", "plan"), ("🧭 Roadmap", "roadmap"),
             ("🌠 සිහින", "dreams"), ("💡 දුර්වලතා", "ls"), ("🤖 AI", "ai")]
    rows = [[B("🌐 Website (Full Dashboard)", web_app=WebAppInfo(PUBLIC + "/"))]] if PUBLIC else []
    return M(rows + grid([B(l, callback_data="go:" + p) for l, p in items]))
def reply_kb(meta):
    web = KeyboardButton("🧑‍💻 Web APP ✅", web_app=WebAppInfo(PUBLIC + "/")) if PUBLIC else KeyboardButton("🧑‍💻 Web APP ✅")
    return ReplyKeyboardMarkup([[KeyboardButton("🕐 Count Down 🍀"), web],
                                [KeyboardButton("🔔 Notifications: ON" if meta.get("notif", True) else "🔕 Notifications: OFF")]], resize_keyboard=True)

def home_caption(D):
    n = now(); g = "🌅 Good Morning" if n.hour < 12 else "☀️ Good Afternoon" if n.hour < 17 else "🌙 Good Evening"
    ex = exam_dt(D); sec = max(0, int((ex - n).total_seconds())); d = sec // 86400; t = ds(today())
    out = [f"{g} 🌄", "", "<b>Daily Quote 💬</b>", f"<i>{random.choice(QUOTES)}</i>", "",
           f"🎓 {ex.year} A/L විභාගයට තව,", f"🔥 මාස <b>{d // 30}</b>", f"📚 සති <b>{d // 7}</b>", f"🔴 දින <b>{d}</b>",
           f"⏳ පැය <b>{sec // 3600 % 24}</b>", f"⚡ මිනිත්තු <b>{sec // 60 % 60}</b>", "", f"📅 අද Date: {t}", "",
           f"📖 {bar(study_on(D, today()) / (float(D.get('goalH', 6)) or 6), 8)} {study_on(D, today()):.1f}/{float(D.get('goalH', 6)):g}h · ✅ {round(day_pct(D, t) * 100)}% · 🔥 {streak(D)}"]
    op = sorted([x for x in D.get("tasks", []) if not x.get("done")], key=lambda x: x.get("d") or "9")[:3]
    for x in op: out.append(f"{'🎓' if is_exam(x['t']) else '📌'} {esc(x['t'][:30])} · {when(x.get('d'))}")
    return "\n".join(out)[:1000]

def p_habits(D, arg=None):
    t = ds(today()); dn = set(hlog(D).get(t, [])); hs = habs(D); p = day_pct(D, t)
    kb = [[B(("✅ " if h[0] in dn else "⬜ ") + h[1][:45], callback_data=f"hb:{h[0]}")] for h in hs]
    return f"✅ <b>Daily discipline</b>\n{bar(p)} <b>{round(p * 100)}%</b>\n🔥 Streak {streak(D)}", M(kb + [home_btn()])

def p_planner(D, arg=None):
    L = sorted(D.get("tasks", []), key=lambda x: (x.get("done"), x.get("d") or "9")); op = [x for x in L if not x.get("done")]
    txt = f"📅 <b>Planner</b>  open {len(op)}\n"; kb = []
    for x in L[:12]:
        txt += f"\n{'✔' if x.get('done') else ('🎓' if is_exam(x['t']) else '📌')} {esc(x['t'])} — <i>{SUBJ.get(x.get('s'), 'General')} · {when(x.get('d'))}</i>"
        kb.append([B(("↩️ " if x.get("done") else "✔ ") + x["t"][:24], callback_data=f"tk:{x['id']}"), B("🗑", callback_data=f"tx:{x['id']}")])
    if not L: txt += "\nNo tasks yet."
    txt += "\n\n➕ Add: <code>title | date | subject</code>\nexample: <code>Physics mock exam | 2026-11-02 | physics</code>"
    return txt, M(kb + [[B("➕ Add task / exam", callback_data="ta")], home_btn()])

def p_study(D, arg=None):
    t = today(); lines = [f"{(t - timedelta(i)).strftime('%m-%d')} {bar(study_on(D, t - timedelta(i)) / (float(D.get('goalH', 6)) or 6), 8)} {study_on(D, t - timedelta(i)):.1f}h" for i in range(6, -1, -1)]
    txt = (f"📚 <b>Study log</b>\nToday <b>{study_on(D, t):.1f}h</b> · Month <b>{month_hours(D):.1f}h</b>\n\n<code>" + "\n".join(lines) +
           "</code>\n\nTap a subject, then send hours (e.g. <code>1.5</code> or <code>2 class</code>)")
    return txt, M(grid([B(SUBJ[k], callback_data=f"lg:{k}") for k in STUDY], 2) + [home_btn()])

def p_mist(D, arg=None):
    L = sorted(D.get("mist", []), key=lambda m: bool(m.get("r")))[:8]
    txt = f"🧠 <b>Mistakes</b>  {len(D.get('mist', []))} total\n"; kb = []
    for m in L:
        txt += f"\n{'✔' if m.get('r') else '•'} <b>{esc(m['q'])}</b> [{SUBJ.get(m.get('s'), '')}]\n   ❌ {esc(m.get('w', '-'))}\n   ✅ {esc(m.get('c', '-'))}\n"
        kb.append([B(("↩️ " if m.get("r") else "✔ ") + m["q"][:22], callback_data=f"ms:{m['id']}"), B("🗑", callback_data=f"mx:{m['id']}")])
    if not L: txt += "\nNo mistakes yet."
    return txt, M(kb + [[B("➕ Add", callback_data="ma")], home_btn()])

def p_focus(D, arg=None, fs="Physics", active=None):
    txt = f"⏱️ <b>Focus timer</b>\nSubject: <b>{SUBJ[fs]}</b>\nFinished sessions are added to your study log automatically."
    if active: txt += f"\n\n🔴 Running: {SUBJ[active['s']]} {active['m']} min"
    kb = grid([B(("● " if k == fs else "") + SUBJ[k], callback_data=f"fs:{k}") for k in STUDY], 2)
    kb.append([B(f"▶ {m} min", callback_data=f"fg:{m}") for m in (25, 45, 60, 90)])
    if active: kb.append([B("⏹ Cancel", callback_data="fc")])
    return txt, M(kb + [home_btn()])

def p_targets(D, arg=None):
    tg = D.get("tgt") or {"short": [], "long": []}; txt = "🎯 <b>Targets</b>\n"; kb = []
    for w, lab in (("short", "⚡ Short-term"), ("long", "🌟 Long-term")):
        txt += f"\n<b>{lab}</b>\n" + ("\n".join(f"{x.get('em', '🎯')} {esc(x.get('t', ''))}" for x in tg.get(w, [])) or "—") + "\n"
        kb.append([B(f"➕ {w}", callback_data=f"tga:{w}")])
    return txt, M(kb + [home_btn()])

def p_papers(D, arg=None):
    db = pdb(D); txt = "📝 <b>Past papers & Syllabus</b>\n"; kb = []
    for k in STUDY:
        sub = [s for t in db[k]["topics"] for s in t.get("subTopics", [])]; done = sum(cnt(s.get("rounds", [])) for s in sub); mx = 10 * len(sub)
        pp = sum(cnt(r) for r in db[k]["papers"].values())
        txt += f"\n<b>{SUBJ[k]}</b>  {bar(done / mx if mx else 0)} {round(done / mx * 100) if mx else 0}% · paper rounds {pp}"
        kb.append([B(f"📄 {SUBJ[k]} Papers", callback_data=f"pp:{k}"), B(f"📚 {SUBJ[k]} Syllabus", callback_data=f"sy:{k}")])
    return txt, M(kb + [home_btn()])

def p_marks(D, arg=None):
    txt = f"📈 <b>ලකුණු</b> (target 95)\n🏆 Best paper mark: <b>{best_mark(D):g}</b>\n"
    for k in STUDY:
        vals = []
        for rows in mdb(D).get(k, {}).values():
            for r in rows:
                try: vals.append(float(r.get("final") or 0))
                except Exception: pass
        if vals: txt += f"\n• {SUBJ[k]}: best {max(vals):g} · avg {sum(vals) / len(vals):.0f} ({len(vals)} papers)"
    return txt + "\n\nවිෂයක් තෝරලා ලකුණු ඇතුළත් කරන්න 👇", M(grid([B("➕ " + SUBJ[k], callback_data=f"mk:{k}") for k in STUDY], 2) + [home_btn()])

def p_plan(D, arg=None):
    pl = D.get("plan") or {}; st = pl.get("start") or ds(today()); s = date.fromisoformat(st); t = today(); k = (t - s).days + 1; cells = []
    for i in range(90):
        d = s + timedelta(i); p = day_pct(D, ds(d)); cells.append("⬜" if d > t else "🟩" if p >= .7 else "🟨" if p > 0 else "🟥")
    g = "\n".join("".join(cells[i:i + 10]) for i in range(0, 90, 10))
    return (f"🗓️ <b>දින 90 tracker</b>  Day <b>{min(max(k, 1), 90)}/90</b>  (start {st})\n{g}\n🟩 ≥70%  🟨 some  🟥 missed\n🔥 Streak {streak(D)}",
            M([[B("🚀 අද සිට අලුතින් පටන් ගන්න", callback_data="s90")], home_btn()]))

def p_ls(D, arg=None):
    ls = D.get("ls") or {"weak": ["", "", "", ""], "mind": ["I am turning past failure into power.", "", ""]}
    txt = ("💡 <b>දුර්වලතා & Mindset</b>\n\n<b>Weak points</b>\n" + "\n".join(f"{i + 1}. {esc(x) or '—'}" for i, x in enumerate(ls["weak"])) +
           "\n\n<b>Mindset</b>\n" + "\n".join(f"{i + 1}. {esc(x) or '—'}" for i, x in enumerate(ls["mind"])))
    return txt, M([[B("✏️ සංස්කරණය", callback_data="lse")], home_btn()])

def p_roadmap(D, arg=None):
    rd = D.get("rd", [0]); txt = "🧭 <b>Roadmap</b> (tap when reached)\n" + "\n".join(f"{'✅' if i in rd else '⬜'} {a} <i>{b}</i>" for i, (a, b) in enumerate(RM))
    return txt, M(grid([B(("✅ " if i in rd else "") + RM[i][0][:16], callback_data=f"rd:{i}") for i in range(7)], 2) + [home_btn()])

def p_dreams(D, arg=None):
    w = D.get("won", []); txt = f"🌠 <b>Dream board</b> {len(w)}/{len(DR)} won\n<i>MIND IT!</i>\n" + "\n".join(f"{e} {t}{' 🏆' if i in w else ''}" for i, (e, t) in enumerate(DR))
    return txt, M(grid([B(("🏆 " if i in w else DR[i][0] + " ") + DR[i][1][:16], callback_data=f"dr:{i}") for i in range(7)], 2) + [home_btn()])

def p_ai(D, arg=None):
    ok = ai_on()
    return ("🤖 <b>Study AI</b>\nJust type any question (Sinhala / Singlish / English)." if ok else "🤖 Study AI is off (set GEMINI_API_KEY)."), M([home_btn()])

PAGES = dict(plan=p_plan, ls=p_ls, habits=p_habits, planner=p_planner, study=p_study, mist=p_mist, targets=p_targets, papers=p_papers,
             marks=p_marks, roadmap=p_roadmap, dreams=p_dreams, ai=p_ai)

# ============================================================ bot plumbing
async def ctx_data(update):
    u = update.effective_user
    if OWNERS and u.id not in OWNERS:
        await update.effective_message.reply_text("🔒 Private bot."); return None, None
    return did(u.id), await db_load(did(u.id))
async def meta_get(uid, D=None):
    D = D if D is not None else await db_load(uid); return D.get("_meta") or {}
async def meta_set(uid, chat, **kw):
    m = await meta_get(uid); m.update(chat=chat, **kw); m["chats"] = sorted(set(m.get("chats", [])) | {chat}); await db_put(uid, {"_meta": m}); return m

async def send_home(chat, bot, D, uid=None):
    img, _, _ = banner(D); m = await meta_get(uid, D) if uid else {}
    await bot.send_photo(chat, img, caption=home_caption(D), parse_mode=H, reply_markup=menu_kb())

async def cmd_start(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    meta = await meta_set(uid, update.effective_chat.id, **({} if D.get("_meta") else dict(am="05:30", pm="21:30", notif=True)))
    schedule(ctx.application, uid, meta)
    await update.effective_message.reply_text("👋 Welcome! Your bot and the Web App share the same data — change in one, it updates in the other.",
                                              reply_markup=reply_kb(meta))
    await send_home(update.effective_chat.id, ctx.bot, D, uid)
async def cmd_menu(update, ctx):
    uid, D = await ctx_data(update)
    if uid: await send_home(update.effective_chat.id, ctx.bot, D, uid)
async def cmd_exam(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    try: date.fromisoformat(ctx.args[0]); await db_put(uid, {"exam": ctx.args[0]}); D["exam"] = ctx.args[0]; await send_home(update.effective_chat.id, ctx.bot, D, uid)
    except Exception: await update.effective_message.reply_text("Usage: /exam 2027-08-02")
async def cmd_goal(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    try: g = max(.5, min(16, float(ctx.args[0]))); await db_put(uid, {"goalH": g}); await update.effective_message.reply_text(f"🎯 Daily goal {g:g}h")
    except Exception: await update.effective_message.reply_text("Usage: /goal 6")
async def cmd_remind(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    try:
        for t in ctx.args[:2]: datetime.strptime(t, "%H:%M")
        meta = await meta_set(uid, update.effective_chat.id, am=ctx.args[0], pm=ctx.args[1] if len(ctx.args) > 1 else (D.get("_meta") or {}).get("pm", "21:30"))
        schedule(ctx.application, uid, meta); await update.effective_message.reply_text(f"⏰ Morning {meta['am']} · Night {meta['pm']}")
    except Exception: await update.effective_message.reply_text("Usage: /remind 05:30 21:30")
async def cmd_log(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    try:
        h = float(ctx.args[1]); assert 0 < h <= 16; s = skey(ctx.args[0]); s = s if s in STUDY else "Physics"
        add_study(D, s, h, "Classes" if "class" in " ".join(ctx.args[2:]).lower() else "SelfStudy"); await db_put(uid, {SK: D[SK]})
        await update.effective_message.reply_text(f"✅ +{h:g}h {SUBJ[s]} · today {study_on(D, today()):.1f}h")
    except Exception: await update.effective_message.reply_text("Usage: /log physics 1.5")
async def cmd_ai(update, ctx):
    q = update.effective_message.text.partition(" ")[2]
    if q: await ai_reply(update, ctx, q)
async def cmd_cancel(update, ctx): ctx.user_data.pop("await", None); await update.effective_message.reply_text("Cancelled.")

async def show(q, text, kb):
    m = q.message
    try:
        if m.photo:
            try: await m.delete()
            except Exception: pass
            await m.chat.send_message(text, reply_markup=kb, parse_mode=H)
        else: await q.edit_message_text(text, reply_markup=kb, parse_mode=H)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): log.warning(e)

# ---- free text
async def on_text(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    text = (update.effective_message.text or "").strip(); chat = update.effective_chat.id
    if text.startswith("🕐 Count Down"): return await send_home(chat, ctx.bot, D, uid)
    if text.startswith(("🔔 Notifications", "🔕 Notifications")):
        meta = await meta_set(uid, chat, notif=not (D.get("_meta") or {}).get("notif", True))
        return await update.effective_message.reply_text("🔔 Notifications ON" if meta["notif"] else "🔕 Notifications OFF", reply_markup=reply_kb(meta))
    st = ctx.user_data.pop("await", None)
    if not st: return await ai_reply(update, ctx, text)
    k = st[0]; r = update.effective_message.reply_text
    try:
        if k == "log":
            h = float(text.split()[0]); assert 0 < h <= 16
            add_study(D, st[1], h, "Classes" if "class" in text.lower() else "SelfStudy"); await db_put(uid, {SK: D[SK]})
            await r(f"✅ +{h:g}h {SUBJ[st[1]]} · today {study_on(D, today()):.1f}h"); t, kb = p_study(D)
        elif k == "task":
            p = [x.strip() for x in text.split("|")]; d = pdate(p[1]) if len(p) > 1 else ""
            D.setdefault("tasks", []).append(dict(id=uid8(), t=p[0], d=d, s=skey(p[2]) if len(p) > 2 else "General", done=False)); await db_put(uid, {"tasks": D["tasks"]}); t, kb = p_planner(D)
        elif k == "mist":
            p = [x.strip() for x in text.split("|")]; assert len(p) >= 2
            D.setdefault("mist", []).insert(0, dict(id=uid8(), s=skey(p[0]), q=p[1], w=p[2] if len(p) > 2 else "-", c=p[3] if len(p) > 3 else "-", r=False)); await db_put(uid, {"mist": D["mist"]}); t, kb = p_mist(D)
        elif k == "mark":
            p = [x.strip() for x in text.split("|")]; f = float(p[1]); assert 0 <= f <= 100
            g = lambda i: p[i] if len(p) > i else ""; rec = dict(name=p[0] or "Paper", round=g(2) or "1", final=f, mcq=g(3), seq=g(4), essay=g(5), partA="", partB="")
            arr = mdb(D)[st[1]].setdefault(st[2], []); ix = next((i for i, x in enumerate(arr) if x.get("name") == rec["name"] and str(x.get("round")) == rec["round"]), -1)
            arr[ix] = rec if ix > -1 else arr.append(rec) or rec; arr.sort(key=lambda x: float(x.get("round") or 1)); await db_put(uid, {MK: D[MK]}); await r(f"✅ {rec['name']} · {f:g} සුරැකුණා"); t, kb = p_marks(D)
        elif k == "ls":
            m_ = re.match(r"(weak|mind)\s*(\d)\s*\|\s*(.*)", text, re.I | re.S); assert m_
            ls = D.get("ls") or {"weak": ["", "", "", ""], "mind": ["I am turning past failure into power.", "", ""]}; i = int(m_.group(2)) - 1; assert 0 <= i < len(ls[m_.group(1).lower()])
            ls[m_.group(1).lower()][i] = m_.group(3).strip(); D["ls"] = ls; await db_put(uid, {"ls": ls}); t, kb = p_ls(D)
        elif k == "tgt":
            tg = D.get("tgt") or {"short": [], "long": []}; tg[st[1]].append(dict(id=uid8(), em="🎯", t=text, d="", img="")); D["tgt"] = tg; await db_put(uid, {"tgt": tg}); t, kb = p_targets(D)
        await r(t, reply_markup=kb, parse_mode=H)
    except Exception:
        ctx.user_data["await"] = st
        await r({"log": "පැය ගණන එවන්න, උදා: 1.5 (නැවැත්තුවොත් /cancel)", "task": "Format: නම | දිනය | විෂය  (දිනය: 2026-11-02, tomorrow, +3)", "mark": "Format: නම | අවසාන ලකුණු | වටය | MCQ | SEQ | Essay  (නම + ලකුණු ඇති)", "ls": "Format: weak 2 | text  හෝ  mind 1 | text", "mist": "Format: විෂය | මාතෘකාව | වැරදුණු විදිය | නිවැරදි විදිය"}.get(k, "Try again (/cancel)"))

async def ai_reply(update, ctx, text):
    if not ai_on(): return await update.effective_message.reply_text("Use the buttons 👇 /menu  (AI off: set GEMINI_API_KEY on Railway)")
    await update.effective_chat.send_action("typing"); h = ctx.user_data.setdefault("ai", []); h.append({"role": "user", "content": text}); del h[:-8]
    try:
        ans = await llm(AI_SYS, h); h.append({"role": "assistant", "content": ans}); await update.effective_message.reply_text(ans[:4000])
    except Exception as e: log.exception(e); await update.effective_message.reply_text("⚠️ AI error")

# ---- focus timer
async def focus_done(ctx):
    uid, f = ctx.job.data; D = await db_load(uid); fl = D.get("focus") or {}
    add_study(D, f["s"], round(f["m"] / 60, 2)); t = fl.setdefault(ds(today()), {"n": 0, "m": 0}); t["n"] += 1; t["m"] += f["m"]
    meta = D.get("_meta") or {}; meta.pop("focus", None)
    await db_put(uid, {SK: D[SK], "focus": fl, "_meta": meta})
    await ctx.bot.send_message(f.get("chat") or meta.get("chat", uid), f"🎉 Focus done! +{f['m']} min {SUBJ[f['s']]} added to your study log. 5 min break 🧘")

# ---- callbacks
async def on_cb(update, ctx):
    q = update.callback_query; await q.answer(); raw = q.from_user.id
    if OWNERS and raw not in OWNERS: return
    uid = did(raw)
    D = await db_load(uid); p = q.data.split(":"); c = p[0]; t = ds(today()); app = ctx.application
    async def page(name, **kw):
        if name == "home":
            img, _, _ = banner(D)
            try: await q.message.delete()
            except Exception: pass
            return await q.message.chat.send_photo(img, caption=home_caption(D), parse_mode=H, reply_markup=menu_kb())
        if name == "focus":
            meta = D.get("_meta") or {}; tx, kb = p_focus(D, fs=ctx.user_data.get("fsub", "Physics"), active=meta.get("focus"))
        else: tx, kb = PAGES[name](D)
        await show(q, tx, kb)
    async def ask(state, prompt): ctx.user_data["await"] = state; await q.message.reply_text(prompt, parse_mode=H)
    if c == "go": return await page(p[1])
    if c == "hb":
        d = hlog(D).setdefault(t, []); d.remove(p[1]) if p[1] in d else d.append(p[1])
        D["bestStreak"] = max(int(D.get("bestStreak", 0)), streak(D)); await db_put(uid, {"hlog": D["hlog"], "bestStreak": D["bestStreak"]}); return await page("habits")
    if c == "tk":
        for x in D.get("tasks", []):
            if x["id"] == p[1]: x["done"] = not x.get("done")
        await db_put(uid, {"tasks": D.get("tasks", [])}); return await page("planner")
    if c == "tx": D["tasks"] = [x for x in D.get("tasks", []) if x["id"] != p[1]]; await db_put(uid, {"tasks": D["tasks"]}); return await page("planner")
    if c == "ta": return await ask(("task",), "📅 මෙහෙම එවන්න:\n<code>නම | දිනය | විෂය</code>\nඋදා: <code>Chemistry exam | 2026-11-10 | chemistry</code>\n(දිනය: 2026-11-10 හෝ tomorrow හෝ +3)")
    if c == "lg": return await ask(("log", p[1]), f"⏱️ <b>{SUBJ[p[1]]}</b> පැය කීයක් කළාද?\nඋදා: <code>1.5</code> (Classes නම් <code>2 class</code>)")
    if c == "ma": return await ask(("mist",), "🧠 මෙහෙම එවන්න:\n<code>විෂය | මාතෘකාව | වැරදුණු විදිය | නිවැරදි විදිය</code>\nඋදා: <code>physics | SHM | sign වැරදුණා | -w²x පාවිච්චි කරන්න</code>")
    if c == "ms":
        for m in D.get("mist", []):
            if m["id"] == p[1]: m["r"] = not m.get("r")
        await db_put(uid, {"mist": D.get("mist", [])}); return await page("mist")
    if c == "mx": D["mist"] = [m for m in D.get("mist", []) if m["id"] != p[1]]; await db_put(uid, {"mist": D["mist"]}); return await page("mist")
    if c == "tga": return await ask(("tgt", p[1]), f"🎯 ඔයාගේ {'short' if p[1]=='short' else 'long'}-term ඉලක්කය එවන්න:")
    if c == "rd":
        rd = D.get("rd", [0]); i = int(p[1]); rd.remove(i) if i in rd else rd.append(i); D["rd"] = rd; await db_put(uid, {"rd": rd}); return await page("roadmap")
    if c == "dr":
        w = D.get("won", []); i = int(p[1])
        if i in w: w.remove(i)
        else: w.append(i); await q.message.reply_text("🎉🏆 DREAM WON! " + DR[i][1])
        D["won"] = w; await db_put(uid, {"won": w}); return await page("dreams")
    if c == "mk": return await show(q, f"📈 <b>{SUBJ[p[1]]}</b> — Paper series එක තෝරන්න", M(grid([B(n, callback_data=f"mks:{p[1]}:{i}") for i, n in enumerate(mdb(D)[p[1]])], 2) + [home_btn()]))
    if c == "mks":
        se = list(mdb(D)[p[1]])[int(p[2])]
        return await ask(("mark", p[1], se), f"📈 <b>{SUBJ[p[1]]} · {se}</b>\nමෙහෙම එවන්න:\n<code>පේපර් නම | අවසාන ලකුණු | වටය | MCQ | SEQ | Essay</code>\nඋදා: <code>2019 Paper | 72 | 1 | 28 | 30 | 14</code>\n(නම සහ ලකුණු විතරක් ඇති)")
    if c == "pp":
        yrs = pdb(D)[p[1]]["papers"]
        return await show(q, f"📄 <b>{SUBJ[p[1]]} Past papers</b>\nවර්ෂයක් තෝරන්න (· පසු අංකය = round ගණන)", M(grid([B(f"{y}·{cnt(v_)}", callback_data=f"py:{p[1]}:{y}") for y, v_ in yrs.items()], 4) + [[B("⬅️", callback_data="go:papers")]]))
    if c in ("py", "pr"):
        v_ = pdb(D)[p[1]]["papers"].setdefault(p[2], [False] * 10)
        if c == "pr": v_[int(p[3])] = not v_[int(p[3])]; await db_put(uid, {PK: D[PK]})
        kb = [[B(("✅" if v_[i] else "") + str(i + 1), callback_data=f"pr:{p[1]}:{p[2]}:{i}") for i in range(j, j + 5)] for j in (0, 5)] + [[B("⬅️", callback_data=f"pp:{p[1]}")]]
        return await show(q, f"📄 <b>{SUBJ[p[1]]} {p[2]}</b>\nRounds: {cnt(v_)}/10 — කරපු round එක tap කරන්න", M(kb))
    if c == "sy":
        tps = pdb(D)[p[1]]["topics"]
        return await show(q, f"📚 <b>{SUBJ[p[1]]} Syllabus</b>\nපාඩමක් තෝරන්න", M([[B(f"{i + 1}. {t_.get('name', '')[:24]} {sum(cnt(s.get('rounds', [])) for s in t_.get('subTopics', []))}/{10 * len(t_.get('subTopics', []))}", callback_data=f"st:{p[1]}:{i}")] for i, t_ in enumerate(tps)] + [[B("⬅️", callback_data="go:papers")]]))
    if c == "st":
        t_ = pdb(D)[p[1]]["topics"][int(p[2])]
        return await show(q, f"📚 <b>{esc(t_.get('name', ''))}</b>\nඋප මාතෘකාවක් තෝරන්න", M([[B(f"{s.get('name', '')[:28]} · {cnt(s.get('rounds', []))}/10", callback_data=f"su:{p[1]}:{p[2]}:{j}")] for j, s in enumerate(t_.get("subTopics", []))] + [[B("⬅️", callback_data=f"sy:{p[1]}")]]))
    if c in ("su", "sx"):
        s_ = pdb(D)[p[1]]["topics"][int(p[2])]["subTopics"][int(p[3])]; s_.setdefault("rounds", [False] * 10)
        if c == "sx": s_["rounds"][int(p[4])] = not s_["rounds"][int(p[4])]; await db_put(uid, {PK: D[PK]})
        kb = [[B(("✅" if s_["rounds"][i] else "") + str(i + 1), callback_data=f"sx:{p[1]}:{p[2]}:{p[3]}:{i}") for i in range(j, j + 5)] for j in (0, 5)] + [[B("⬅️", callback_data=f"st:{p[1]}:{p[2]}")]]
        return await show(q, f"📚 <b>{esc(s_.get('name', ''))}</b>\nRounds: {cnt(s_['rounds'])}/10 — කරපු round එක tap කරන්න", M(kb))
    if c == "s90": D["plan"] = {**(D.get("plan") or {"done": []}), "start": t}; await db_put(uid, {"plan": D["plan"]}); return await page("plan")
    if c == "lse": return await ask(("ls",), "💡 මෙහෙම එවන්න:\n<code>weak 1 | මගේ දුර්වලතාව</code>\nහෝ\n<code>mind 2 | මගේ mindset වාක්‍යය</code>")
    if c == "fs": ctx.user_data["fsub"] = p[1]; return await page("focus")
    if c == "fg":
        meta = D.get("_meta") or {}
        if meta.get("focus"): return await q.answer("A timer is already running", show_alert=True)
        m = int(p[1]); s = ctx.user_data.get("fsub", "Physics"); meta.update(chat=q.message.chat_id, focus=dict(s=s, m=m, chat=q.message.chat_id)); D["_meta"] = meta; await db_put(uid, {"_meta": meta})
        for j in app.job_queue.get_jobs_by_name(f"f{uid}"): j.schedule_removal()
        app.job_queue.run_once(focus_done, m * 60, data=(uid, dict(s=s, m=m, chat=q.message.chat_id)), name=f"f{uid}"); return await page("focus")
    if c == "fc":
        meta = D.get("_meta") or {}; meta.pop("focus", None); D["_meta"] = meta; await db_put(uid, {"_meta": meta})
        for j in app.job_queue.get_jobs_by_name(f"f{uid}"): j.schedule_removal()
        return await page("focus")

# ---- reminders
def schedule(app, uid, meta):
    for n, fn, key in (("am", am_job, "am"), ("pm", pm_job, "pm")):
        for j in app.job_queue.get_jobs_by_name(f"{n}{uid}"): j.schedule_removal()
        try: h, mi = map(int, (meta.get(key) or ("05:30" if n == "am" else "21:30")).split(":"))
        except Exception: continue
        app.job_queue.run_daily(fn, time(h, mi, tzinfo=TZ), data=uid, name=f"{n}{uid}")
async def am_job(ctx):
    uid = ctx.job.data; D = await db_load(uid); meta = D.get("_meta") or {}
    if not meta.get("notif", True) or not meta.get("chat"): return
    due = [x for x in D.get("tasks", []) if not x.get("done") and x.get("d") and dleft(x["d"]) <= 1]
    for ch in (meta.get("chats") or [meta["chat"]]):
        try:
            await send_home(ch, ctx.bot, D, uid)
            if due: await ctx.bot.send_message(ch, "⚠️ <b>Due soon</b>\n" + "\n".join(f"{'🎓' if is_exam(x['t']) else '📌'} {esc(x['t'])} — {when(x['d'])}" for x in due), parse_mode=H)
        except Exception as e: log.warning("am_job %s: %s", ch, e)
async def pm_job(ctx):
    uid = ctx.job.data; D = await db_load(uid); meta = D.get("_meta") or {}
    if not meta.get("notif", True) or not meta.get("chat"): return
    t = ds(today()); dn = hlog(D).get(t, []); miss = [h for h in habs(D) if h[0] not in dn]
    msg = f"🌙 <b>Night review</b>\nStudy {study_on(D, today()):.1f}/{float(D.get('goalH', 6)):g}h · Habits {round(day_pct(D, t) * 100)}%\n"
    msg += ("\nStill open:\n" + "\n".join(f"⬜ {esc(h[1])}" for h in miss) + "\n\nFinish before you sleep 🔥") if miss else "\nAll habits done. Streak safe! 🏆"
    for ch in (meta.get("chats") or [meta["chat"]]):
        try: await ctx.bot.send_message(ch, msg, parse_mode=H, reply_markup=M([[B("✅ Habits", callback_data="go:habits")]]))
        except Exception as e: log.warning("pm_job %s: %s", ch, e)

# ============================================================ web API + app
@asynccontextmanager
async def lifespan(api):
    global HC
    HC = httpx.AsyncClient(timeout=20)
    bot = Application.builder().token(TOKEN).build()
    for n, f in dict(start=cmd_start, menu=cmd_menu, exam=cmd_exam, goal=cmd_goal, remind=cmd_remind, log=cmd_log, ai=cmd_ai, cancel=cmd_cancel).items():
        bot.add_handler(CommandHandler(n, f))
    bot.add_handler(CallbackQueryHandler(on_cb)); bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await bot.initialize(); await bot.start(); await bot.updater.start_polling(drop_pending_updates=True)
    try:
        try: await db_put(0, {"_ping": 1}); log.info("Supabase write OK")
        except Exception as e: log.error("SUPABASE WRITE TEST FAILED: %s", e)
        if PUBLIC: await bot.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("📊 Dashboard", WebAppInfo(PUBLIC + "/")))
        for uid, meta in (await db_metas()).items(): schedule(bot, uid, meta or {})
    except Exception as e: log.warning("startup: %s", e)
    log.info("Bot + web running"); yield
    await bot.updater.stop(); await bot.stop(); await bot.shutdown(); await HC.aclose()

api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))

def auth(request: Request) -> int:
    try: return verify(request.headers.get("X-Init-Data", ""))
    except Exception: raise HTTPException(401, "unauthorized")

@api.get("/")
async def index(): return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-store"})
@api.get("/health")
async def health(): return {"ok": True}
@api.get("/api/data")
async def get_data(request: Request):
    uid = auth(request); d = await db_load(uid); d = {k: v for k, v in d.items() if not k.startswith("_")}
    return JSONResponse({"data": d}, headers={"Cache-Control": "no-store"})
@api.post("/api/data")
async def post_data(request: Request):
    uid = auth(request); body = await request.json(); items = body.get("items") or {}
    clean = {k: v for k, v in items.items() if re.fullmatch(r"[\w\-]{1,80}", k) and not k.startswith("_")}
    if len(json.dumps(clean)) > 3_000_000: raise HTTPException(413, "too big")
    await db_put(uid, clean); return {"ok": True, "saved": list(clean)}

AI_HITS = {}
@api.post("/api/ai")
async def ai_api(request: Request):
    uid = auth(request)
    if not ai_on(): raise HTTPException(503, "AI key not set")
    hits = [t for t in AI_HITS.get(uid, []) if _t.time() - t < 60]
    if len(hits) >= 12: raise HTTPException(429, "rate limit")
    AI_HITS[uid] = hits + [_t.time()]
    prompt = str((await request.json()).get("prompt", ""))[:8000]
    try: return {"text": await llm("", [{"role": "user", "content": prompt}])}
    except Exception as e: log.warning("AI error: %s", e); raise HTTPException(502, "ai error")

if __name__ == "__main__":
    for n, v in dict(BOT_TOKEN=TOKEN, SUPABASE_URL=SB, SUPABASE_KEY=KEY).items():
        if not v: raise SystemExit(f"Missing env var {n}")
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
