import os, time, uuid, sqlite3, json
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from openai import OpenAI, APITimeoutError, RateLimitError, APIError

load_dotenv()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(timeout=20)  # reads OPENAI_API_KEY from .env
BASE = Path(__file__).parent
DB = BASE / "chat.db"
BOT = "Aira"

SYSTEM = """You are {bot}, the friendly help-desk assistant of RVITM College.
PERSONA: polite, short sentences, simple English.
SCOPE: answer ONLY RVITM college questions using the KNOWLEDGE below.
Decline anything else (assignments, politics, general trivia) politely and say what you can help with.
HONESTY: use only facts in KNOWLEDGE. If an in-scope answer is missing, say
"I don't have that information. Please contact the college office." Never guess or invent numbers, dates or names.
SAFETY: never ask for personal data, passwords or OTPs. Send sensitive/complaint matters to a human at the office.
KNOWLEDGE:
{kb}"""

JUDGE = """You are a strict fact-checker. Given SOURCE and an ANSWER, split the answer into atomic factual claims
and label each: "supported" (stated in source), "unsupported" (not in source), or "contradicted" (conflicts with source).
Ignore greetings, refusals and offers of help. Return JSON only:
{"claims":[{"claim":"...","verdict":"supported|unsupported|contradicted","evidence":"short source quote or empty"}]}"""

app = FastAPI(title="TruthLens")

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS messages(message_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, latency_ms INTEGER, tokens_used INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS feedback(feedback_id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER, rating INTEGER);
        CREATE TABLE IF NOT EXISTS verifications(message_id INTEGER PRIMARY KEY, score REAL, details TEXT);
        CREATE TABLE IF NOT EXISTS test_results(test_id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, expected TEXT,
            actual TEXT, correct INTEGER, latency_ms INTEGER);""")
init()

def kb(): return (BASE / "knowledge.md").read_text(encoding="utf-8")  # re-read each call: edit file, no code change

def llm(messages, **kw):
    try:
        return client.chat.completions.create(model=MODEL, messages=messages, **kw)
    except RateLimitError: raise HTTPException(429, "Too many requests. Please wait a moment and retry.")
    except APITimeoutError: raise HTTPException(504, "The AI took too long. Please try again.")
    except APIError as e: raise HTTPException(502, f"AI service error: {getattr(e, 'message', 'unknown')}")

class ChatIn(BaseModel):
    session_id: str
    message: str

@app.post("/api/chat")
def chat(body: ChatIn):
    text = body.message.strip()
    if not text: raise HTTPException(400, "Please type a message.")
    if len(text) > 1000: raise HTTPException(400, "Message too long (max 1000 characters).")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO sessions(session_id) VALUES(?)", (body.session_id,))
        rows = c.execute("SELECT role,content FROM messages WHERE session_id=? ORDER BY message_id DESC LIMIT 10",
                         (body.session_id,)).fetchall()[::-1]
        c.execute("INSERT INTO messages(session_id,role,content) VALUES(?,?,?)", (body.session_id, "user", text))
    history = [{"role": "assistant" if r["role"] == "bot" else "user", "content": r["content"]} for r in rows]
    msgs = [{"role": "system", "content": SYSTEM.format(bot=BOT, kb=kb())}] + history + [{"role": "user", "content": text}]
    t0 = time.time()
    r = llm(msgs, temperature=0.2)
    ms = int((time.time() - t0) * 1000)
    reply = r.choices[0].message.content
    with db() as c:
        cur = c.execute("INSERT INTO messages(session_id,role,content,latency_ms,tokens_used) VALUES(?,?,?,?,?)",
                        (body.session_id, "bot", reply, ms, r.usage.total_tokens))
        mid = cur.lastrowid
    return {"reply": reply, "message_id": mid, "latency_ms": ms}

@app.post("/api/verify/{message_id}")
def verify(message_id: int):
    with db() as c:
        m = c.execute("SELECT content FROM messages WHERE message_id=? AND role='bot'", (message_id,)).fetchone()
    if not m: raise HTTPException(404, "Message not found.")
    r = llm([{"role": "system", "content": JUDGE},
             {"role": "user", "content": f"SOURCE:\n{kb()}\n\nANSWER:\n{m['content']}"}],
            temperature=0, response_format={"type": "json_object"})
    try: claims = json.loads(r.choices[0].message.content).get("claims", [])
    except Exception: raise HTTPException(502, "Could not parse verification result.")
    score = None
    if claims:
        pts = sum({"supported": 1, "unsupported": 0, "contradicted": 0}.get(x.get("verdict"), 0) for x in claims)
        score = round(100 * pts / len(claims))
    with db() as c:
        c.execute("INSERT OR REPLACE INTO verifications VALUES(?,?,?)", (message_id, score, json.dumps(claims)))
    return {"score": score, "claims": claims}

@app.get("/api/history/{session_id}")
def history(session_id: str):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY message_id", (session_id,))]

class FbIn(BaseModel):
    message_id: int
    rating: int

@app.post("/api/feedback")
def feedback(b: FbIn):
    if b.rating not in (1, -1): raise HTTPException(400, "rating must be 1 or -1")
    with db() as c: c.execute("INSERT INTO feedback(message_id,rating) VALUES(?,?)", (b.message_id, b.rating))
    return {"ok": True}

@app.post("/api/reset/{session_id}")
def reset(session_id: str):
    with db() as c: c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    return {"ok": True}

@app.get("/")
def index(): return FileResponse(BASE / "static" / "index.html")

@app.get("/api/stats")
def stats():
    with db() as c:
        v = c.execute("SELECT COUNT(score) n, AVG(score) a FROM verifications WHERE score IS NOT NULL").fetchone()
        l = c.execute("SELECT AVG(latency_ms) a FROM messages WHERE role='bot'").fetchone()
        f = c.execute("SELECT SUM(rating=1) up, SUM(rating=-1) dn FROM feedback").fetchone()
    return {"checked": v["n"], "avg_score": round(v["a"]) if v["a"] is not None else None,
            "avg_latency": round(l["a"]) if l["a"] else None, "up": f["up"] or 0, "down": f["dn"] or 0}
