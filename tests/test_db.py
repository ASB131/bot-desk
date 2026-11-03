import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import Database


UTC = timezone.utc


class DatabaseTests(unittest.TestCase):
    def test_library_deck_cycles_without_repeat(self):
        with TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "deck.db")
            try:
                ids = [
                    database.add_image("A", "a.png"),
                    database.add_image("B", "b.png"),
                    database.add_image("C", "c.png"),
                ]
                for _ in range(2):
                    seen = {database.pick_library_image().id for _ in range(3)}
                    self.assertEqual(seen, set(ids))
            finally:
                database.close()

    def test_images_events_and_weekly_photo(self):
        with TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "bot.db")
            try:
                self._exercise(database)
            finally:
                database.close()

    def _exercise(self, database: Database) -> None:
        image_id = database.add_image("Party", "party.png")
        with self.assertRaises(ValueError):
            database.add_image("party", "other.png")

        starts = datetime.now(UTC) - timedelta(hours=1)
        event_id = database.add_event(
            name="Summer fair",
            guild_id="999999999999999999",
            starts_at=starts,
            channel_id="12345678901234567",
            message="Hello {name}",
            image_id=image_id,
            pfp_image_id=None,
            countdown=True,
            allow_pings=False,
            recurrence="none",
            daily_before_days=0,
            daily_before_message="",
            daily_after_days=0,
            daily_after_message="",
        )
        event = database.get_event(event_id)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.image_name, "Party")

        database.mark_fired(event_id, "")
        database.update_event(
            event_id,
            name="Summer fair",
            guild_id="999999999999999999",
            starts_at=datetime.now(UTC) + timedelta(days=2),
            channel_id="12345678901234567",
            message="Hello again",
            image_id=image_id,
            pfp_image_id=None,
            countdown=True,
            allow_pings=False,
            recurrence="none",
            daily_before_days=0,
            daily_before_message="",
            daily_after_days=0,
            daily_after_message="",
            clear_countdown_message=False,
        )
        refreshed = database.get_event(event_id)
        assert refreshed is not None
        self.assertFalse(refreshed.fired)
        self.assertEqual(refreshed.message, "Hello again")

        for _ in range(5):
            database.record_failure(event_id, "missing permission")
        failed = database.get_event(event_id)
        assert failed is not None
        self.assertTrue(failed.missed)
        self.assertEqual(failed.attempts, 5)

        database.add_avatar_job(image_id=image_id, weekday=6, hour=9, minute=0)
        with self.assertRaises(ValueError):
            database.add_avatar_job(image_id=image_id, weekday=6, hour=18, minute=0)

        database.delete_image(image_id)
        orphan = database.get_event(event_id)
        assert orphan is not None
        self.assertIsNone(orphan.image_id)
        self.assertEqual(database.list_avatar_jobs(), [])

    def test_scheduled_pictures_reschedule(self):
        with TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "sched.db")
            try:
                guild = "111111111111111111"
                database.save_scheduled_pictures_config(
                    guild,
                    enabled=True,
                    channel_id="12345678901234567",
                    min_minutes=10,
                    max_minutes=10,
                    reschedule=True,
                )
                config = database.get_scheduled_pictures_config(guild)
                self.assertTrue(config.enabled)
                self.assertIsNotNone(config.next_at)
                self.assertEqual(config.min_minutes, 10)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
