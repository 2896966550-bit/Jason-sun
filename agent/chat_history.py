"""聊天历史管理 —— SQLite"""
import sqlite3
import os, time
from contextlib import closing
from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "chat_history.db")

def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, role TEXT, content TEXT, ts REAL)")
    c.commit()
    return c

def save_message(role: str, content: str):
    with closing(_conn()) as c:
        c.execute("INSERT INTO history (role, content, ts) VALUES (?, ?, ?)", (role, content, time.time()))
        c.commit()

def load_history(limit: int = 50) -> list:
    with closing(_conn()) as c:
        rows = c.execute("SELECT role, content, ts FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [(r, c, t) for r, c, t in reversed(rows)]

def get_recent_unique_questions(limit: int = 15) -> list:
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT content FROM (SELECT content, MAX(id) as mid FROM history WHERE role='user' GROUP BY content ORDER BY mid DESC LIMIT ?)",
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]

def get_full_conversation(question: str) -> list:
    with closing(_conn()) as c:
        user_row = c.execute(
            "SELECT id, ts FROM history WHERE role='user' AND content=? ORDER BY id DESC LIMIT 1",
            (question,),
        ).fetchone()
        if not user_row:
            return []
        uid, uts = user_row
        ai_row = c.execute(
            "SELECT content FROM history WHERE role='assistant' AND id > ? "
            "AND id < COALESCE((SELECT MIN(id) FROM history WHERE role='user' AND id > ?), 9223372036854775807) "
            "ORDER BY id ASC LIMIT 1",
            (uid, uid),
        ).fetchone()
    return [(question, ai_row[0])] if ai_row else [(question, "")]

def get_recent_pairs(n: int = 3) -> list:
    """Return the latest N user turns, retaining repeats and incomplete turns."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("n 必须是整数")
    if n <= 0:
        return []
    with closing(_conn()) as c:
        rows = c.execute("SELECT role, content FROM history ORDER BY id DESC LIMIT ?", (n * 2 + 10,)).fetchall()

    turns = []
    for role, content in reversed(rows):
        if role == "user":
            turns.append([{"role": "user", "content": content}])
        elif role == "assistant" and turns and len(turns[-1]) == 1:
            turns[-1].append({"role": "assistant", "content": content[:300]})
    return [message for turn in turns[-n:] for message in turn]
