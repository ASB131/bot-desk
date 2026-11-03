import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings
from app.logic import PROMPT_POOL_SLUG, WEEKDAYS, clean_name, next_occurrence, random_minutes_between


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class ImageRow:
    id: int
    name: str
    stored_name: str
    created_at: datetime


@dataclass
class EventRow:
    id: int
    name: str
    guild_id: str
    starts_at: datetime
    channel_id: str
    message: str
    image_id: int | None
    pfp_image_id: int | None
    countdown: bool
    countdown_message_id: str | None
    allow_pings: bool
    fired: bool
    missed: bool
    attempts: int
    last_error: str
    created_at: datetime
    recurrence: str
    daily_before_days: int
    daily_before_message: str
    daily_after_days: int
    daily_after_message: str
    daily_last_posted_on: str | None
    after_anchor_on: str | None
    image_name: str | None = None
    pfp_name: str | None = None


@dataclass
class AvatarJob:
    id: int
    image_id: int
    weekday: int
    hour: int
    minute: int
    last_run_on: str | None
    last_error: str
    created_at: datetime
    image_name: str


@dataclass
class ImagePool:
    id: int
    slug: str
    label: str
    created_at: datetime


@dataclass
class FactRow:
    id: int
    guild_id: str
    body: str
    created_at: datetime


@dataclass
class FactsConfig:
    enabled: bool
    channel_id: str
    min_minutes: int
    max_minutes: int
    attach_library_image: bool
    next_at: datetime | None
    last_error: str


@dataclass
class ScheduledPicturesConfig:
    enabled: bool
    channel_id: str
    min_minutes: int
    max_minutes: int
    next_at: datetime | None
    last_error: str


@dataclass
class ActivityRow:
    id: int
    kind: str
    summary: str
    detail: str
    guild: str
    channel: str
    ok: bool
    created_at: datetime


@dataclass
class EmojiRuleRow:
    id: int
    emoji: str
    pool_id: int
    pool_slug: str
    pool_label: str
    created_at: datetime


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.upload_dir = db_path.parent / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                stored_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message TEXT NOT NULL,
                image_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
                pfp_image_id INTEGER REFERENCES images(id) ON DELETE SET NULL,
                countdown INTEGER NOT NULL DEFAULT 1,
                countdown_message_id TEXT,
                allow_pings INTEGER NOT NULL DEFAULT 0,
                fired INTEGER NOT NULL DEFAULT 0,
                missed INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS avatar_jobs (
                id INTEGER PRIMARY KEY,
                image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                weekday INTEGER NOT NULL UNIQUE,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                last_run_on TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS image_pools (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pool_images (
                pool_id INTEGER NOT NULL REFERENCES image_pools(id) ON DELETE CASCADE,
                image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                PRIMARY KEY (pool_id, image_id)
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emoji_rules (
                id INTEGER PRIMARY KEY,
                emoji TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pool_id INTEGER NOT NULL REFERENCES image_pools(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                guild TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()
        self._ensure_event_columns()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS emoji_triggers (
                id INTEGER PRIMARY KEY,
                emoji TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()
        self._ensure_fact_columns()
        self._ensure_guild_scope()
        self.ensure_pool(PROMPT_POOL_SLUG, "Prompt")

    def _ensure_guild_scope(self) -> None:
        event_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "guild_id" not in event_cols:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN guild_id TEXT NOT NULL DEFAULT ''"
            )
        fact_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()
        }
        if "guild_id" not in fact_cols:
            self._conn.execute(
                "ALTER TABLE facts ADD COLUMN guild_id TEXT NOT NULL DEFAULT ''"
            )
        emoji_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(emoji_triggers)").fetchall()
        }
        if "guild_id" not in emoji_cols:
            self._conn.execute(
                "ALTER TABLE emoji_triggers ADD COLUMN guild_id TEXT NOT NULL DEFAULT ''"
            )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_facts_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT NOT NULL DEFAULT '',
                min_minutes INTEGER NOT NULL DEFAULT 60,
                max_minutes INTEGER NOT NULL DEFAULT 180,
                attach_library INTEGER NOT NULL DEFAULT 1,
                facts_next_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS guild_scheduled_pictures_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                channel_id TEXT NOT NULL DEFAULT '',
                min_minutes INTEGER NOT NULL DEFAULT 60,
                max_minutes INTEGER NOT NULL DEFAULT 180,
                next_at TEXT,
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_emoji_triggers_guild
            ON emoji_triggers (guild_id, emoji COLLATE NOCASE)
            """
        )
        self._conn.commit()
        self._import_legacy_facts_settings()

    def _import_legacy_facts_settings(self) -> None:
        row = self._conn.execute(
            "SELECT guild_id FROM guild_facts_settings WHERE guild_id = ''"
        ).fetchone()
        if row is not None:
            return
        enabled = 1 if self.get_setting("facts_enabled") == "1" else 0
        channel_id = self.get_setting("facts_channel_id") or ""
        min_minutes = int(self.get_setting("facts_min_minutes") or "60")
        max_minutes = int(self.get_setting("facts_max_minutes") or "180")
        attach = 0 if self.get_setting("facts_attach_library") == "0" else 1
        last_error = self.get_setting("facts_last_error") or ""
        raw_next = self.get_setting("facts_next_at")
        self._conn.execute(
            """
            INSERT INTO guild_facts_settings (
                guild_id, enabled, channel_id, min_minutes, max_minutes,
                attach_library, facts_next_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("", enabled, channel_id, min_minutes, max_minutes, attach, raw_next, last_error),
        )
        self._conn.commit()

    def _ensure_guild_facts_row(self, guild_id: str) -> None:
        row = self._conn.execute(
            "SELECT guild_id FROM guild_facts_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is not None:
            return
        legacy = self._conn.execute(
            "SELECT enabled, channel_id, min_minutes, max_minutes, attach_library, facts_next_at, last_error "
            "FROM guild_facts_settings WHERE guild_id = ''"
        ).fetchone()
        if legacy is not None and guild_id != "":
            self._conn.execute(
                """
                INSERT INTO guild_facts_settings (
                    guild_id, enabled, channel_id, min_minutes, max_minutes,
                    attach_library, facts_next_at, last_error
                ) VALUES (?, 0, '', ?, ?, ?, NULL, '')
                """,
                (guild_id, int(legacy[2]), int(legacy[3]), int(legacy[4])),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO guild_facts_settings (
                    guild_id, enabled, channel_id, min_minutes, max_minutes,
                    attach_library, facts_next_at, last_error
                ) VALUES (?, 0, '', 60, 180, 1, NULL, '')
                """,
                (guild_id,),
            )
        self._conn.commit()

    def _ensure_fact_columns(self) -> None:
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()
        }
        if "image_id" not in existing:
            self._conn.execute(
                "ALTER TABLE facts ADD COLUMN image_id INTEGER REFERENCES images(id) ON DELETE SET NULL"
            )
            self._conn.commit()

    def _ensure_event_columns(self) -> None:
        columns = {
            "recurrence": "TEXT NOT NULL DEFAULT 'none'",
            "daily_before_days": "INTEGER NOT NULL DEFAULT 0",
            "daily_before_message": "TEXT NOT NULL DEFAULT ''",
            "daily_after_days": "INTEGER NOT NULL DEFAULT 0",
            "daily_after_message": "TEXT NOT NULL DEFAULT ''",
            "daily_last_posted_on": "TEXT",
            "after_anchor_on": "TEXT",
        }
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        for name, ddl in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
        self._conn.commit()

    def image_file(self, stored_name: str) -> Path:
        if "/" in stored_name or "\\" in stored_name or stored_name.startswith("."):
            raise ValueError("Bad image name")
        path = (self.upload_dir / stored_name).resolve()
        if path.parent != self.upload_dir.resolve():
            raise ValueError("Bad image name")
        return path

    def add_image(self, name: str, stored_name: str) -> int:
        cleaned = clean_name(name)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO images (name, stored_name, created_at) VALUES (?, ?, ?)",
                    (cleaned, stored_name, _stamp(_utcnow())),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("That name is already used.") from exc
            image_id = int(cursor.lastrowid)
            self._library_deck_add_locked(image_id)
            return image_id

    def list_images(self) -> list[ImageRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM images ORDER BY name COLLATE NOCASE").fetchall()
        return [_image(row) for row in rows]

    def get_image(self, image_id: int) -> ImageRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        return _image(row) if row else None

    def get_image_by_name(self, name: str) -> ImageRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM images WHERE name = ? COLLATE NOCASE",
                (name.strip(),),
            ).fetchone()
        return _image(row) if row else None

    def delete_image(self, image_id: int) -> ImageRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            self._conn.commit()
            self._library_deck_remove_locked(image_id)
        return _image(row)

    def add_event(
        self,
        *,
        name: str,
        guild_id: str,
        starts_at: datetime,
        channel_id: str,
        message: str,
        image_id: int | None,
        pfp_image_id: int | None,
        countdown: bool,
        allow_pings: bool,
        recurrence: str,
        daily_before_days: int,
        daily_before_message: str,
        daily_after_days: int,
        daily_after_message: str,
    ) -> int:
        self._ensure_image(image_id)
        self._ensure_image(pfp_image_id)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events (
                    name, guild_id, starts_at, channel_id, message, image_id, pfp_image_id,
                    countdown, allow_pings, recurrence,
                    daily_before_days, daily_before_message,
                    daily_after_days, daily_after_message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    guild_id,
                    _stamp(starts_at),
                    channel_id,
                    message,
                    image_id,
                    pfp_image_id,
                    int(countdown),
                    int(allow_pings),
                    recurrence,
                    daily_before_days,
                    daily_before_message,
                    daily_after_days,
                    daily_after_message,
                    _stamp(_utcnow()),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_event(
        self,
        event_id: int,
        *,
        name: str,
        guild_id: str,
        starts_at: datetime,
        channel_id: str,
        message: str,
        image_id: int | None,
        pfp_image_id: int | None,
        countdown: bool,
        allow_pings: bool,
        recurrence: str,
        daily_before_days: int,
        daily_before_message: str,
        daily_after_days: int,
        daily_after_message: str,
        clear_countdown_message: bool,
    ) -> None:
        self._ensure_image(image_id)
        self._ensure_image(pfp_image_id)
        reset = starts_at > _utcnow()
        with self._lock:
            existing = self._conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
            if existing is None:
                raise ValueError("That event no longer exists.")
            self._conn.execute(
                """
                UPDATE events SET
                    name = ?, guild_id = ?, starts_at = ?, channel_id = ?, message = ?,
                    image_id = ?, pfp_image_id = ?, countdown = ?, allow_pings = ?,
                    recurrence = ?, daily_before_days = ?, daily_before_message = ?,
                    daily_after_days = ?, daily_after_message = ?,
                    countdown_message_id = CASE WHEN ? THEN NULL ELSE countdown_message_id END,
                    fired = CASE WHEN ? THEN 0 ELSE fired END,
                    missed = CASE WHEN ? THEN 0 ELSE missed END,
                    attempts = CASE WHEN ? THEN 0 ELSE attempts END,
                    last_error = CASE WHEN ? THEN '' ELSE last_error END,
                    daily_last_posted_on = CASE WHEN ? THEN NULL ELSE daily_last_posted_on END,
                    after_anchor_on = CASE WHEN ? THEN NULL ELSE after_anchor_on END
                WHERE id = ?
                """,
                (
                    name,
                    guild_id,
                    _stamp(starts_at),
                    channel_id,
                    message,
                    image_id,
                    pfp_image_id,
                    int(countdown),
                    int(allow_pings),
                    recurrence,
                    daily_before_days,
                    daily_before_message,
                    daily_after_days,
                    daily_after_message,
                    int(clear_countdown_message),
                    int(reset),
                    int(reset),
                    int(reset),
                    int(reset),
                    int(reset),
                    int(reset),
                    event_id,
                ),
            )
            self._conn.commit()

    def list_events(
        self,
        *,
        guild_id: str | None = None,
        legacy_channel_ids: set[str] | None = None,
    ) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(_EVENT_SQL + " ORDER BY e.starts_at").fetchall()
        events = [_event(row) for row in rows]
        if not guild_id:
            return events
        allowed = legacy_channel_ids or set()
        return [
            event
            for event in events
            if event.guild_id == guild_id
            or (event.guild_id == "" and event.channel_id in allowed)
        ]

    def get_event(self, event_id: int) -> EventRow | None:
        with self._lock:
            row = self._conn.execute(_EVENT_SQL + " WHERE e.id = ?", (event_id,)).fetchone()
        return _event(row) if row else None

    def mark_fired(self, event_id: int, last_error: str = "") -> None:
        event = self.get_event(event_id)
        if event is None:
            return
        anchor = event.starts_at.astimezone(self.timezone()).date().isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE events SET fired = 1, last_error = ?, after_anchor_on = ?,
                daily_last_posted_on = NULL
                WHERE id = ?
                """,
                (last_error, anchor, event_id),
            )
            self._conn.commit()

    def complete_event_delivery(self, event_id: int, last_error: str = "") -> None:
        event = self.get_event(event_id)
        if event is None:
            return
        anchor = event.starts_at.astimezone(self.timezone()).date().isoformat()
        if event.recurrence == "none":
            self.mark_fired(event_id, last_error)
            return
        nxt = next_occurrence(event.starts_at, event.recurrence)
        with self._lock:
            self._conn.execute(
                """
                UPDATE events SET
                    after_anchor_on = ?,
                    starts_at = ?,
                    fired = 0,
                    missed = 0,
                    attempts = 0,
                    countdown_message_id = NULL,
                    daily_last_posted_on = NULL,
                    last_error = ?
                WHERE id = ?
                """,
                (anchor, _stamp(nxt), last_error, event_id),
            )
            self._conn.commit()

    def set_daily_last_posted(self, event_id: int, day: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET daily_last_posted_on = ? WHERE id = ?",
                (day, event_id),
            )
            self._conn.commit()

    def clear_after_anchor_if_done(self, event_id: int, today: str) -> None:
        from datetime import date as date_cls

        event = self.get_event(event_id)
        if event is None or not event.after_anchor_on:
            return
        try:
            anchor = date_cls.fromisoformat(event.after_anchor_on)
            today_date = date_cls.fromisoformat(today)
        except ValueError:
            return
        days_ago = (today_date - anchor).days
        if event.daily_after_days > 0 and days_ago > event.daily_after_days:
            with self._lock:
                self._conn.execute(
                    "UPDATE events SET after_anchor_on = NULL WHERE id = ?",
                    (event_id,),
                )
                self._conn.commit()

    def record_failure(self, event_id: int, error: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            missed = 1 if attempts >= 5 else 0
            self._conn.execute(
                "UPDATE events SET attempts = ?, missed = ?, last_error = ? WHERE id = ?",
                (attempts, missed, error, event_id),
            )
            self._conn.commit()

    def mark_missed(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET missed = 1, last_error = ? WHERE id = ? AND fired = 0",
                ("The bot was offline for more than 12 hours after this event.", event_id),
            )
            self._conn.commit()

    def set_event_error(self, event_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET last_error = ? WHERE id = ?", (error, event_id))
            self._conn.commit()

    def set_countdown_message(self, event_id: int, message_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET countdown_message_id = ? WHERE id = ?",
                (message_id, event_id),
            )
            self._conn.commit()

    def delete_event(self, event_id: int) -> EventRow | None:
        event = self.get_event(event_id)
        if event is None:
            return None
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self._conn.commit()
        return event

    def add_avatar_job(self, *, image_id: int, weekday: int, hour: int, minute: int) -> int:
        if weekday not in range(7) or hour not in range(24) or minute not in range(60):
            raise ValueError("Choose a day and a time.")
        self._ensure_image(image_id)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO avatar_jobs (image_id, weekday, hour, minute, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (image_id, weekday, hour, minute, _stamp(_utcnow())),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"{WEEKDAYS[weekday]} already has a profile photo change.") from exc
            return int(cursor.lastrowid)

    def list_avatar_jobs(self) -> list[AvatarJob]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT j.id, j.image_id, j.weekday, j.hour, j.minute, j.last_run_on,
                       j.last_error, j.created_at, i.name AS image_name
                FROM avatar_jobs j
                JOIN images i ON i.id = j.image_id
                ORDER BY j.weekday, j.hour, j.minute
                """
            ).fetchall()
        return [_job(row) for row in rows]

    def mark_avatar_ran(self, job_id: int, day: str, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE avatar_jobs SET last_run_on = ?, last_error = ? WHERE id = ?",
                (day, error, job_id),
            )
            self._conn.commit()

    def delete_avatar_job(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM avatar_jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def timezone(self):
        name = self.get_setting("timezone") or get_settings().default_timezone or "UTC"
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def timezone_name(self) -> str:
        zone = self.timezone()
        return getattr(zone, "key", None) or "UTC"

    def _ensure_image(self, image_id: int | None) -> None:
        if image_id is None:
            return
        if self.get_image(image_id) is None:
            raise ValueError("That picture is not in the library.")

    def ensure_pool(self, slug: str, label: str) -> int:
        existing = self.get_pool_by_slug(slug)
        if existing is not None:
            return existing.id
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO image_pools (slug, label, created_at) VALUES (?, ?, ?)",
                (slug, label, _stamp(_utcnow())),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_pool_by_slug(self, slug: str) -> ImagePool | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM image_pools WHERE slug = ? COLLATE NOCASE",
                (slug.strip(),),
            ).fetchone()
        return _pool(row) if row else None

    def get_pool(self, pool_id: int) -> ImagePool | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM image_pools WHERE id = ?", (pool_id,)).fetchone()
        return _pool(row) if row else None

    def list_pools(self, *, include_prompt: bool = True) -> list[ImagePool]:
        with self._lock:
            if include_prompt:
                rows = self._conn.execute("SELECT * FROM image_pools ORDER BY slug COLLATE NOCASE").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM image_pools WHERE slug <> ? COLLATE NOCASE ORDER BY slug COLLATE NOCASE",
                    (PROMPT_POOL_SLUG,),
                ).fetchall()
        return [_pool(row) for row in rows]

    def create_pool(self, slug: str, label: str) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO image_pools (slug, label, created_at) VALUES (?, ?, ?)",
                    (slug, label, _stamp(_utcnow())),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("That pool id is already used.") from exc
            return int(cursor.lastrowid)

    def delete_pool(self, pool_id: int) -> None:
        pool = self.get_pool(pool_id)
        if pool is None:
            raise ValueError("That pool is gone.")
        if pool.slug.lower() == PROMPT_POOL_SLUG:
            raise ValueError("The prompt pool cannot be deleted.")
        with self._lock:
            self._conn.execute("DELETE FROM image_pools WHERE id = ?", (pool_id,))
            self._conn.commit()

    def list_pool_images(self, pool_id: int) -> list[ImageRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT i.id, i.name, i.stored_name, i.created_at
                FROM pool_images p
                JOIN images i ON i.id = p.image_id
                WHERE p.pool_id = ?
                ORDER BY i.name COLLATE NOCASE
                """,
                (pool_id,),
            ).fetchall()
        return [_image(row) for row in rows]

    def add_pool_image(self, pool_id: int, image_id: int) -> None:
        self._ensure_image(image_id)
        if self.get_pool(pool_id) is None:
            raise ValueError("That pool is gone.")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO pool_images (pool_id, image_id) VALUES (?, ?)",
                (pool_id, image_id),
            )
            self._conn.commit()

    def remove_pool_image(self, pool_id: int, image_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pool_images WHERE pool_id = ? AND image_id = ?",
                (pool_id, image_id),
            )
            self._conn.commit()

    def pick_random_pool_image(self, pool_id: int) -> ImageRow | None:
        images = self.list_pool_images(pool_id)
        if not images:
            return None
        return random.choice(images)

    def list_facts(self, guild_id: str) -> list[FactRow]:
        with self._lock:
            rows = self._conn.execute(
                _FACT_SQL + " WHERE f.guild_id = ? ORDER BY f.id",
                (guild_id,),
            ).fetchall()
        facts = [_fact(row) for row in rows]
        if facts or not guild_id:
            return facts
        rows = self._conn.execute(
            _FACT_SQL + " WHERE f.guild_id = '' ORDER BY f.id"
        ).fetchall()
        return [_fact(row) for row in rows]

    def get_fact(self, fact_id: int, *, guild_id: str | None = None) -> FactRow | None:
        with self._lock:
            if guild_id is None:
                row = self._conn.execute(_FACT_SQL + " WHERE f.id = ?", (fact_id,)).fetchone()
            else:
                row = self._conn.execute(
                    _FACT_SQL + " WHERE f.id = ? AND f.guild_id = ?",
                    (fact_id, guild_id),
                ).fetchone()
        return _fact(row) if row else None

    def add_fact(self, guild_id: str, body: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO facts (guild_id, body, created_at) VALUES (?, ?, ?)",
                (guild_id, body, _stamp(_utcnow())),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_fact(self, fact_id: int, *, guild_id: str, body: str) -> None:
        if self.get_fact(fact_id, guild_id=guild_id) is None:
            raise ValueError("That fact no longer exists.")
        with self._lock:
            self._conn.execute(
                "UPDATE facts SET body = ? WHERE id = ? AND guild_id = ?",
                (body, fact_id, guild_id),
            )
            self._conn.commit()

    def delete_fact(self, fact_id: int, *, guild_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM facts WHERE id = ? AND guild_id = ?",
                (fact_id, guild_id),
            )
            self._conn.commit()

    def pick_random_fact(self, guild_id: str) -> FactRow | None:
        facts = self.list_facts(guild_id)
        scoped = [fact for fact in facts if fact.guild_id == guild_id or fact.guild_id == ""]
        if not scoped:
            return None
        return random.choice(scoped)

    def get_facts_config(self, guild_id: str) -> FactsConfig:
        self._ensure_guild_facts_row(guild_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT enabled, channel_id, min_minutes, max_minutes, attach_library,
                       facts_next_at, last_error
                FROM guild_facts_settings WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        assert row is not None
        raw_next = row[5]
        next_at = None if not raw_next else _parse(str(raw_next))
        return FactsConfig(
            enabled=bool(row[0]),
            channel_id=str(row[1] or ""),
            min_minutes=int(row[2]),
            max_minutes=int(row[3]),
            attach_library_image=bool(row[4]),
            next_at=next_at,
            last_error=str(row[6] or ""),
        )

    def save_facts_config(
        self,
        guild_id: str,
        *,
        enabled: bool,
        channel_id: str,
        min_minutes: int,
        max_minutes: int,
        attach_library_image: bool,
        reschedule: bool,
    ) -> None:
        self._ensure_guild_facts_row(guild_id)
        with self._lock:
            self._conn.execute(
                """
                UPDATE guild_facts_settings SET
                    enabled = ?, channel_id = ?, min_minutes = ?, max_minutes = ?,
                    attach_library = ?
                WHERE guild_id = ?
                """,
                (
                    int(enabled),
                    channel_id,
                    min_minutes,
                    max_minutes,
                    int(attach_library_image),
                    guild_id,
                ),
            )
            self._conn.commit()
        if enabled and reschedule:
            self.reschedule_facts(guild_id, _utcnow())
        elif not enabled:
            self.set_facts_error(guild_id, "")
            with self._lock:
                self._conn.execute(
                    "UPDATE guild_facts_settings SET facts_next_at = NULL WHERE guild_id = ?",
                    (guild_id,),
                )
                self._conn.commit()

    def list_facts_guilds(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT guild_id FROM guild_facts_settings WHERE enabled = 1"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def reschedule_facts(self, guild_id: str, now: datetime) -> datetime:
        config = self.get_facts_config(guild_id)
        delay = random_minutes_between(config.min_minutes, config.max_minutes)
        nxt = now + timedelta(minutes=delay)
        with self._lock:
            self._conn.execute(
                "UPDATE guild_facts_settings SET facts_next_at = ? WHERE guild_id = ?",
                (_stamp(nxt), guild_id),
            )
            self._conn.commit()
        return nxt

    def set_facts_error(self, guild_id: str, error: str) -> None:
        self._ensure_guild_facts_row(guild_id)
        with self._lock:
            self._conn.execute(
                "UPDATE guild_facts_settings SET last_error = ? WHERE guild_id = ?",
                (error, guild_id),
            )
            self._conn.commit()

    def _ensure_guild_scheduled_pictures_row(self, guild_id: str) -> None:
        row = self._conn.execute(
            "SELECT guild_id FROM guild_scheduled_pictures_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is not None:
            return
        legacy = self._conn.execute(
            """
            SELECT min_minutes, max_minutes FROM guild_facts_settings WHERE guild_id = ?
            """,
            (guild_id if guild_id else "",),
        ).fetchone()
        if legacy is not None and guild_id != "":
            self._conn.execute(
                """
                INSERT INTO guild_scheduled_pictures_settings (
                    guild_id, enabled, channel_id, min_minutes, max_minutes, next_at, last_error
                ) VALUES (?, 0, '', ?, ?, NULL, '')
                """,
                (guild_id, int(legacy[0]), int(legacy[1])),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO guild_scheduled_pictures_settings (
                    guild_id, enabled, channel_id, min_minutes, max_minutes, next_at, last_error
                ) VALUES (?, 0, '', 60, 180, NULL, '')
                """,
                (guild_id,),
            )
        self._conn.commit()

    def get_scheduled_pictures_config(self, guild_id: str) -> ScheduledPicturesConfig:
        self._ensure_guild_scheduled_pictures_row(guild_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT enabled, channel_id, min_minutes, max_minutes, next_at, last_error
                FROM guild_scheduled_pictures_settings WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        assert row is not None
        raw_next = row[4]
        next_at = None if not raw_next else _parse(str(raw_next))
        return ScheduledPicturesConfig(
            enabled=bool(row[0]),
            channel_id=str(row[1] or ""),
            min_minutes=int(row[2]),
            max_minutes=int(row[3]),
            next_at=next_at,
            last_error=str(row[5] or ""),
        )

    def save_scheduled_pictures_config(
        self,
        guild_id: str,
        *,
        enabled: bool,
        channel_id: str,
        min_minutes: int,
        max_minutes: int,
        reschedule: bool,
    ) -> None:
        self._ensure_guild_scheduled_pictures_row(guild_id)
        with self._lock:
            self._conn.execute(
                """
                UPDATE guild_scheduled_pictures_settings SET
                    enabled = ?, channel_id = ?, min_minutes = ?, max_minutes = ?
                WHERE guild_id = ?
                """,
                (int(enabled), channel_id, min_minutes, max_minutes, guild_id),
            )
            self._conn.commit()
        if enabled and reschedule:
            self.reschedule_scheduled_pictures(guild_id, _utcnow())
        elif not enabled:
            self.set_scheduled_pictures_error(guild_id, "")
            with self._lock:
                self._conn.execute(
                    "UPDATE guild_scheduled_pictures_settings SET next_at = NULL WHERE guild_id = ?",
                    (guild_id,),
                )
                self._conn.commit()

    def list_scheduled_pictures_guilds(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT guild_id FROM guild_scheduled_pictures_settings WHERE enabled = 1"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def reschedule_scheduled_pictures(self, guild_id: str, now: datetime) -> datetime:
        config = self.get_scheduled_pictures_config(guild_id)
        delay = random_minutes_between(config.min_minutes, config.max_minutes)
        nxt = now + timedelta(minutes=delay)
        with self._lock:
            self._conn.execute(
                "UPDATE guild_scheduled_pictures_settings SET next_at = ? WHERE guild_id = ?",
                (_stamp(nxt), guild_id),
            )
            self._conn.commit()
        return nxt

    def set_scheduled_pictures_error(self, guild_id: str, error: str) -> None:
        self._ensure_guild_scheduled_pictures_row(guild_id)
        with self._lock:
            self._conn.execute(
                "UPDATE guild_scheduled_pictures_settings SET last_error = ? WHERE guild_id = ?",
                (error, guild_id),
            )
            self._conn.commit()

    def list_emoji_triggers(self, guild_id: str) -> list[EmojiRuleRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, emoji, guild_id, created_at FROM emoji_triggers
                WHERE guild_id = ? ORDER BY emoji COLLATE NOCASE
                """,
                (guild_id,),
            ).fetchall()
        return [
            EmojiRuleRow(
                id=int(row["id"]),
                emoji=str(row["emoji"]),
                pool_id=0,
                pool_slug="library",
                pool_label="Library",
                created_at=_parse(str(row["created_at"])),
            )
            for row in rows
        ]

    def add_emoji_trigger(self, guild_id: str, emoji: str) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO emoji_triggers (emoji, guild_id, created_at) VALUES (?, ?, ?)",
                    (emoji, guild_id, _stamp(_utcnow())),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("That emoji is already listed for this server.") from exc
            return int(cursor.lastrowid)

    def delete_emoji_trigger(self, rule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM emoji_triggers WHERE id = ?", (rule_id,))
            self._conn.commit()

    def picture_command_name(self) -> str:
        raw = (self.get_setting("cmd_picture") or "picture").strip().lower()
        return raw or "picture"

    def set_picture_command_name(self, name: str) -> None:
        self.set_setting("cmd_picture", name)

    def pick_library_image(self) -> ImageRow:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM images ORDER BY name COLLATE NOCASE").fetchall()
            images = [_image(row) for row in rows]
            if not images:
                raise ValueError("No pictures have been uploaded yet.")
            valid = {image.id for image in images}
            deck = self._library_deck_load_locked(valid)
            if not deck:
                deck = list(valid)
                random.shuffle(deck)
            chosen_id = deck.pop()
            self._library_deck_save_locked(deck)
            for image in images:
                if image.id == chosen_id:
                    return image
            fallback = images[0]
            self._library_deck_save_locked([])
            return fallback

    def _library_deck_key(self) -> str:
        return "library_deck"

    def _library_deck_load_locked(self, valid: set[int]) -> list[int]:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (self._library_deck_key(),),
        ).fetchone()
        raw = row[0] if row else None
        if not raw:
            return []
        deck: list[int] = []
        seen: set[int] = set()
        for part in raw.split(","):
            piece = part.strip()
            if not piece.isdigit():
                continue
            image_id = int(piece)
            if image_id in valid and image_id not in seen:
                deck.append(image_id)
                seen.add(image_id)
        return deck

    def _library_deck_save_locked(self, deck: list[int]) -> None:
        key = self._library_deck_key()
        if deck:
            self._conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, ",".join(str(image_id) for image_id in deck)),
            )
        else:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        self._conn.commit()

    def _library_deck_add_locked(self, image_id: int) -> None:
        rows = self._conn.execute("SELECT id FROM images").fetchall()
        valid = {int(row[0]) for row in rows}
        deck = self._library_deck_load_locked(valid)
        if not deck or image_id in deck:
            return
        deck.append(image_id)
        self._library_deck_save_locked(deck)

    def _library_deck_remove_locked(self, image_id: int) -> None:
        rows = self._conn.execute("SELECT id FROM images").fetchall()
        valid = {int(row[0]) for row in rows}
        deck = self._library_deck_load_locked(valid)
        if image_id not in deck:
            return
        deck = [item for item in deck if item != image_id]
        self._library_deck_save_locked(deck)

    def add_activity(
        self,
        *,
        kind: str,
        summary: str,
        detail: str = "",
        guild: str = "",
        channel: str = "",
        ok: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity_log (kind, summary, detail, guild, channel, ok, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, summary, detail, guild, channel, int(ok), _stamp(_utcnow())),
            )
            total = int(self._conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0])
            if total > 500:
                drop = total - 500
                self._conn.execute(
                    """
                    DELETE FROM activity_log WHERE id IN (
                        SELECT id FROM activity_log ORDER BY id ASC LIMIT ?
                    )
                    """,
                    (drop,),
                )
            self._conn.commit()

    def list_activity(self, limit: int = 200) -> list[ActivityRow]:
        limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_activity(row) for row in rows]


_EVENT_SQL = """
SELECT
    e.id, e.name, e.guild_id, e.starts_at, e.channel_id, e.message,
    e.image_id, e.pfp_image_id, e.countdown, e.countdown_message_id,
    e.allow_pings, e.fired, e.missed, e.attempts, e.last_error, e.created_at,
    e.recurrence, e.daily_before_days, e.daily_before_message,
    e.daily_after_days, e.daily_after_message, e.daily_last_posted_on, e.after_anchor_on,
    pi.name AS image_name,
    pf.name AS pfp_name
FROM events e
LEFT JOIN images pi ON pi.id = e.image_id
LEFT JOIN images pf ON pf.id = e.pfp_image_id
"""


def _image(row: sqlite3.Row) -> ImageRow:
    return ImageRow(
        id=int(row["id"]),
        name=str(row["name"]),
        stored_name=str(row["stored_name"]),
        created_at=_parse(str(row["created_at"])),
    )


def _event(row: sqlite3.Row) -> EventRow:
    keys = row.keys()
    guild_id = ""
    if "guild_id" in keys and row["guild_id"] is not None:
        guild_id = str(row["guild_id"])
    return EventRow(
        id=int(row["id"]),
        name=str(row["name"]),
        guild_id=guild_id,
        starts_at=_parse(str(row["starts_at"])),
        channel_id=str(row["channel_id"]),
        message=str(row["message"]),
        image_id=None if row["image_id"] is None else int(row["image_id"]),
        pfp_image_id=None if row["pfp_image_id"] is None else int(row["pfp_image_id"]),
        countdown=bool(row["countdown"]),
        countdown_message_id=None if row["countdown_message_id"] is None else str(row["countdown_message_id"]),
        allow_pings=bool(row["allow_pings"]),
        fired=bool(row["fired"]),
        missed=bool(row["missed"]),
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"] or ""),
        created_at=_parse(str(row["created_at"])),
        recurrence=str(row["recurrence"] if row["recurrence"] is not None else "none"),
        daily_before_days=int(row["daily_before_days"] or 0),
        daily_before_message=str(row["daily_before_message"] or ""),
        daily_after_days=int(row["daily_after_days"] or 0),
        daily_after_message=str(row["daily_after_message"] or ""),
        daily_last_posted_on=None if row["daily_last_posted_on"] is None else str(row["daily_last_posted_on"]),
        after_anchor_on=None if row["after_anchor_on"] is None else str(row["after_anchor_on"]),
        image_name=None if row["image_name"] is None else str(row["image_name"]),
        pfp_name=None if row["pfp_name"] is None else str(row["pfp_name"]),
    )


def _pool(row: sqlite3.Row) -> ImagePool:
    return ImagePool(
        id=int(row["id"]),
        slug=str(row["slug"]),
        label=str(row["label"]),
        created_at=_parse(str(row["created_at"])),
    )


_FACT_SQL = """
SELECT f.id, f.guild_id, f.body, f.created_at
FROM facts f
"""


def _fact(row: sqlite3.Row) -> FactRow:
    keys = row.keys()
    guild_id = ""
    if "guild_id" in keys and row["guild_id"] is not None:
        guild_id = str(row["guild_id"])
    return FactRow(
        id=int(row["id"]),
        guild_id=guild_id,
        body=str(row["body"]),
        created_at=_parse(str(row["created_at"])),
    )


def _activity(row: sqlite3.Row) -> ActivityRow:
    return ActivityRow(
        id=int(row["id"]),
        kind=str(row["kind"]),
        summary=str(row["summary"]),
        detail=str(row["detail"] or ""),
        guild=str(row["guild"] or ""),
        channel=str(row["channel"] or ""),
        ok=bool(row["ok"]),
        created_at=_parse(str(row["created_at"])),
    )


def _emoji_rule(row: sqlite3.Row) -> EmojiRuleRow:
    return EmojiRuleRow(
        id=int(row["id"]),
        emoji=str(row["emoji"]),
        pool_id=int(row["pool_id"]),
        pool_slug=str(row["slug"]),
        pool_label=str(row["label"]),
        created_at=_parse(str(row["created_at"])),
    )


def _job(row: sqlite3.Row) -> AvatarJob:
    return AvatarJob(
        id=int(row["id"]),
        image_id=int(row["image_id"]),
        weekday=int(row["weekday"]),
        hour=int(row["hour"]),
        minute=int(row["minute"]),
        last_run_on=None if row["last_run_on"] is None else str(row["last_run_on"]),
        last_error=str(row["last_error"] or ""),
        created_at=_parse(str(row["created_at"])),
        image_name=str(row["image_name"]),
    )


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _db = Database(settings.data_dir / "bot.db")
    return _db
