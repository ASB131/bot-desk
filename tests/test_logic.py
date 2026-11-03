import unittest
from datetime import datetime, timedelta, timezone

from app.logic import (
    avatar_is_due,
    classify_event,
    clean_name,
    humanize_delta,
    next_avatar_at,
    parse_local_datetime,
    render_message,
)


UTC = timezone.utc


class LogicTests(unittest.TestCase):
    def test_humanize(self):
        self.assertEqual(humanize_delta(timedelta(0)), "now")
        self.assertEqual(humanize_delta(timedelta(seconds=-5)), "now")
        self.assertEqual(humanize_delta(timedelta(seconds=30)), "under a minute")
        self.assertEqual(humanize_delta(timedelta(seconds=30), with_seconds=True), "30s")
        self.assertEqual(humanize_delta(timedelta(minutes=90)), "1h 30m")
        self.assertEqual(humanize_delta(timedelta(days=2, hours=4, minutes=10)), "2d 4h 10m")

    def test_render_and_parse(self):
        plus_one = timezone(timedelta(hours=1))
        parsed = parse_local_datetime("2026-06-01T18:30", plus_one)
        self.assertEqual(parsed, datetime(2026, 6, 1, 17, 30, tzinfo=UTC))
        text = render_message("Hi {name} at {time}, {countdown} left", name="Fair", when_label="Monday", countdown_label="1d")
        self.assertEqual(text, "Hi Fair at Monday, 1d left")
        with self.assertRaises(ValueError):
            clean_name("  ")
        self.assertEqual(clean_name("  Party night "), "Party night")

    def test_classify_event(self):
        now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        future = now + timedelta(days=1)
        recent = now - timedelta(hours=1)
        old = now - timedelta(hours=13)
        self.assertEqual(classify_event(future, now, False, False), "scheduled")
        self.assertEqual(classify_event(recent, now, False, False), "due")
        self.assertEqual(classify_event(old, now, False, False), "stale")
        self.assertEqual(classify_event(old, now, True, False), "sent")
        self.assertEqual(classify_event(recent, now, False, True), "missed")

    def test_weekly_avatar_rules(self):
        created = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        saturday = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.assertFalse(avatar_is_due(6, 9, 0, None, created, saturday))
        self.assertEqual(
            next_avatar_at(6, 9, 0, None, created, saturday),
            datetime(2026, 11, 8, 9, 0, tzinfo=UTC),
        )

        sunday_morning = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
        self.assertFalse(avatar_is_due(6, 9, 0, None, created, sunday_morning))
        self.assertEqual(
            next_avatar_at(6, 9, 0, None, created, sunday_morning),
            datetime(2026, 11, 8, 9, 0, tzinfo=UTC),
        )

        sunday_after = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
        self.assertTrue(avatar_is_due(6, 9, 0, None, created, sunday_after))
        self.assertEqual(next_avatar_at(6, 9, 0, None, created, sunday_after), sunday_after)

        made_after_slot = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        sunday_evening = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)
        self.assertFalse(avatar_is_due(6, 9, 0, None, made_after_slot, sunday_evening))
        self.assertEqual(
            next_avatar_at(6, 9, 0, None, made_after_slot, sunday_evening),
            datetime(2026, 11, 15, 9, 0, tzinfo=UTC),
        )

        self.assertFalse(avatar_is_due(6, 9, 0, "2026-11-08", created, sunday_after))
        self.assertEqual(
            next_avatar_at(6, 9, 0, "2026-11-08", created, sunday_after),
            datetime(2026, 11, 15, 9, 0, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
