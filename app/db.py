"""SQLite storage: reviews, Booking's own category scores, and a log of every collection run."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "reviews.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id        TEXT PRIMARY KEY,      -- Booking's own review key (reviewUrl), stable across scrapes
    property_slug    TEXT NOT NULL,
    property_name    TEXT NOT NULL,
    score            REAL NOT NULL,         -- 1..10 Booking scale
    title            TEXT,
    positive_text    TEXT,
    negative_text    TEXT,
    review_lang      TEXT,
    reviewed_at      TEXT NOT NULL,         -- ISO date the review was published (UTC)
    checkin_date     TEXT,
    checkout_date    TEXT,
    num_nights       INTEGER,
    room_type        TEXT,
    traveller_type   TEXT,                  -- Couple / Family / Solo traveller ...
    guest_country    TEXT,
    hotel_reply      TEXT,
    helpful_votes    INTEGER,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    raw_json         TEXT NOT NULL          -- full source record, so we can re-parse without re-scraping
);
CREATE INDEX IF NOT EXISTS ix_reviews_prop_date ON reviews(property_slug, reviewed_at);

CREATE TABLE IF NOT EXISTS category_scores (
    property_slug TEXT NOT NULL,
    category      TEXT NOT NULL,            -- e.g. Cleanliness, Staff, Value for money
    score         REAL,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (property_slug, category, captured_at)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    property_slug  TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,           -- running / ok / partial / failed
    method         TEXT,                    -- graphql / dom-fallback
    fetched        INTEGER DEFAULT 0,
    inserted       INTEGER DEFAULT 0,
    updated        INTEGER DEFAULT 0,
    rejected       INTEGER DEFAULT 0,
    total_on_site  INTEGER,
    message        TEXT
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


UPSERT_COLS = [
    "review_id", "property_slug", "property_name", "score", "title", "positive_text",
    "negative_text", "review_lang", "reviewed_at", "checkin_date", "checkout_date", "num_nights",
    "room_type", "traveller_type", "guest_country", "hotel_reply", "helpful_votes",
    "first_seen_at", "last_seen_at", "raw_json",
]


def upsert_review(conn: sqlite3.Connection, rec: dict) -> str:
    """Insert a new review or refresh a known one. Returns 'inserted', 'updated' or 'unchanged'.

    A review can legitimately change after publication (guest edits it, hotel adds a reply), so
    known reviews are updated in place rather than duplicated. first_seen_at is never overwritten.
    """
    existing = conn.execute(
        "SELECT raw_json FROM reviews WHERE review_id = ?", (rec["review_id"],)
    ).fetchone()
    if existing is None:
        placeholders = ",".join("?" for _ in UPSERT_COLS)
        conn.execute(
            f"INSERT INTO reviews ({','.join(UPSERT_COLS)}) VALUES ({placeholders})",
            [rec[c] for c in UPSERT_COLS],
        )
        return "inserted"
    if existing["raw_json"] == rec["raw_json"]:
        conn.execute("UPDATE reviews SET last_seen_at=? WHERE review_id=?",
                     (rec["last_seen_at"], rec["review_id"]))
        return "unchanged"
    cols = [c for c in UPSERT_COLS if c not in ("review_id", "first_seen_at")]
    conn.execute(
        f"UPDATE reviews SET {','.join(c + '=?' for c in cols)} WHERE review_id=?",
        [rec[c] for c in cols] + [rec["review_id"]],
    )
    return "updated"


def known_ids(conn: sqlite3.Connection, slug: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT review_id FROM reviews WHERE property_slug=?", (slug,))}


def start_run(conn, slug: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (property_slug, started_at, status) VALUES (?,?, 'running')",
        (slug, started_at),
    )
    conn.commit()
    return cur.lastrowid


LOCK_PATH = DEFAULT_DB.parent / "collector.lock"


def acquire_collector_lock():
    """Return an open, locked file handle, or None if another collector holds the lock."""
    import fcntl
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def collector_running() -> bool:
    fh = acquire_collector_lock()
    if fh is None:
        return True
    fh.close()  # closing releases the lock straight away
    return False


def mark_interrupted_runs(conn) -> None:
    """Call while holding the lock: any run still marked 'running' was killed mid-way."""
    conn.execute("""UPDATE scrape_runs SET status='failed', finished_at=NULL,
                    message='interrupted before finishing' WHERE status='running'""")
    conn.commit()


def finish_run(conn, run_id: int, **fields) -> None:
    keys = list(fields)
    conn.execute(
        f"UPDATE scrape_runs SET {','.join(k + '=?' for k in keys)} WHERE id=?",
        [fields[k] for k in keys] + [run_id],
    )
    conn.commit()


def save_category_scores(conn, slug: str, scores: dict[str, float], captured_at: str) -> None:
    for cat, val in scores.items():
        conn.execute(
            "INSERT OR REPLACE INTO category_scores VALUES (?,?,?,?)", (slug, cat, val, captured_at)
        )


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
