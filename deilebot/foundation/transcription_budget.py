"""Monthly STT usage budget — atomic check+increment in sqlite."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class TranscriptionBudgetTracker:
    """Tracks cumulative transcription minutes per calendar month in sqlite.

    Uses an asyncio lock to serialize concurrent try_reserve calls on the
    same process, preventing TOCTOU between simultaneous audio messages.
    The lock is process-local; across replicas, sqlite's BEGIN IMMEDIATE
    provides the serialization guarantee.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS transcription_budget (
        month_key TEXT PRIMARY KEY,
        used_minutes REAL NOT NULL DEFAULT 0.0
    )
    """

    def __init__(self, sqlite_path: Path):
        self._path = sqlite_path
        self._lock: asyncio.Lock = asyncio.Lock()
        self._db = None

    @staticmethod
    def _month_key(dt: Optional[datetime] = None) -> str:
        if dt is None:
            dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m")

    async def _get_db(self):
        if self._db is None:
            import aiosqlite
            self._db = await aiosqlite.connect(str(self._path))
            await self._db.execute(self._DDL)
            await self._db.commit()
        return self._db

    async def get_used_minutes(self, month_key: Optional[str] = None) -> float:
        if month_key is None:
            month_key = self._month_key()
        db = await self._get_db()
        async with db.execute(
            "SELECT used_minutes FROM transcription_budget WHERE month_key = ?",
            (month_key,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0.0

    async def try_reserve(
        self,
        duration_seconds: float,
        max_minutes: float,
        month_key: Optional[str] = None,
    ) -> bool:
        """Atomically check + increment. Returns True if allowed, False if over budget."""
        if month_key is None:
            month_key = self._month_key()
        duration_minutes = duration_seconds / 60.0
        async with self._lock:
            db = await self._get_db()
            # BEGIN IMMEDIATE serializes concurrent writers at the sqlite level
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT used_minutes FROM transcription_budget WHERE month_key = ?",
                    (month_key,),
                ) as cur:
                    row = await cur.fetchone()
                used = row[0] if row else 0.0
                if used + duration_minutes > max_minutes:
                    await db.execute("ROLLBACK")
                    return False
                await db.execute(
                    """
                    INSERT INTO transcription_budget (month_key, used_minutes)
                    VALUES (?, ?)
                    ON CONFLICT(month_key) DO UPDATE SET used_minutes = used_minutes + ?
                    """,
                    (month_key, duration_minutes, duration_minutes),
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise
        return True

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
