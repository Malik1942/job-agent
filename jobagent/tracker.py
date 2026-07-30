"""SQLite-backed tracker plus a plain-CSV log of submitted jobs.

The SQLite table is the full record (one row per job uid, so a job is never
applied to twice). The CSV is a human-friendly append-only log of jobs that were
actually submitted: open it in Excel or Numbers to see everything the agent has
applied to. Which statuses get logged is configurable (default: applied only).
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3

from .models import ApplicationRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    uid               TEXT PRIMARY KEY,
    company           TEXT,
    title             TEXT,
    url               TEXT,
    score             REAL,
    status            TEXT,
    note              TEXT,
    cover_letter_path TEXT,
    location          TEXT,
    source            TEXT,
    created_at        TEXT
);
"""

# CSV columns. Chinese headers to match the request; utf-8-sig so Excel on
# macOS / Windows renders them correctly.
CSV_HEADERS = ["时间", "公司", "职位", "地点", "来源", "匹配分",
               "状态", "求职信", "岗位链接", "详细信息"]


class Tracker:
    def __init__(self, db_path: str, csv_path: str = "",
                 csv_statuses: list[str] | None = None):
        self.db_path = db_path
        self.csv_path = csv_path
        self.csv_statuses = csv_statuses if csv_statuses is not None else ["applied"]
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
        # Remember which job links are already in the CSV, so re-runs never
        # write a duplicate row.
        self._csv_urls = self._load_csv_urls()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, c) -> None:
        # Add columns that older databases may not have.
        have = {r["name"] for r in c.execute("PRAGMA table_info(applications)")}
        for col in ("location", "source", "screenshot",
                    "filled_fields", "unanswered_required"):
            if col not in have:
                c.execute(f"ALTER TABLE applications ADD COLUMN {col} TEXT")

    # ---- SQLite ------------------------------------------------------------
    def seen(self, uid: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM applications WHERE uid = ?", (uid,)).fetchone()
            return row is not None

    _DECIDED = ("applied", "submitted_unverified", "closed_before_submit")

    def decided_uids(self, statuses: tuple[str, ...] = _DECIDED) -> set[str]:
        """Uids that reached a terminal outcome — used to keep already-
        submitted jobs out of fresh review queues."""
        marks = ",".join("?" for _ in statuses)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT uid FROM applications WHERE status IN ({marks})",
                statuses,
            ).fetchall()
            return {r[0] for r in rows}

    def decided_urls(self, statuses: tuple[str, ...] = _DECIDED) -> set[str]:
        """Normalized urls of terminally-decided jobs. Complements
        decided_uids: older records made from queue items lack external_id,
        so their hash uids never match a fresh scan's external uid — the url
        still does."""
        marks = ",".join("?" for _ in statuses)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT url FROM applications WHERE status IN ({marks})",
                statuses,
            ).fetchall()
            return {(r[0] or "").rstrip("/").lower() for r in rows if r[0]}

    def record(self, rec: ApplicationRecord) -> None:
        import json as _json
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO applications
                   (uid, company, title, url, score, status, note,
                    cover_letter_path, location, source, created_at,
                    screenshot, filled_fields, unanswered_required)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec.uid, rec.company, rec.title, rec.url, rec.score, rec.status,
                 rec.note, rec.cover_letter_path, rec.location, rec.source,
                 rec.created_at, rec.screenshot,
                 _json.dumps(rec.filled_fields or []),
                 _json.dumps(rec.unanswered_required or [])),
            )
        # Mirror submitted jobs into the CSV log.
        self._maybe_log_csv(rec)

    def all(self) -> list[ApplicationRecord]:
        import json as _json
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM applications ORDER BY created_at DESC"
            ).fetchall()
        records = []
        for r in rows:
            d = dict(r)
            for col in ("filled_fields", "unanswered_required"):
                raw = d.get(col)
                try:
                    d[col] = _json.loads(raw) if raw else []
                except (TypeError, ValueError):
                    d[col] = []
            d["screenshot"] = d.get("screenshot") or ""
            records.append(ApplicationRecord(**d))
        return records

    def counts(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) n FROM applications GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- CSV log -----------------------------------------------------------
    def _load_csv_urls(self) -> set[str]:
        if not self.csv_path or not Path(self.csv_path).exists():
            return set()
        urls = set()
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    link = row.get("岗位链接")
                    if link:
                        urls.add(link)
        except Exception:
            pass
        return urls

    def _maybe_log_csv(self, rec: ApplicationRecord) -> None:
        if not self.csv_path or rec.status not in self.csv_statuses:
            return
        if rec.url and rec.url in self._csv_urls:
            return  # already logged this job

        path = Path(self.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rec.company,
            rec.title,
            rec.location,
            rec.source,
            f"{rec.score:.2f}",
            rec.status,
            rec.cover_letter_path,
            rec.url,
            rec.note,
        ]
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(CSV_HEADERS)
            w.writerow(row)
        if rec.url:
            self._csv_urls.add(rec.url)
