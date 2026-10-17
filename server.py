#!/usr/bin/env python3
"""A/L Dashboard cloud: one process = web dashboard (Telegram Mini App) + Telegram bot.
Storage: Supabase (table app_data). Hosting: Railway.
ENV: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY (service_role), PUBLIC_URL (https://xxx.up.railway.app)
optional: BOT_TZ (Asia/Colombo), GEMINI_API_KEY (+GEMINI_MODEL) or ANTHROPIC_API_KEY, OWNER_IDS (comma separated telegram ids), PORT
"""
import os, io, re, json, hmac, hashlib, html, uuid, random, asyncio, logging, base64, math, time as _t
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo
import httpx, uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import (Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, KeyboardButton,
                      ReplyKeyboardMarkup, WebAppInfo, MenuButtonWebApp, BotCommand)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters)

TOKEN = os.environ.get("BOT_TOKEN", "")
SB = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY", "")
PUBLIC = os.environ.get("PUBLIC_URL", "").rstrip("/")
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Colombo"))
OWNERS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("al"); H = ParseMode.HTML
HC: httpx.AsyncClient = None
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
BRAND, TAGLINE = "OmegaVX", "BEYOND LIMITS"
SIN_DAYS = ["සඳුදා", "අඟහරුවාදා", "බදාදා", "බ්‍රහස්පතින්දා", "සිකුරාදා", "සෙනසුරාදා", "ඉරිදා"]
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
    rows = [{"tg_id": uid, "k": k, "v": v, "updated_at": datetime.utcnow().isoformat() + "Z"} for k, v in items.items()]
    r = await HC.post(f"{SB}/rest/v1/app_data", params={"on_conflict": "tg_id,k"}, content=json.dumps(rows),
                      headers=_h({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}))
    r.raise_for_status()
async def db_metas():
    r = await HC.get(f"{SB}/rest/v1/app_data", params={"k": "eq._meta", "select": "tg_id,v"}, headers=_h())
    r.raise_for_status(); return {x["tg_id"]: x["v"] for x in r.json()}

# ============================================================ Telegram Mini App auth
def verify(init: str) -> int:
    d = dict(parse_qsl(init, keep_blank_values=True)); h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h): raise ValueError("bad hash")
    if _t.time() - int(d.get("auth_date", "0")) > 3 * 86400: raise ValueError("expired")
    uid = int(json.loads(d["user"])["id"])
    if OWNERS and uid not in OWNERS: raise ValueError("not allowed")
    return uid

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
          "I never give up.", "Own every single day.", "දිනලා විතරක් ඉවර කරන්න.", "Small steps every day beat big plans never started.",
          "හිතන්න එපා, පටන් ගන්න.", "ඔයාට පුළුවන්. ඒක මතක තියාගන්න.", "අද කරන්නේ නැත්නම් හෙට ඒක තවත් අමාරුයි.",
          "වෙහෙස එක දවසක්, ජයග්‍රහණය ජීවිත කාලයක්.", "සෑම දිනයක්ම ඔබේ ඉලක්කයට තව එක පියවරක්.",
          "Don't stop when you're tired. Stop when you're done.", "Be stronger than your excuses.", "Discipline equals freedom.",
          "Your future self is watching you right now.", "Beyond limits. 🚀"]
AI_SYS = ("You are OmegaVX Coach, a friendly study coach and tutor for a Sri Lankan A/L student (Combined Maths, Physics, Chemistry, plus Python/AI). "
          "ALWAYS reply in Sinhala (technical terms may stay in English). Be concise, explain step by step, encourage discipline. "
          "Reply in plain text for Telegram: no markdown symbols like ** or #, short paragraphs, light emoji use. "
          "If the student sends a photo of a question, read it carefully and solve it step by step.")

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

# ============================================================ extra stats helpers
def subj_hours(D, d):
    day = (D.get(SK) or {}).get(f"{d.year}-{d.month - 1}", {}).get(str(d.day), {}); out = {}
    for kind in ("SelfStudy", "Classes"):
        for k, h in (day.get(kind) or {}).items():
            if isinstance(h, (int, float)): out[k] = out.get(k, 0) + h
    return out
def total_study(D):
    tot = 0
    for mo in (D.get(SK) or {}).values():
        if not isinstance(mo, dict): continue
        for day in mo.values():
            if isinstance(day, dict):
                for kind in ("SelfStudy", "Classes"):
                    tot += sum(h for h in (day.get(kind) or {}).values() if isinstance(h, (int, float)))
    return tot
def paper_rounds(D):
    db = pdb(D); return sum(cnt(r) for k in STUDY for r in db[k]["papers"].values())
def xp_level(D):
    xp = int(total_study(D) * 10 + int(D.get("bestStreak", 0)) * 20 + paper_rounds(D) * 2 + sum(1 for m in D.get("mist", []) if m.get("r")) * 5)
    lv = int(math.sqrt(xp / 50)) + 1; lo, hi = 50 * (lv - 1) ** 2, 50 * lv ** 2
    return xp, lv, (xp - lo) / (hi - lo), hi

# ============================================================ countdown banner (picture) — OmegaVX style
_FB = set(); _FC = {}; _IC = {}
def _font(size, bold=True):
    k = (size, bold)
    if k in _FC: return _FC[k]
    cands = [os.path.join(ASSETS, "bold.ttf" if bold else "regular.ttf")] + (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"])
    f = None
    for p in cands:
        try: f = ImageFont.truetype(p, size); break
        except Exception: pass
    if f is None: f = ImageFont.load_default(size); _FB.add(k)
    _FC[k] = f; return f
def _txt(dr, xy, s, size, fill, anchor="mm", bold=True, spacing=0):
    f = _font(size, bold); sw = max(1, size // 40) if (size, bold) in _FB and bold else 0
    if spacing:  # letter-spaced text (centered)
        w = sum(dr.textlength(c, font=f) + spacing for c in s) - spacing; x = xy[0] - w / 2 if anchor[0] == "m" else xy[0]
        for c in s: dr.text((x, xy[1]), c, font=f, fill=fill, anchor="l" + anchor[1]); x += dr.textlength(c, font=f) + spacing
        return
    dr.text(xy, s, font=f, fill=fill, anchor=anchor, stroke_width=sw, stroke_fill=fill)
def _rr(im, box, r, fill, outline=None, width=0, blur=0, shift=(0, 0)):
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0)); b = (box[0] + shift[0], box[1] + shift[1], box[2] + shift[0], box[3] + shift[1])
    ImageDraw.Draw(ov).rounded_rectangle(b, r, fill=fill, outline=outline, width=width)
    if blur: ov = ov.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(im, ov)
def _bg(W, Hh):
    k = ("bg", W, Hh)
    if k in _IC: return _IC[k].copy()
    im = Image.new("RGB", (W, Hh)); dr = ImageDraw.Draw(im)
    for y in range(Hh):
        t = y / Hh; dr.line([(0, y), (W, y)], fill=(int(14 + 16 * t), int(8 + 4 * t), int(30 + 30 * t)))
    glow = Image.new("RGB", (W, Hh), (0, 0, 0)); ImageDraw.Draw(glow).ellipse((W * .05, Hh * .1, W * .95, Hh * .8), fill=(120, 45, 210))
    im = Image.blend(im, glow.filter(ImageFilter.GaussianBlur(130)), .38).convert("RGBA")
    rnd = random.Random(7); ov = Image.new("RGBA", (W, Hh), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)   # circuit lines like the logo
    for _ in range(int(W * Hh / 9000)):
        x, y = rnd.randrange(0, W, 20), rnd.randrange(0, Hh, 20); pts = [(x, y)]
        for _ in range(rnd.randint(2, 4)):
            if rnd.random() < .5: x += rnd.choice([-1, 1]) * rnd.randrange(40, 200, 20)
            else: y += rnd.choice([-1, 1]) * rnd.randrange(40, 200, 20)
            pts.append((x, y))
        od.line(pts, fill=(168, 85, 247, 60), width=3); e = pts[-1]; od.ellipse((e[0] - 6, e[1] - 6, e[0] + 6, e[1] + 6), outline=(196, 140, 255, 110), width=3)
    im = Image.alpha_composite(im, ov); _IC[k] = im; return im.copy()
def _logo(d):
    k = ("logo", d)
    if k not in _IC:
        try: lg = Image.open(os.path.join(ASSETS, "omegavx.png")).convert("RGB").crop((200, 150, 440, 390)).resize((d, d), Image.LANCZOS)
        except Exception:
            lg = Image.new("RGB", (d, d), (20, 10, 40)); _txt(ImageDraw.Draw(lg), (d // 2, d // 2), "Ω", d // 2, (192, 132, 252))
        m = Image.new("L", (d * 4, d * 4), 0); ImageDraw.Draw(m).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255); m = m.resize((d, d), Image.LANCZOS)
        out = Image.new("RGBA", (d, d), (0, 0, 0, 0)); out.paste(lg, (0, 0), m); _IC[k] = out
    return _IC[k]
def _jpg(im):
    b = io.BytesIO(); im.convert("RGB").save(b, "JPEG", quality=92); b.seek(0); b.name = "omegavx.jpg"; return b

def _bg_light(W, Hh):
    k = ("bgl", W, Hh)
    if k in _IC: return _IC[k].copy()
    im = Image.new("RGB", (W, Hh)); dr = ImageDraw.Draw(im)
    for y in range(Hh):
        t = y / Hh; dr.line([(0, y), (W, y)], fill=(int(247 - 12 * t), int(244 - 16 * t), int(255 - 4 * t)))
    im = im.convert("RGBA"); rnd = random.Random(11); ov = Image.new("RGBA", (W, Hh), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)   # very faint circuit doodles
    for _ in range(int(W * Hh / 11000)):
        x, y = rnd.randrange(0, W, 20), rnd.randrange(0, Hh, 20); pts = [(x, y)]
        for _ in range(rnd.randint(2, 3)):
            if rnd.random() < .5: x += rnd.choice([-1, 1]) * rnd.randrange(40, 160, 20)
            else: y += rnd.choice([-1, 1]) * rnd.randrange(40, 160, 20)
            pts.append((x, y))
        od.line(pts, fill=(139, 92, 246, 26), width=3); e = pts[-1]; od.ellipse((e[0] - 6, e[1] - 6, e[0] + 6, e[1] + 6), outline=(139, 92, 246, 40), width=3)
    im = Image.alpha_composite(im, ov); _IC[k] = im; return im.copy()

def banner(D):   # minimal white theme (like the Exam Countdown Bot card)
    ex = exam_dt(D); sec = max(0, int((ex - now()).total_seconds())); days = sec // 86400
    vals = [("MONTHS", days // 30), ("WEEKS", days // 7), ("HOURS", sec // 3600 % 24), ("MINS", sec // 60 % 60)]
    W, Hh = 1000, 860; CX = W // 2; im = _bg_light(W, Hh); card = (110, 200, W - 110, 650)
    im = _rr(im, card, 64, (90, 60, 160, 70), blur=30, shift=(0, 24)); im = _rr(im, card, 64, (255, 255, 255, 255))
    dr = ImageDraw.Draw(im); n = str(days); _txt(dr, (CX, 425), n, 310 if len(n) <= 3 else 240, (28, 25, 40)); _txt(dr, (CX, 580), "DAYS LEFT", 34, (130, 126, 150), spacing=6, bold=False)
    pill = (250, 140, W - 250, 250)
    im = _rr(im, pill, 55, (90, 60, 160, 60), blur=14, shift=(0, 8)); im = _rr(im, pill, 55, (255, 255, 255, 255), outline=(235, 230, 250, 255), width=2)
    im.alpha_composite(_logo(84), (270, 153)); dr = ImageDraw.Draw(im)
    _txt(dr, (386, 180), BRAND, 44, (28, 25, 40), anchor="lm"); _txt(dr, (388, 224), TAGLINE, 18, (124, 58, 237), anchor="lm", spacing=5)
    _txt(dr, (CX, 735), f"A/L {ex.year}  •  {ex.strftime('%d %b %Y').upper()}", 28, (90, 85, 115), bold=False)
    _txt(dr, (CX, 783), f"POWERED BY {BRAND.upper()}", 18, (139, 92, 246), spacing=5)
    return _jpg(im), days, vals

def weekly_img(D):
    W, Hh = 1000, 700; im = _bg(W, Hh); t = today(); goal = float(D.get("goalH", 6)) or 6
    days = [t - timedelta(i) for i in range(6, -1, -1)]; vals = [study_on(D, d) for d in days]; mx = max(goal, max(vals), 1) * 1.2
    im = _rr(im, (40, 120, W - 40, Hh - 40), 44, (255, 255, 255, 245)); dr = ImageDraw.Draw(im)
    lg = _logo(64); im.alpha_composite(lg, (44, 30)); dr = ImageDraw.Draw(im)
    _txt(dr, (126, 52), "WEEKLY STUDY REPORT", 36, (255, 255, 255), anchor="lm"); _txt(dr, (126, 92), f"{BRAND}  •  total {sum(vals):.1f}h", 22, (196, 181, 253), anchor="lm", bold=False)
    base, top, x0 = 560, 205, 100; hh = base - top; sl = (W - 200) / 7
    gy = base - goal / mx * hh
    for xx in range(x0, W - 100, 24): dr.line([(xx, gy), (xx + 12, gy)], fill=(245, 158, 11), width=3)
    _txt(dr, (W - 100, gy - 16), f"goal {goal:g}h", 20, (217, 119, 6), anchor="rm")
    for i, (d, v) in enumerate(zip(days, vals)):
        cx = x0 + sl * i + sl / 2; bh = max(4, v / mx * hh); col = (124, 58, 237) if v >= goal else (167, 139, 250)
        dr.rounded_rectangle((cx - 34, base - bh, cx + 34, base), 12, fill=col)
        _txt(dr, (cx, base - bh - 20), f"{v:.1f}", 24, (40, 30, 70)); _txt(dr, (cx, base + 30), d.strftime("%a"), 24, (110, 100, 140), bold=False)
        _txt(dr, (cx, base + 58), d.strftime("%m-%d"), 17, (160, 150, 185), bold=False)
    dr.line([(x0, base), (W - 100, base)], fill=(210, 200, 235), width=3)
    return _jpg(im)

# ============================================================ pages (text, keyboard)
def grid(rows, per=3): return [rows[i:i + per] for i in range(0, len(rows), per)]
def home_btn(): return [B("🏠 මුල් පිටුව", callback_data="go:home")]
def web_kb():
    return M(([[B("🌐 Open Web App", web_app=WebAppInfo(PUBLIC + "/"))]] if PUBLIC else []) + [home_btn()])
def menu_kb():
    items = [("✅ පුරුදු", "habits"), ("📅 සැලසුම", "planner"), ("📚 අධ්‍යයන", "study"), ("🧠 වැරදි", "mist"), ("⏱️ Focus", "focus"),
             ("🎯 ඉලක්ක", "targets"), ("📝 Papers", "papers"), ("📈 ලකුණු", "marks"), ("🗓️ දින 90", "plan"), ("🧭 Roadmap", "roadmap"),
             ("🌠 සිහින", "dreams"), ("💡 දුර්වලතා", "ls"),
             ("📋 අද", "today"), ("📊 Report", "report"), ("🏆 Level", "stats"), ("🔁 Revise", "revise"), ("📒 Notes", "notes"),
             ("💬 Motivation", "quote"), ("⚙️ Settings", "settings"), ("🤖 AI", "ai")]
    rows = [[B("🌐 Website (Full Dashboard)", web_app=WebAppInfo(PUBLIC + "/"))]] if PUBLIC else []
    return M(rows + grid([B(l, callback_data="go:" + p) for l, p in items]))

# bottom keyboard buttons -> page
BOTTOM = {"📋 අද": "today", "✅ පුරුදු": "habits", "📚 අධ්‍යයන": "study", "⏱️ Focus": "focus", "📊 Report": "report",
          "🏆 Level": "stats", "🤖 AI": "ai", "💬 Motivation": "quote"}
def reply_kb(meta):
    web = KeyboardButton("🧑‍💻 Web APP ✅", web_app=WebAppInfo(PUBLIC + "/")) if PUBLIC else KeyboardButton("🧑‍💻 Web APP ✅")
    K = KeyboardButton
    return ReplyKeyboardMarkup([[K("🕐 Count Down 🍀"), web],
                                [K("📋 අද"), K("✅ පුරුදු"), K("📚 අධ්‍යයන")],
                                [K("⏱️ Focus"), K("📊 Report"), K("🏆 Level")],
                                [K("🤖 AI"), K("💬 Motivation")],
                                [K("🔔 Notifications: ON" if meta.get("notif", True) else "🔕 Notifications: OFF")]],
                               resize_keyboard=True, input_field_placeholder="ප්‍රශ්නයක් ටයිප් කරන්න, AI උත්තර දෙයි 🤖")

def home_caption(D):
    n = now(); g = "🌅 Good Morning" if n.hour < 12 else "☀️ Good Afternoon" if n.hour < 17 else "🌙 Good Evening"
    nm = esc((D.get("_meta") or {}).get("name") or ""); g += (f", <b>{nm}</b>" if nm else "") + " 🌄"
    ex = exam_dt(D); sec = max(0, int((ex - n).total_seconds())); d = sec // 86400; t = ds(today()); pct = round(max(0, min(1, 1 - d / 365)) * 100)
    out = [g, "", "💬 <b>Daily Quote</b>", f"<i>{esc(random.choice(QUOTES))}</i>", "",
           f"🎓 {ex.year} A/L විභාගයට තව,", f"🔥 මාස <b>{d // 30}</b>", f"📚 සති <b>{d // 7}</b>", f"🔴 දින <b>{d}</b>",
           f"⏳ පැය <b>{sec // 3600 % 24}</b>", f"⚡ මිනිත්තු <b>{sec // 60 % 60}</b>", "",
           f"🚀 {bar(pct / 100, 12)} <b>{pct}%</b>", f"📅 අද Date: {t} · {SIN_DAYS[today().weekday()]}", "",
           f"📖 {bar(study_on(D, today()) / (float(D.get('goalH', 6)) or 6), 8)} {study_on(D, today()):.1f}/{float(D.get('goalH', 6)):g}h · ✅ {round(day_pct(D, t) * 100)}% · 🔥 {streak(D)}"]
    op = sorted([x for x in D.get("tasks", []) if not x.get("done")], key=lambda x: x.get("d") or "9")[:3]
    for x in op: out.append(f"{'🎓' if is_exam(x['t']) else '📌'} {esc(x['t'][:30])} · {when(x.get('d'))}")
    out += ["", f"⚡ <i>Powered by {BRAND}</i> 🚀"]
    return "\n".join(out)

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

def quiz_prompt(k):
    return (f"Ask me ONE challenging A/L {SUBJ.get(k, k)} MCQ (Sri Lankan syllabus) with 4 options A-D. Do NOT reveal the answer yet. "
            "After I reply, tell me if I'm right and explain briefly.")

def p_ai(D, arg=None):
    if not ai_on(): return "🤖 Study AI is off (Railway Variables වල <code>GEMINI_API_KEY</code> දාන්න).", M([home_btn()])
    kb = grid([B("🧪 " + SUBJ[k] + " quiz", callback_data=f"aq:{k}") for k in STUDY], 2) + [[B("💪 Motivate me", callback_data="am")], home_btn()]
    return ("🤖 <b>Study AI</b>\nමොකක් හරි ටයිප් කරන්න (Sinhala / Singlish / English) — AI තනිවම උත්තර දෙයි.\n"
            "📷 ප්‍රශ්නයක photo එකක් එවන්නත් පුළුවන්, step by step විසඳනවා.\n🧪 Quiz එකක් ඕන නම් පහළින් තෝරන්න 👇"), M(kb)

def p_today(D, arg=None):
    t = today(); k = ds(t); goal = float(D.get("goalH", 6)) or 6; sh = subj_hours(D, t); tot = study_on(D, t)
    hs = habs(D); dn = set(hlog(D).get(k, [])); miss = [h for h in hs if h[0] not in dn]; fl = (D.get("focus") or {}).get(k, {"n": 0, "m": 0})
    txt = f"📋 <b>අද</b> · {k} · {SIN_DAYS[t.weekday()]}\n\n📖 {bar(tot / goal)} <b>{tot:.1f}/{goal:g}h</b>\n"
    txt += ("  " + " · ".join(f"{SUBJ[s]} {h:.1f}h" for s, h in sh.items() if h) + "\n") if sh else ""
    txt += f"⏱️ Focus: {fl['n']} sessions · {fl['m']} min\n\n✅ Habits <b>{len(hs) - len(miss)}/{len(hs)}</b> {bar(day_pct(D, k), 8)}\n"
    txt += "".join(f"⬜ {esc(h[1][:40])}\n" for h in miss[:4])
    due = sorted([x for x in D.get("tasks", []) if not x.get("done") and x.get("d") and dleft(x["d"]) <= 2], key=lambda x: x["d"])
    if due: txt += "\n⚠️ <b>Due soon</b>\n" + "\n".join(f"{'🎓' if is_exam(x['t']) else '📌'} {esc(x['t'][:34])} — {when(x['d'])}" for x in due[:4]) + "\n"
    txt += f"\n🔥 Streak <b>{streak(D)}</b>"
    return txt, M([[B("✅ Habits", callback_data="go:habits"), B("📚 Log study", callback_data="go:study"), B("⏱️ Focus", callback_data="go:focus")], home_btn()])

def report_text(D):
    t = today(); days = [t - timedelta(i) for i in range(6, -1, -1)]; vals = [study_on(D, d) for d in days]; goal = float(D.get("goalH", 6)) or 6
    sub = {}
    for d in days:
        for k_, h in subj_hours(D, d).items(): sub[k_] = sub.get(k_, 0) + h
    bi = max(range(7), key=lambda i: vals[i]); hp = sum(day_pct(D, ds(d)) for d in days) / 7
    out = [f"📊 <b>Weekly report</b>  {days[0].strftime('%m-%d')} → {days[-1].strftime('%m-%d')}", "",
           f"⏱️ Total <b>{sum(vals):.1f}h</b> · avg <b>{sum(vals) / 7:.1f}h</b>/day",
           f"🏅 Best day: {days[bi].strftime('%a')} ({vals[bi]:.1f}h)", f"🎯 Goal hit: <b>{sum(1 for v in vals if v >= goal)}/7</b> days",
           f"✅ Habits avg: <b>{round(hp * 100)}%</b>"]
    if sub: out.append("📚 " + " · ".join(f"{SUBJ.get(k_, k_)} {h:.1f}h" for k_, h in sorted(sub.items(), key=lambda x: -x[1])))
    out += [f"🔥 Streak <b>{streak(D)}</b>", "", f"⚡ <i>{BRAND}</i>"]
    return "\n".join(out)

def p_stats(D, arg=None):
    xp, lv, frac, nxt = xp_level(D); th = total_study(D); bs = max(int(D.get("bestStreak", 0)), streak(D)); pr = paper_rounds(D); rs = sum(1 for m in D.get("mist", []) if m.get("r"))
    bm = best_mark(D)
    B_ = [("🔥 7-day streak", bs >= 7), ("🔥 30-day streak", bs >= 30), ("⏱️ 50 study hours", th >= 50), ("⏱️ 100 study hours", th >= 100), ("⏱️ 300 study hours", th >= 300),
          ("📝 25 paper rounds", pr >= 25), ("📝 100 paper rounds", pr >= 100), ("🧠 10 mistakes fixed", rs >= 10), ("🎯 paper mark 75+", bm >= 75), ("🏆 paper mark 90+", bm >= 90)]
    txt = (f"🏆 <b>Level {lv}</b> · {xp} XP\n{bar(frac, 12)} → {nxt} XP\n\n⏱️ Total study <b>{th:.1f}h</b>\n🔥 Best streak <b>{bs}</b> days\n📝 Paper rounds <b>{pr}</b>\n🧠 Mistakes fixed <b>{rs}</b>\n\n<b>Badges</b>\n"
           + "\n".join(("✅ " if ok_ else "🔒 ") + n for n, ok_ in B_))
    return txt, M([home_btn()])

def p_settings(D, arg=None):
    m = D.get("_meta") or {}; g = float(D.get("goalH", 6)) or 6; n = m.get("notif", True); am = m.get("am", "05:30"); pm = m.get("pm", "21:30")
    txt = (f"⚙️ <b>Settings</b>\n\n🔔 Notifications: <b>{'ON' if n else 'OFF'}</b>\n🌅 Morning: <b>{am}</b>\n🌙 Night: <b>{pm}</b>\n🎯 Daily goal: <b>{g:g}h</b>\n"
           f"🎓 Exam: <b>{D.get('exam', '2027-08-02')}</b>  <i>(/exam 2027-08-02)</i>")
    kb = [[B("🔔 Notifications: " + ("ON ✅" if n else "OFF 🔕"), callback_data="cs:n")],
          [B(("● " if abs(g - x) < .01 else "🎯 ") + f"{x}h", callback_data=f"cs:g:{x}") for x in (4, 6, 8, 10)],
          [B(("● " if am == a else "🌅 ") + a, callback_data="cs:a:" + a.replace(":", "")) for a in ("04:30", "05:00", "05:30", "06:00")],
          [B(("● " if pm == a else "🌙 ") + a, callback_data="cs:p:" + a.replace(":", "")) for a in ("21:00", "21:30", "22:00", "23:00")]]
    return txt, M(kb + [home_btn()])

def p_quote(D, arg=None):
    return f"💬 <b>Motivation</b>\n\n<i>{esc(random.choice(QUOTES))}</i>\n\n🔴 විභාගයට තව දින <b>{max(0, (exam_dt(D) - now()).days)}</b>", \
        M([[B("🔄 තව එකක්", callback_data="go:quote"), B("🤖 AI Motivation", callback_data="am")], home_btn()])

def p_notes(D, arg=None):
    L = D.get("notes", [])[:8]; txt = f"📒 <b>Notes</b>  {len(D.get('notes', []))} total\n"; kb = []
    for n in L:
        txt += f"\n• {esc(n.get('t', '')[:200])} <i>({n.get('d', '')})</i>"; kb.append([B("🗑 " + n.get("t", "")[:26], callback_data=f"nx:{n['id']}")])
    if not L: txt += "\nNo notes yet. <code>/note ඔයාගේ note එක</code> හෝ පහළ බොත්තම."
    return txt, M(kb + [[B("➕ Add note", callback_data="na")], home_btn()])

def p_revise(D, arg=None):
    L = [m for m in D.get("mist", []) if not m.get("r")]
    if not L: return "🔁 <b>Revise</b>\nRevise කරන්න වැරදි නැහැ 🎉", M([[B("➕ වැරදි එකතු කරන්න", callback_data="ma")], home_btn()])
    m = random.choice(L)
    return (f"🔁 <b>Revise</b> ({len(L)} open)\n\n<b>{esc(m['q'])}</b> [{SUBJ.get(m.get('s'), '')}]\n❌ {esc(m.get('w', '-'))}\n\n✅ <tg-spoiler>{esc(m.get('c', '-'))}</tg-spoiler>\n\n"
            "<i>Spoiler tap කරලා පිළිතුර බලන්න</i>"), M([[B("✔ ඉගෙනගත්තා", callback_data=f"rs:{m['id']}"), B("⏭ ඊළඟ", callback_data="go:revise")], home_btn()])

PAGES = dict(plan=p_plan, ls=p_ls, habits=p_habits, planner=p_planner, study=p_study, mist=p_mist, targets=p_targets, papers=p_papers,
             marks=p_marks, roadmap=p_roadmap, dreams=p_dreams, ai=p_ai, today=p_today, stats=p_stats, settings=p_settings, quote=p_quote,
             notes=p_notes, revise=p_revise)

def render(name, D, ctx=None):
    """-> (text, keyboard, image or None). Photo pages: home, report."""
    if name == "home": return home_caption(D), menu_kb(), banner(D)[0]
    if name == "report": return report_text(D), M([[B("📋 අද", callback_data="go:today"), B("🏆 Level", callback_data="go:stats")], home_btn()]), weekly_img(D)
    if name == "focus":
        tx, kb = p_focus(D, fs=(ctx.user_data.get("fsub", "Physics") if ctx else "Physics"), active=(D.get("_meta") or {}).get("focus")); return tx, kb, None
    tx, kb = PAGES[name](D); return tx, kb, None
async def send_page(chat, ctx, D, name):
    tx, kb, img = render(name, D, ctx)
    if img: return await ctx.bot.send_photo(chat, img, caption=tx, parse_mode=H, reply_markup=kb)
    return await ctx.bot.send_message(chat, tx, parse_mode=H, reply_markup=kb)

# ============================================================ bot plumbing
async def ctx_data(update):
    u = update.effective_user
    if OWNERS and u.id not in OWNERS:
        await update.effective_message.reply_text("🔒 Private bot."); return None, None
    return u.id, await db_load(u.id)
async def meta_get(uid, D=None):
    D = D if D is not None else await db_load(uid); return D.get("_meta") or {}
async def meta_set(uid, chat, **kw):
    m = await meta_get(uid); m.update(chat=chat, **kw); await db_put(uid, {"_meta": m}); return m

async def send_home(chat, bot, D, uid=None):
    img, _, _ = banner(D); m = await meta_get(uid, D) if uid else {}
    await bot.send_photo(chat, img, caption=home_caption(D), parse_mode=H, reply_markup=menu_kb())

async def cmd_start(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    meta = await meta_set(uid, update.effective_chat.id, name=(update.effective_user.first_name or "")[:30], **({} if D.get("_meta") else dict(am="05:30", pm="21:30", notif=True)))
    schedule(ctx.application, uid, meta); D["_meta"] = meta
    await update.effective_message.reply_text(
        f"👋 ආයුබෝවන් <b>{esc(update.effective_user.first_name or '')}</b>! <b>{BRAND} Coach</b> වෙත සාදරයෙන් පිළිගන්නවා 🚀\n\n"
        "🌐 Bot එකයි Web App එකයි එකම data පාවිච්චි කරනවා — එකක වෙනස්කම් අනිත් එකේ auto update වෙනවා.\n"
        "🤖 පහළින් මොකක් හරි ටයිප් කරන්න, AI තනිවම උත්තර දෙනවා (📷 photo එකකුත් එවන්න පුළුවන්).", parse_mode=H, reply_markup=reply_kb(meta))
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
    uid, D = await ctx_data(update)
    if not uid: return
    q = update.effective_message.text.partition(" ")[2].strip()
    if q: await ai_reply(update, ctx, q, D)
    else: await send_page(update.effective_chat.id, ctx, D, "ai")
async def cmd_cancel(update, ctx): ctx.user_data.pop("await", None); await update.effective_message.reply_text("Cancelled ✅")
def page_cmd(name):
    async def f(update, ctx):
        uid, D = await ctx_data(update)
        if uid: await send_page(update.effective_chat.id, ctx, D, name)
    return f
async def cmd_note(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    txt = update.effective_message.text.partition(" ")[2].strip()
    if txt:
        D.setdefault("notes", []).insert(0, dict(id=uid8(), t=txt[:500], d=ds(today()))); await db_put(uid, {"notes": D["notes"]})
    await send_page(update.effective_chat.id, ctx, D, "notes")
async def cmd_quiz(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    k = skey(ctx.args[0]) if ctx.args else random.choice(STUDY); k = k if k in STUDY else random.choice(STUDY)
    await ai_reply(update, ctx, quiz_prompt(k), D)
async def cmd_help(update, ctx):
    await update.effective_message.reply_text(
        "📖 <b>Commands</b>\n/menu · /today · /habits · /study · /focus · /report · /level\n/revise · /notes · /note text · /quote · /quiz physics\n"
        "/log physics 1.5 · /goal 6 · /exam 2027-08-02 · /remind 05:30 21:30 · /settings · /cancel\n\n🤖 Command නැතුව ටයිප් කරන ඕනම දෙයකට AI උත්තර දෙයි.", parse_mode=H)

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
    if text.startswith("🕐 Count Down"): ctx.user_data.pop("await", None); return await send_home(chat, ctx.bot, D, uid)
    if text in BOTTOM: ctx.user_data.pop("await", None); return await send_page(chat, ctx, D, BOTTOM[text])
    if text.startswith(("🔔 Notifications", "🔕 Notifications")):
        meta = await meta_set(uid, chat, notif=not (D.get("_meta") or {}).get("notif", True))
        return await update.effective_message.reply_text("🔔 Notifications ON" if meta["notif"] else "🔕 Notifications OFF", reply_markup=reply_kb(meta))
    st = ctx.user_data.pop("await", None); ts = ctx.user_data.pop("await_ts", 0)
    if st and _t.time() - ts > 600: st = None          # stale form -> treat as normal chat
    if not st: return await ai_reply(update, ctx, text, D)
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
        elif k == "note":
            D.setdefault("notes", []).insert(0, dict(id=uid8(), t=text[:500], d=ds(today()))); await db_put(uid, {"notes": D["notes"]}); t, kb = p_notes(D)
        elif k == "tgt":
            tg = D.get("tgt") or {"short": [], "long": []}; tg[st[1]].append(dict(id=uid8(), em="🎯", t=text, d="", img="")); D["tgt"] = tg; await db_put(uid, {"tgt": tg}); t, kb = p_targets(D)
        await r(t, reply_markup=kb, parse_mode=H)
    except Exception:
        f_ = ctx.user_data.get("await_fail", 0) + 1
        if f_ >= 3: ctx.user_data.pop("await_fail", None); return await ai_reply(update, ctx, text, D)   # 3 bad tries -> let AI answer instead
        ctx.user_data["await"] = st; ctx.user_data["await_ts"] = ts; ctx.user_data["await_fail"] = f_
        await r({"log": "පැය ගණන එවන්න, උදා: 1.5 (නැවැත්තුවොත් /cancel)", "task": "Format: නම | දිනය | විෂය  (දිනය: 2026-11-02, tomorrow, +3)", "mark": "Format: නම | අවසාන ලකුණු | වටය | MCQ | SEQ | Essay  (නම + ලකුණු ඇති)", "ls": "Format: weak 2 | text  හෝ  mind 1 | text", "mist": "Format: විෂය | මාතෘකාව | වැරදුණු විදිය | නිවැරදි විදිය"}.get(k, "Try again (/cancel)"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
def _gkey(): return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
def ai_on(): return bool(_gkey() or os.environ.get("ANTHROPIC_API_KEY"))
async def llm(system, msgs, image=None, max_tokens=1200):
    """msgs = [{"role": "user"|"assistant", "content": str}]; image = base64 jpeg for the LAST user message. Gemini if GEMINI_API_KEY, else Claude."""
    if _gkey():
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in msgs]
        if image: contents[-1]["parts"].append({"inline_data": {"mime_type": "image/jpeg", "data": image}})
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": max(max_tokens, 3000)}}
        if system: body["systemInstruction"] = {"parts": [{"text": system}]}
        r = await HC.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent", timeout=60, json=body,
                          headers={"x-goog-api-key": _gkey(), "content-type": "application/json"})
        if r.status_code >= 400: log.warning("Gemini error %s %s", r.status_code, r.text[:300]); r.raise_for_status()
        c = (r.json().get("candidates") or [{}])[0]
        return "".join(x.get("text", "") for x in (c.get("content") or {}).get("parts", [])).strip() or "🤐 (පිළිතුරක් ආවේ නැහැ, වෙන විදිහකට අහන්න)"
    from anthropic import AsyncAnthropic
    ms = [dict(m) for m in msgs]
    if image: ms[-1]["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}, {"type": "text", "text": ms[-1]["content"]}]
    kw = {"system": system} if system else {}
    r = await AsyncAnthropic().messages.create(model=AI_MODEL, max_tokens=max_tokens, messages=ms, **kw)
    return "".join(b.text for b in r.content if b.type == "text").strip()

def ai_ctx(D):
    if not D: return ""
    return (f" Student context: {max(0, (exam_dt(D) - now()).days)} days left to A/L, studied {study_on(D, today()):.1f}h today "
            f"(goal {float(D.get('goalH', 6)):g}h), habit streak {streak(D)} days.")
async def ai_chat(ctx, chat, text, D=None, image=None, msg=None):
    async def out(t):
        for i in range(0, len(t), 4000):
            if msg: await msg.reply_text(t[i:i + 4000])
            else: await ctx.bot.send_message(chat, t[i:i + 4000])
    if not ai_on():
        return await out("🤖 AI එක on කරන්න Railway Variables වල GEMINI_API_KEY දාන්න. (දැනට buttons පාවිච්චි කරන්න 👇 /menu)")
    try: await ctx.bot.send_chat_action(chat, "typing")
    except Exception: pass
    h = ctx.user_data.setdefault("ai", [])
    try:
        ans = await llm(AI_SYS + ai_ctx(D), h + [{"role": "user", "content": text}], image=image) or "…"
        h += [{"role": "user", "content": text if not image else f"[photo] {text}"}, {"role": "assistant", "content": ans}]; del h[:-10]
        await out(ans)
    except Exception as e: log.exception(e); await out("⚠️ AI error — ටිකකින් නැවත try කරන්න")
async def ai_reply(update, ctx, text, D=None): return await ai_chat(ctx, update.effective_chat.id, text, D, msg=update.effective_message)
async def on_photo(update, ctx):
    uid, D = await ctx_data(update)
    if not uid: return
    m = update.effective_message
    try:
        f = await m.photo[-1].get_file(); raw = bytes(await f.download_as_bytearray())
        await ai_chat(ctx, m.chat_id, m.caption or "මේ ප්‍රශ්නය/පිටුව කියවලා step by step පැහැදිලි කරලා විසඳන්න.", D, image=base64.b64encode(raw).decode(), msg=m)
    except Exception as e: log.exception(e); await m.reply_text("⚠️ Photo එක කියවන්න බැරි වුණා, නැවත එවන්න.")

# ---- auto reactions (every incoming message / command)
POS = ["🔥", "\u2764", "👍", "💯", "⚡", "🏆", "🎉", "🤩", "👏", "😎", "🫡"]
CMD_R = dict(start="🔥", menu="⚡", today="💯", habits="💯", study="\u270d", log="\u270d", focus="🔥", report="🏆", level="🏆", revise="🤓", notes="\u270d", note="\u270d",
             quote="\u2764", quiz="🤓", ai="🤓", settings="👌", goal="💯", exam="🫡", remind="👍", cancel="👌", help="🤝")
async def auto_react(update, ctx):
    m = update.effective_message; u = update.effective_user
    if not m or not u or (OWNERS and u.id not in OWNERS): return
    t = (m.text or "").strip()
    if t.startswith("/"): e = CMD_R.get(t[1:].split()[0].split("@")[0].lower(), random.choice(POS))
    elif m.photo: e = "👀"
    elif t.startswith(("🕐", "🔔", "🔕")): e = "🔥"
    elif t in BOTTOM: e = "⚡"
    elif re.fullmatch(r"[\d.\s]+(class)?", t.lower()): e = "\u270d"
    elif re.match(r"((hi|hello|hey)\b|හායි|ආයුබෝවන්)", t.lower()): e = "🤗"
    elif "?" in t: e = "🤔"
    else: e = random.choice(POS)
    try: await m.set_reaction(e)
    except Exception as ex: log.debug("react: %s", ex)

# ---- focus timer
async def focus_done(ctx):
    uid, f = ctx.job.data; D = await db_load(uid); fl = D.get("focus") or {}
    add_study(D, f["s"], round(f["m"] / 60, 2)); t = fl.setdefault(ds(today()), {"n": 0, "m": 0}); t["n"] += 1; t["m"] += f["m"]
    meta = D.get("_meta") or {}; meta.pop("focus", None)
    await db_put(uid, {SK: D[SK], "focus": fl, "_meta": meta})
    await ctx.bot.send_message(meta.get("chat", uid), f"🎉 Focus done! +{f['m']} min {SUBJ[f['s']]} added to your study log. 5 min break 🧘")

# ---- callbacks
TOAST = dict(hb="🔥 Habit updated", tk="✅ Updated", tx="🗑 Deleted", mx="🗑 Deleted", ms="🧠 Updated", rs="💪 නියමයි! ඉගෙනගත්තා", rd="🧭 Updated", dr="🏆 Dream!",
             pr="📄 ✔", sx="📚 ✔", s90="🚀 අලුත් පටන්ගැන්මක්!", cs="⚙️ Saved ✅", nx="🗑 Deleted", fs="⏱️ Subject set", fc="⏹ Cancelled", aq="🧪 Quiz ready…", am="💪 ඔන්න…")
async def on_cb(update, ctx):
    q = update.callback_query; uid = q.from_user.id; p = q.data.split(":"); c = p[0]
    if c != "fg": await q.answer(TOAST.get(c))          # small pop-up reaction on every button (fg answers itself below)
    if OWNERS and uid not in OWNERS: return
    D = await db_load(uid); t = ds(today()); app = ctx.application
    async def page(name, **kw):
        tx, kb, img = render(name, D, ctx)
        if img:
            try: await q.message.delete()
            except Exception: pass
            return await q.message.chat.send_photo(img, caption=tx, parse_mode=H, reply_markup=kb)
        await show(q, tx, kb)
    async def ask(state, prompt):
        ctx.user_data["await"] = state; ctx.user_data["await_ts"] = _t.time(); ctx.user_data.pop("await_fail", None); await q.message.reply_text(prompt, parse_mode=H)
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
        await q.answer("⏱️ Focus started 🔥"); m = int(p[1]); s = ctx.user_data.get("fsub", "Physics"); meta.update(chat=q.message.chat_id, focus=dict(s=s, m=m)); D["_meta"] = meta; await db_put(uid, {"_meta": meta})
        for j in app.job_queue.get_jobs_by_name(f"f{uid}"): j.schedule_removal()
        app.job_queue.run_once(focus_done, m * 60, data=(uid, dict(s=s, m=m)), name=f"f{uid}"); return await page("focus")
    if c == "fc":
        meta = D.get("_meta") or {}; meta.pop("focus", None); D["_meta"] = meta; await db_put(uid, {"_meta": meta})
        for j in app.job_queue.get_jobs_by_name(f"f{uid}"): j.schedule_removal()
        return await page("focus")

    if c == "na": return await ask(("note",), "📒 ඔයාගේ note එක එවන්න:")
    if c == "nx": D["notes"] = [n for n in D.get("notes", []) if n["id"] != p[1]]; await db_put(uid, {"notes": D["notes"]}); return await page("notes")
    if c == "rs":
        for m in D.get("mist", []):
            if m["id"] == p[1]: m["r"] = True
        await db_put(uid, {"mist": D.get("mist", [])}); return await page("revise")
    if c == "aq": return await ai_chat(ctx, q.message.chat_id, quiz_prompt(p[1]), D)
    if c == "am":
        return await ai_chat(ctx, q.message.chat_id, "මට අද දවසට කෙටි, ශක්තිමත් motivation message එකක් දෙන්න. ඊට පස්සේ අද කරන්න ඕන ප්‍රධාන දේවල් 3ක් කියන්න.", D)
    if c == "cs":
        meta = D.get("_meta") or {}; meta["chat"] = q.message.chat_id
        if p[1] == "n": meta["notif"] = not meta.get("notif", True)
        elif p[1] == "g": D["goalH"] = float(p[2]); await db_put(uid, {"goalH": D["goalH"]})
        elif p[1] in ("a", "p"): meta["am" if p[1] == "a" else "pm"] = p[2][:2] + ":" + p[2][2:]
        D["_meta"] = meta; await db_put(uid, {"_meta": meta}); schedule(app, uid, meta); return await page("settings")

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
    await send_home(meta["chat"], ctx.bot, D, uid)
    due = [x for x in D.get("tasks", []) if not x.get("done") and x.get("d") and dleft(x["d"]) <= 1]
    if due: await ctx.bot.send_message(meta["chat"], "⚠️ <b>Due soon</b>\n" + "\n".join(f"{'🎓' if is_exam(x['t']) else '📌'} {esc(x['t'])} — {when(x['d'])}" for x in due), parse_mode=H)
async def pm_job(ctx):
    uid = ctx.job.data; D = await db_load(uid); meta = D.get("_meta") or {}
    if not meta.get("notif", True) or not meta.get("chat"): return
    t = ds(today()); dn = hlog(D).get(t, []); miss = [h for h in habs(D) if h[0] not in dn]
    msg = f"🌙 <b>Night review</b>\nStudy {study_on(D, today()):.1f}/{float(D.get('goalH', 6)):g}h · Habits {round(day_pct(D, t) * 100)}%\n"
    msg += ("\nStill open:\n" + "\n".join(f"⬜ {esc(h[1])}" for h in miss) + "\n\nFinish before you sleep 🔥") if miss else "\nAll habits done. Streak safe! 🏆"
    await ctx.bot.send_message(meta["chat"], msg, parse_mode=H, reply_markup=M([[B("✅ Habits", callback_data="go:habits")]]))

# ============================================================ web API + app
@asynccontextmanager
async def lifespan(api):
    global HC
    HC = httpx.AsyncClient(timeout=20)
    bot = Application.builder().token(TOKEN).build()
    for n, f in dict(start=cmd_start, menu=cmd_menu, exam=cmd_exam, goal=cmd_goal, remind=cmd_remind, log=cmd_log, ai=cmd_ai, cancel=cmd_cancel,
                     note=cmd_note, quiz=cmd_quiz, help=cmd_help).items():
        bot.add_handler(CommandHandler(n, f))
    for n, pg in dict(today="today", habits="habits", study="study", focus="focus", report="report", level="stats", revise="revise", notes="notes",
                      quote="quote", settings="settings", planner="planner", papers="papers", marks="marks", mistakes="mist", dreams="dreams").items():
        bot.add_handler(CommandHandler(n, page_cmd(pg)))
    bot.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, auto_react, block=False), group=-1)   # react to EVERY message/command
    bot.add_handler(CallbackQueryHandler(on_cb)); bot.add_handler(MessageHandler(filters.PHOTO, on_photo))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await bot.initialize(); await bot.start(); await bot.updater.start_polling(drop_pending_updates=True)
    try:
        await bot.bot.set_my_commands([BotCommand(c_, d_) for c_, d_ in (
            ("menu", "🏠 Main menu + countdown"), ("today", "📋 Today summary"), ("habits", "✅ Daily habits"), ("study", "📚 Study log"), ("focus", "⏱️ Focus timer"),
            ("report", "📊 Weekly report"), ("level", "🏆 Level & badges"), ("revise", "🔁 Revise mistakes"), ("notes", "📒 Notes"), ("quote", "💬 Motivation"),
            ("quiz", "🧪 AI quiz"), ("ai", "🤖 Ask AI"), ("settings", "⚙️ Settings"), ("help", "📖 All commands"))])
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
    try: return {"text": await llm("", [{"role": "user", "content": prompt}], max_tokens=1000)}
    except Exception as e: log.warning("AI error %s", e); raise HTTPException(502, "ai error")

if __name__ == "__main__":
    for n, v in dict(BOT_TOKEN=TOKEN, SUPABASE_URL=SB, SUPABASE_KEY=KEY).items():
        if not v: raise SystemExit(f"Missing env var {n}")
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
