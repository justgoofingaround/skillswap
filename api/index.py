"""SkillSwap API. Runs as a Vercel Python function (class `handler`) and locally via ../run_local.py.

Database: Postgres when DATABASE_URL (or POSTGRES_URL) is set, otherwise a local SQLite file.
AI matching: Claude when ANTHROPIC_API_KEY is set, otherwise a labeled keyword fallback.
All credit and booking rules are enforced here, not in the browser.
"""
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
PG = DATABASE_URL.startswith("postgres")
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's filesystem is read-only except /tmp. Without Postgres there, fall back to /tmp
# so the app still works, but data is temporary (per instance, lost on redeploy).
SQLITE_PATH = os.environ.get("DB_PATH") or ("/tmp/skillswap.db" if ON_VERCEL else str(Path(__file__).resolve().parent.parent / "skillswap.db"))
STORAGE = "postgres" if PG else ("temporary" if ON_VERCEL else "local")
AI_MODEL = "claude-opus-5-5"

START_CREDITS = 2
MAX_HOURS = 3
PAIR_WEEKLY_CAP = 3
FREE_ACTIVE = 3
MAX_BODY = 16_000
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BLOCKS = {"morning": "9am–12pm", "afternoon": "12–5pm", "evening": "5–9pm"}
SLOT_KEYS = {f"{d}-{b}" for d in DAYS for b in BLOCKS}

_hits = {}
_ready = False

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, code TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL, major TEXT DEFAULT '', headline TEXT DEFAULT '',
      teaches TEXT DEFAULT '[]', wants TEXT DEFAULT '[]', created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY, learner TEXT NOT NULL, teacher TEXT NOT NULL,
      skill TEXT NOT NULL, hours INTEGER NOT NULL, note TEXT DEFAULT '',
      status TEXT NOT NULL, rating INTEGER DEFAULT 0, review TEXT DEFAULT '',
      created TEXT NOT NULL, completed_at TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS s_learner ON sessions(learner)",
    "CREATE INDEX IF NOT EXISTS s_teacher ON sessions(teacher)",
]
MIGRATIONS = [  # columns added after the first release; each may already exist
    "ALTER TABLE users ADD COLUMN availability TEXT DEFAULT '[]'",
    "ALTER TABLE sessions ADD COLUMN slot TEXT DEFAULT ''",
]


# ---------- database ----------
class DB:
    """Tiny wrapper so the same SQL ('?' placeholders) runs on SQLite and Postgres."""

    def __init__(self):
        if PG:
            import psycopg
            from psycopg.rows import dict_row
            self.con = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=10)
        else:
            self.con = sqlite3.connect(SQLITE_PATH, timeout=10)
            self.con.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}

    def run(self, sql, args=()):
        return self.con.execute(sql.replace("?", "%s") if PG else sql, args)

    def one(self, sql, args=()):
        return self.run(sql, args).fetchone()

    def all(self, sql, args=()):
        return list(self.run(sql, args).fetchall())

    def lock_user(self, uid):
        if PG:  # serialize concurrent bookings by the same learner
            self.run("SELECT id FROM users WHERE id = ? FOR UPDATE", (uid,))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.con.rollback()
        else:
            self.con.commit()
        self.con.close()


def init_db():
    global _ready
    if _ready:
        return
    with DB() as d:
        for stmt in SCHEMA:
            d.run(stmt)
    for stmt in MIGRATIONS:
        try:
            with DB() as d:
                d.run(stmt)
        except Exception:
            pass  # column already exists
    _ready = True


# ---------- time & slots (campus time: New York) ----------
def campus_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def upcoming_slots(availability, days=7, start=None):
    """Concrete dates for a weekly availability list, soonest first: [{"slot": "2026-11-13|evening", ...}]."""
    start = start or campus_today()
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        for b in BLOCKS:
            if f"{DAYS[d.weekday()]}-{b}" in availability:
                out.append({"slot": f"{d.isoformat()}|{b}", "date": d.isoformat(), "block": b})
    return out


def slot_label(slot):
    try:
        ds, b = slot.split("|")
        d = date.fromisoformat(ds)
        return f"{d.strftime('%a %b')} {d.day}, {b}"
    except ValueError:
        return "Flexible time"


# ---------- helpers ----------
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(s, n):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def skills(v):
    items = v if isinstance(v, list) else str(v or "").split(",")
    out = []
    for x in items:
        x = clean(x, 40).lower()
        if x and x not in out:
            out.append(x)
    return out[:12]


def availability(v):
    items = v if isinstance(v, list) else []
    return sorted({str(x) for x in items if str(x) in SLOT_KEYS})


def week_key(iso):
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        d = datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-{w}"


def public_user(r):
    return {"id": r["id"], "name": r["name"], "major": r.get("major") or "", "headline": r.get("headline") or "",
            "teaches": json.loads(r.get("teaches") or "[]"), "wants": json.loads(r.get("wants") or "[]"),
            "availability": json.loads(r.get("availability") or "[]")}


def rate_limit(key, per_hour):
    t = time.time()
    hits = [h for h in _hits.get(key, []) if t - h < 3600]
    if len(hits) >= per_hour:
        raise ApiError(429, "Too many requests. Try again in a bit.")
    _hits[key] = hits + [t]


def ledger(uid, sessions, names):
    """Balance is derived from session history; it is never stored."""
    rows = [{"label": "Welcome credits", "amt": START_CREDITS}]
    earned = spent = held = 0
    pair = {}
    done = sorted((s for s in sessions if s["status"] == "completed"), key=lambda s: s["completed_at"] or "")
    for s in done:
        if s["learner"] == uid:
            spent += s["hours"]
            rows.append({"label": f"Learned {s['skill']} from {names.get(s['teacher'], 'someone')}", "amt": -s["hours"]})
        if s["teacher"] == uid:
            k = s["learner"] + "|" + week_key(s["completed_at"] or s["created"])
            got = min(max(0, PAIR_WEEKLY_CAP - pair.get(k, 0)), s["hours"])
            pair[k] = pair.get(k, 0) + got
            earned += got
            cap = " (weekly cap)" if got < s["hours"] else ""
            rows.append({"label": f"Taught {s['skill']} to {names.get(s['learner'], 'someone')}{cap}", "amt": got})
    for s in sessions:
        if s["learner"] == uid and s["status"] in ("requested", "accepted"):
            held += s["hours"]
    rows.reverse()
    return {"balance": START_CREDITS + earned - spent - held, "held": held, "rows": rows[:20]}


def campus(users, sessions):
    done = [s for s in sessions if s["status"] == "completed"]
    majors = {u["id"]: u["major"].strip().lower() for u in users}
    cross = [s for s in done if majors.get(s["teacher"]) and majors.get(s["learner"]) and majors[s["teacher"]] != majors[s["learner"]]]
    rated = [s["rating"] for s in done if s["rating"]]
    sup, dem = {}, {}
    for u in users:
        for t in u["teaches"]:
            sup[t] = sup.get(t, 0) + 1
        for w in u["wants"]:
            dem[w] = dem.get(w, 0) + 1

    def top(d):
        return sorted(d.items(), key=lambda kv: -kv[1])[:6]

    gaps = sorted((w for w in dem if w not in sup), key=lambda w: -dem[w])
    return {"members": len(users), "swaps": len(done), "hours": sum(s["hours"] for s in done),
            "cross": round(100 * len(cross) / len(done)) if done else None,
            "rating": round(sum(rated) / len(rated), 1) if rated else None,
            "supply": top(sup), "demand": top(dem), "gaps": [[g, dem[g]] for g in gaps[:12]]}


def teacher_stats(sessions):
    st = {}
    for s in sessions:
        if s["status"] != "completed":
            continue
        t = st.setdefault(s["teacher"], {"taught": 0, "sum": 0, "n": 0})
        t["taught"] += 1
        if s["rating"]:
            t["sum"] += s["rating"]
            t["n"] += 1
    return {k: {"taught": v["taught"], "avg": round(v["sum"] / v["n"], 1) if v["n"] else None} for k, v in st.items()}


# ---------- AI: plain-English need -> skills ----------
NEED_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What the student needs, in under 12 words"},
        "explanation": {"type": "string", "description": "One or two sentences, plain English: what this skill is and why it solves their problem"},
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The specific skill, e.g. 'pivot tables'"},
                    "catalog_match": {"type": "string", "description": "The exact catalog skill that covers it, or empty string if none"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Lowercase words a teacher might list for this skill"},
                },
                "required": ["name", "catalog_match", "keywords"],
                "additionalProperties": False,
            },
        },
        "session_title": {"type": "string", "description": "A short title for the session, e.g. 'Weekly category summary with pivot tables'"},
        "hours": {"type": "integer", "description": "Suggested session length, 1 to 3"},
        "deadline_date": {"type": "string", "description": "Last useful date for the session as YYYY-MM-DD, or empty string if no deadline"},
    },
    "required": ["summary", "explanation", "skills", "session_title", "hours", "deadline_date"],
    "additionalProperties": False,
}


def ai_interpret(need, catalog, today):
    import anthropic

    client = anthropic.Anthropic(timeout=25.0, max_retries=1)
    prompt = (
        "A university student on a peer skill-exchange platform described what they need help with. "
        "They may not know the name of the skill. Work out the specific skill(s) that would solve it.\n\n"
        f"Today is {today.strftime('%A, %Y-%m-%d')} (campus time, New York).\n"
        f"Skills peers currently offer (the catalog): {json.dumps(sorted(catalog))}\n\n"
        "Rules:\n"
        "- Return 1 to 3 skills, most useful first. Be specific (\"pivot tables\", not \"computers\").\n"
        "- catalog_match must be copied exactly from the catalog when a catalog skill covers the need "
        "(for example pivot tables are covered by \"excel\"); otherwise an empty string.\n"
        "- keywords: 3 to 6 lowercase words or short phrases a teacher might list for this skill.\n"
        "- deadline_date: if they mention a deadline, the last date a session could still help. "
        "\"Before Friday\" means the Thursday before that Friday. Empty string if none.\n"
        "- hours: 1 unless the need clearly takes longer. Maximum 3.\n\n"
        f"Student's words:\n\"\"\"{need}\"\"\""
    )
    resp = client.beta.messages.create(
        model=AI_MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": NEED_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


SYNONYMS = {  # used only when the AI is unavailable
    "spreadsheet": ["excel", "google sheets", "pivot tables"], "rows": ["excel", "pivot tables"], "pivot": ["excel", "pivot tables"],
    "summary": ["excel", "pivot tables", "data analysis"], "chart": ["excel", "data analysis"], "slides": ["pitch decks", "public speaking"],
    "presentation": ["public speaking", "pitch decks"], "resume": ["resume review"], "cv": ["resume review"], "interview": ["interview prep", "case interviews"],
    "logo": ["logo design", "figma"], "website": ["web development", "figma"], "code": ["python"], "script": ["python"], "database": ["sql"],
    "photo": ["photography"], "video": ["video editing"],
}


def keyword_interpret(need, catalog):
    words = re.findall(r"[a-z]+", need.lower())
    hits = []
    for w in words:
        for s in SYNONYMS.get(w, []) + [c for c in catalog if w in c.split() and len(w) > 3]:
            if s not in hits:
                hits.append(s)
    skills_out = [{"name": s, "catalog_match": s if s in catalog else "", "keywords": [s]} for s in hits[:3]]
    return {"summary": clean(need, 80), "explanation": "Matched on keywords in your request.", "skills": skills_out,
            "session_title": clean(need, 60), "hours": 1, "deadline_date": ""}


def score_teachers(plan, me, users, sessions, stats, today):
    """Rank real peers for the interpreted need and explain every point of the score."""
    try:
        deadline = date.fromisoformat(plan.get("deadline_date") or "")
    except ValueError:
        deadline = None
    horizon = max(7, (deadline - today).days + 1) if deadline and deadline >= today else 7
    taken = {(s["teacher"], s.get("slot") or "") for s in sessions if s["status"] in ("requested", "accepted") and s.get("slot")}
    mine = set(me["teaches"]) if me else set()
    results = []
    for u in users:
        if me and u["id"] == me["id"]:
            continue
        score, reasons, matched = 0, [], None
        for i, sk in enumerate(plan["skills"]):
            cat, name = sk.get("catalog_match", "").lower(), sk["name"].lower()
            if cat and cat in u["teaches"]:
                matched = cat
                score += 50 - i * 10
                reasons.append({"kind": "skill", "text": f"Teaches {cat.title()}" + (f", which covers {name}" if name != cat else "")})
                break
            kw = [k.lower() for k in sk.get("keywords", [])] + [name]
            hit = next((t for t in u["teaches"] if any(k in t or t in k for k in kw if len(k) > 2)), None)
            if hit:
                matched = hit
                score += 35 - i * 10
                reasons.append({"kind": "skill", "text": f"Teaches {hit.title()}, related to {name}"})
                break
        if not matched:
            continue
        open_slots = [s for s in upcoming_slots(u["availability"], days=horizon, start=today) if (u["id"], s["slot"]) not in taken]
        before = [s for s in open_slots if not deadline or date.fromisoformat(s["date"]) <= deadline]
        if before:
            score += 20
            first = slot_label(before[0]["slot"])
            reasons.append({"kind": "time", "text": f"Free {first}" + (f", before your {(deadline + timedelta(days=1)).strftime('%A')} deadline" if deadline else "")})
        elif open_slots:
            score -= 15
            reasons.append({"kind": "warn", "text": f"Next free {slot_label(open_slots[0]['slot'])}, after your deadline"})
        else:
            score -= 5
            reasons.append({"kind": "warn", "text": "Hasn't set available times yet; you can request a flexible time"})
        st = stats.get(u["id"])
        if st and st["avg"]:
            score += int(st["avg"] * 3)
            reasons.append({"kind": "trust", "text": f"Rated {st['avg']}★ across {st['taught']} session{'s' if st['taught'] != 1 else ''}"})
        else:
            reasons.append({"kind": "trust", "text": "New to SkillSwap, no reviews yet"})
        swap = next((w for w in u["wants"] if w in mine), None)
        if swap:
            score += 8
            reasons.append({"kind": "swap", "text": f"Wants to learn {swap.title()}, which you teach, so you can help each other"})
        results.append({"user": u, "skill": matched, "score": score, "reasons": reasons, "slots": (before or open_slots)[:4]})
    results.sort(key=lambda r: -r["score"])
    return results[:3]


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ---------- HTTP ----------
class handler(BaseHTTPRequestHandler):
    server_version = "SkillSwap/1.2"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "Request too large.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON.")

    def me(self, d, required=True):
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        row = d.one("SELECT * FROM users WHERE token = ?", (tok,)) if tok else None
        if required and not row:
            raise ApiError(401, "Please join first.")
        return row

    def client_ip(self):
        return (self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0]
                or self.client_address[0]).strip()

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        try:
            # On Vercel the rewrite delivers every call as /api/index?route=<original path>;
            # locally the original path arrives as-is.
            from urllib.parse import parse_qs, urlsplit
            u = urlsplit(self.path)
            route = (parse_qs(u.query).get("route") or [""])[0] or u.path.split("/api/", 1)[-1]
            path = "/api/" + route.strip("/")
            if path == "/api/index" or not self.path.startswith("/api"):
                raise ApiError(404, "Not found.")
            init_db()
            with DB() as d:
                if method == "GET" and path == "/api/state":
                    return self.send_json(200, self.state(d))
                if method == "POST" and path == "/api/join":
                    return self.send_json(200, self.join(d, self.body()))
                if method == "POST" and path == "/api/login":
                    return self.send_json(200, self.login(d, self.body()))
                if method == "PUT" and path == "/api/profile":
                    return self.send_json(200, self.profile(d, self.body()))
                if method == "POST" and path == "/api/match":
                    return self.send_json(200, self.match(d, self.body()))
                if method == "POST" and path == "/api/sessions":
                    return self.send_json(200, self.book(d, self.body()))
                m = re.fullmatch(r"/api/sessions/([A-Za-z0-9_-]{6,40})/(accept|decline|cancel|complete)", path)
                if method == "POST" and m:
                    return self.send_json(200, self.act(d, m.group(1), m.group(2), self.body()))
                raise ApiError(404, "Not found.")
        except ApiError as e:
            self.send_json(e.status, {"error": e.msg})
        except Exception as e:  # never leak internals to the client
            print("ERROR", repr(e))
            self.send_json(500, {"error": "Something went wrong. Try again."})

    # ---------- API ----------
    def state(self, d):
        me = self.me(d, required=False)
        users = [public_user(r) for r in d.all("SELECT * FROM users ORDER BY created DESC")]
        sessions = d.all("SELECT * FROM sessions")
        names = {u["id"]: u["name"] for u in users}
        taken = sorted({s["teacher"] + "#" + s["slot"] for s in sessions if s["status"] in ("requested", "accepted") and s.get("slot")})
        out = {"members": users, "stats": teacher_stats(sessions), "campus": campus(users, sessions), "taken": taken,
               "today": campus_today().isoformat(), "aiEnabled": bool(os.environ.get("ANTHROPIC_API_KEY")), "storage": STORAGE,
               "rules": {"start": START_CREDITS, "maxHours": MAX_HOURS, "pairCap": PAIR_WEEKLY_CAP, "freeActive": FREE_ACTIVE}}
        if me:
            mine = [s for s in sessions if me["id"] in (s["learner"], s["teacher"])]
            out["me"] = {**public_user(me), "code": me["code"]}
            out["wallet"] = ledger(me["id"], sessions, names)
            out["sessions"] = sorted(mine, key=lambda s: s["created"], reverse=True)
        return out

    def join(self, d, b):
        rate_limit("join:" + self.client_ip(), 30)
        name, teaches = clean(b.get("name"), 60), skills(b.get("teaches"))
        if not name:
            raise ApiError(400, "Add your name.")
        if not teaches:
            raise ApiError(400, "Add at least one skill you can teach.")
        uid, tok, code = "u_" + secrets.token_urlsafe(9), secrets.token_urlsafe(24), secrets.token_hex(3).upper()
        d.run("INSERT INTO users (id, token, code, name, major, headline, teaches, wants, availability, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (uid, tok, code, name, clean(b.get("major"), 60), clean(b.get("headline"), 100),
               json.dumps(teaches), json.dumps(skills(b.get("wants"))), json.dumps(availability(b.get("availability"))), now()))
        return {"token": tok}

    def login(self, d, b):
        rate_limit("login:" + self.client_ip(), 60)
        code = clean(b.get("code"), 12).upper()
        row = d.one("SELECT token FROM users WHERE code = ?", (code,)) if code else None
        if not row:
            raise ApiError(404, "That code doesn't match anyone.")
        return {"token": row["token"]}

    def profile(self, d, b):
        me = self.me(d)
        name, teaches = clean(b.get("name"), 60), skills(b.get("teaches"))
        if not name or not teaches:
            raise ApiError(400, "Name and at least one teachable skill are required.")
        d.run("UPDATE users SET name=?, major=?, headline=?, teaches=?, wants=?, availability=? WHERE id=?",
              (name, clean(b.get("major"), 60), clean(b.get("headline"), 100), json.dumps(teaches),
               json.dumps(skills(b.get("wants"))), json.dumps(availability(b.get("availability"))), me["id"]))
        return {"ok": True}

    def match(self, d, b):
        me_row = self.me(d)
        rate_limit("match:" + me_row["id"], 40)
        need = clean(b.get("need"), 500)
        if len(need) < 8:
            raise ApiError(400, "Tell us a bit more about what you need.")
        users = [public_user(r) for r in d.all("SELECT * FROM users")]
        sessions = d.all("SELECT * FROM sessions")
        catalog = sorted({t for u in users for t in u["teaches"] if u["id"] != me_row["id"]})
        today = campus_today()
        source = "ai"
        try:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("no key")
            plan = ai_interpret(need, catalog, today)
        except Exception as e:
            print("AI fallback:", repr(e))
            plan, source = keyword_interpret(need, catalog), "keywords"
        plan["skills"] = [s for s in plan.get("skills", []) if s.get("name")][:3]
        plan["hours"] = max(1, min(MAX_HOURS, int(plan.get("hours") or 1)))
        me = public_user(me_row)
        matches = score_teachers(plan, me, users, sessions, teacher_stats(sessions), today)
        for m in matches:
            for s in m["slots"]:
                s["label"] = slot_label(s["slot"])
        return {"source": source, "plan": plan, "matches": matches, "need": need,
                "deadline_label": (date.fromisoformat(plan["deadline_date"]).strftime("%a %b %-d") if plan.get("deadline_date") else "")}

    def book(self, d, b):
        me = self.me(d)
        d.lock_user(me["id"])
        teacher = d.one("SELECT * FROM users WHERE id = ?", (str(b.get("teacher", "")),))
        if not teacher:
            raise ApiError(404, "That person isn't on SkillSwap.")
        if teacher["id"] == me["id"]:
            raise ApiError(400, "You can't book yourself.")
        skill = clean(b.get("skill"), 40).lower()
        if skill not in json.loads(teacher["teaches"]):
            raise ApiError(400, f"{teacher['name']} doesn't teach that.")
        try:
            hours = int(b.get("hours", 1))
        except (TypeError, ValueError):
            hours = 1
        if not 1 <= hours <= MAX_HOURS:
            raise ApiError(400, f"Sessions are 1 to {MAX_HOURS} hours.")
        slot = clean(b.get("slot"), 30)
        if slot:
            valid = {s["slot"] for s in upcoming_slots(json.loads(teacher.get("availability") or "[]"), days=21)}
            if slot not in valid:
                raise ApiError(400, f"{teacher['name']} isn't available then. Pick another time.")
            clash = d.one("SELECT id FROM sessions WHERE teacher = ? AND slot = ? AND status IN ('requested','accepted')", (teacher["id"], slot))
            if clash:
                raise ApiError(409, "Someone just booked that time. Pick another.")
        sessions = d.all("SELECT * FROM sessions WHERE learner = ? OR teacher = ?", (me["id"], me["id"]))
        active = sum(1 for s in sessions if s["learner"] == me["id"] and s["status"] in ("requested", "accepted"))
        if active >= FREE_ACTIVE:
            raise ApiError(402, f"Free accounts can have {FREE_ACTIVE} active swaps. Finish one first.")
        bal = ledger(me["id"], sessions, {})["balance"]
        if bal < hours:
            raise ApiError(402, f"This costs {hours} credit{'s' if hours > 1 else ''} and you have {bal}. Teach a session to earn more.")
        sid = "s_" + secrets.token_urlsafe(9)
        d.run("INSERT INTO sessions (id, learner, teacher, skill, hours, note, status, created, slot) VALUES (?,?,?,?,?,?,?,?,?)",
              (sid, me["id"], teacher["id"], skill, hours, clean(b.get("note"), 300), "requested", now(), slot))
        return {"ok": True, "id": sid}

    def act(self, d, sid, action, b):
        me = self.me(d)
        s = d.one("SELECT * FROM sessions WHERE id = ?", (sid,))
        if not s:
            raise ApiError(404, "Session not found.")
        is_teacher, is_learner = s["teacher"] == me["id"], s["learner"] == me["id"]
        allowed = {
            "accept": is_teacher and s["status"] == "requested",
            "decline": is_teacher and s["status"] == "requested",
            "cancel": is_learner and s["status"] in ("requested", "accepted"),
            "complete": is_learner and s["status"] == "accepted",  # only the payer can confirm
        }
        if not allowed[action]:
            raise ApiError(403, "You can't do that on this session.")
        if action == "complete":
            try:
                rating = max(1, min(5, int(b.get("rating", 5))))
            except (TypeError, ValueError):
                rating = 5
            d.run("UPDATE sessions SET status='completed', rating=?, review=?, completed_at=? WHERE id=?",
                  (rating, clean(b.get("review"), 240), now(), sid))
        else:
            new = {"accept": "accepted", "decline": "declined", "cancel": "cancelled"}[action]
            d.run("UPDATE sessions SET status=? WHERE id=?", (new, sid))
        return {"ok": True}
