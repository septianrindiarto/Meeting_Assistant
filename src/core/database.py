"""
Meeting Scribe — SQLite Database with FTS5 Full-Text Search
Indexes meeting bundles for fast search across past meetings.
"""
from __future__ import annotations

import os
import sqlite3
import logging
import time
from typing import List, Optional, Dict

from src.utils.file_utils import get_app_data_dir

logger = logging.getLogger(__name__)


class SearchResult:
    """A single search result from FTS5."""
    def __init__(self, meeting_id: int, title: str, date: str,
                 duration: str, speakers: str, bundle_path: str,
                 snippet: str = "", rank: float = 0.0):
        self.meeting_id = meeting_id
        self.title = title
        self.date = date
        self.duration = duration
        self.speakers = speakers
        self.bundle_path = bundle_path
        self.snippet = snippet
        self.rank = rank


class MeetingDatabase:
    """
    SQLite database with FTS5 for indexing and searching meetings.

    The database is a local index — the actual data lives in .mscribe bundles.
    If the database is lost, it can be rebuilt from the bundles.

    Usage:
        db = MeetingDatabase()
        db.index_bundle(bundle_path, metadata, transcript_text)
        results = db.search("budget AND Q3")
        meetings = db.list_meetings()
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Args:
            db_path: Path to SQLite database file.
                     Defaults to %APPDATA%/MeetingScribe/meetings.db
        """
        if db_path is None:
            db_path = str(get_app_data_dir() / "meetings.db")

        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a database connection."""
        if self._conn is None:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent read performance
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist.

        Note: we deliberately do NOT use AFTER INSERT / AFTER DELETE triggers
        on the meetings table to keep the FTS index in sync. Earlier versions
        of this app did, and the trigger fought with the explicit Python writes
        in index_bundle(), causing the whole transaction to fail silently and
        the meeting to never appear in the home view. All FTS writes are
        owned by index_bundle()/delete_meeting() instead.
        """
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'Untitled Meeting',
                date TEXT NOT NULL DEFAULT '',
                duration TEXT NOT NULL DEFAULT '',
                duration_seconds REAL DEFAULT 0,
                speakers TEXT NOT NULL DEFAULT '',
                bundle_path TEXT NOT NULL UNIQUE,
                file_size_mb REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
                title,
                transcript_text,
                speakers,
                content='meetings',
                content_rowid='id',
                tokenize='porter unicode61'
            );

            -- Drop legacy triggers from older schema versions if present.
            DROP TRIGGER IF EXISTS meetings_ai;
            DROP TRIGGER IF EXISTS meetings_ad;
        """)
        conn.commit()
        logger.debug("Database schema ensured")

    def index_bundle(self, bundle_path: str, title: str = "",
                     date: str = "", duration: str = "",
                     duration_seconds: float = 0.0,
                     speakers: str = "",
                     transcript_text: str = "",
                     file_size_mb: float = 0.0) -> int:
        """
        Add or update a meeting in the index.

        Args:
            bundle_path: Path to the .mscribe file.
            title: Meeting title.
            date: ISO 8601 date string.
            duration: Human-readable duration.
            duration_seconds: Duration in seconds.
            speakers: Comma-separated speaker names.
            transcript_text: Full transcript text for FTS indexing.
            file_size_mb: Bundle file size in MB.

        Returns:
            The meeting ID.
        """
        conn = self._get_conn()

        try:
            # Check if already indexed
            existing = conn.execute(
                "SELECT id FROM meetings WHERE bundle_path = ?",
                (bundle_path,)
            ).fetchone()

            if existing:
                meeting_id = existing['id']
                conn.execute("""
                    UPDATE meetings SET
                        title = ?, date = ?, duration = ?,
                        duration_seconds = ?, speakers = ?,
                        file_size_mb = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (title, date, duration, duration_seconds,
                      speakers, file_size_mb, meeting_id))

                # Remove stale FTS row, then re-insert with current data.
                conn.execute(
                    "INSERT INTO meetings_fts(meetings_fts, rowid, title, "
                    "transcript_text, speakers) VALUES ('delete', ?, ?, ?, ?)",
                    (meeting_id, title, '', speakers)
                )
            else:
                cursor = conn.execute("""
                    INSERT INTO meetings (title, date, duration, duration_seconds,
                                          speakers, bundle_path, file_size_mb)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (title, date, duration, duration_seconds,
                      speakers, bundle_path, file_size_mb))
                meeting_id = cursor.lastrowid

            # Single FTS write owns the index — no triggers, no double writes.
            conn.execute("""
                INSERT INTO meetings_fts(rowid, title, transcript_text, speakers)
                VALUES (?, ?, ?, ?)
            """, (meeting_id, title, transcript_text, speakers))

            conn.commit()
            logger.debug(f"Indexed meeting {meeting_id}: {title}")
            return meeting_id

        except Exception as e:
            # Surface the error rather than rolling back silently — this is what
            # caused the original "new meeting doesn't appear" bug.
            conn.rollback()
            logger.error(
                f"Failed to index meeting (bundle={bundle_path}): {e}",
                exc_info=True
            )
            raise

    def search(self, query: str, limit: int = 50) -> List[SearchResult]:
        """
        Full-text search across meetings.

        Args:
            query: Search query (supports AND, OR, NOT, prefix*).
            limit: Maximum results to return.

        Returns:
            List of SearchResult objects, ranked by relevance.
        """
        conn = self._get_conn()

        try:
            rows = conn.execute("""
                SELECT m.id, m.title, m.date, m.duration, m.speakers,
                       m.bundle_path,
                       snippet(meetings_fts, 1, '<b>', '</b>', '...', 20) as snippet,
                       rank
                FROM meetings_fts f
                JOIN meetings m ON m.id = f.rowid
                WHERE meetings_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()

            return [
                SearchResult(
                    meeting_id=row['id'],
                    title=row['title'],
                    date=row['date'],
                    duration=row['duration'],
                    speakers=row['speakers'],
                    bundle_path=row['bundle_path'],
                    snippet=row['snippet'] or "",
                    rank=row['rank'],
                )
                for row in rows
            ]
        except sqlite3.OperationalError as e:
            logger.warning(f"Search query error: {e}")
            return []

    def list_meetings(self, sort_by: str = "created_at",
                      order: str = "DESC",
                      limit: int = 100) -> List[Dict]:
        """
        List all indexed meetings.

        Args:
            sort_by: Column to sort by (created_at, date, title, duration_seconds).
                     Defaults to created_at so newly-saved meetings always appear
                     first, even if they share the same calendar date as others.
            order: Sort order (ASC or DESC).
            limit: Maximum results.

        Returns:
            List of meeting metadata dicts.
        """
        conn = self._get_conn()

        # Sanitize sort column
        allowed_sorts = {"date", "title", "duration_seconds", "created_at"}
        if sort_by not in allowed_sorts:
            sort_by = "created_at"
        order = "DESC" if order.upper() != "ASC" else "ASC"

        rows = conn.execute(f"""
            SELECT * FROM meetings
            ORDER BY {sort_by} {order}
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]

    def delete_meeting(self, meeting_id: int) -> None:
        """
        Remove a meeting from the index.
        Does NOT delete the .mscribe bundle file.
        """
        conn = self._get_conn()
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        logger.info(f"Removed meeting {meeting_id} from index")

    def rebuild_index(self, project_folder: str) -> int:
        """
        Rebuild the entire index by scanning a folder for .mscribe bundles.
        Used when moving to a new machine or recovering from database loss.

        Args:
            project_folder: Directory containing .mscribe files.

        Returns:
            Number of bundles indexed.
        """
        from src.core.bundle_manager import BundleManager

        logger.info(f"Rebuilding index from: {project_folder}")
        start = time.time()

        manager = BundleManager()
        bundles = manager.list_bundles(project_folder)

        count = 0
        for meta in bundles:
            try:
                # Open bundle to get transcript text
                bundle_path = meta["bundle_path"]
                meeting = manager.open_bundle(bundle_path)

                transcript_text = " ".join(
                    seg.text for seg in meeting.transcript
                )

                self.index_bundle(
                    bundle_path=bundle_path,
                    title=meta.get("title", "Untitled"),
                    date=meta.get("date", ""),
                    duration=meta.get("duration", ""),
                    duration_seconds=meta.get("duration_seconds", 0),
                    speakers=", ".join(meta.get("attendees", [])),
                    transcript_text=transcript_text,
                    file_size_mb=meta.get("file_size_mb", 0),
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to index {meta.get('bundle_path')}: {e}")

        elapsed = time.time() - start
        logger.info(f"Index rebuilt: {count} bundles in {elapsed:.1f}s")
        return count

    def get_stats(self) -> Dict:
        """Return database statistics for display in the UI."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT
                COUNT(*) as total_meetings,
                SUM(duration_seconds) as total_duration,
                SUM(file_size_mb) as total_size_mb
            FROM meetings
        """).fetchone()

        return {
            "total_meetings": row['total_meetings'] or 0,
            "total_duration_hours": (row['total_duration'] or 0) / 3600,
            "total_size_mb": row['total_size_mb'] or 0,
        }

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
