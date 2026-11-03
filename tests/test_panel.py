import io
import os
import re
import shutil
import unittest
from pathlib import Path
from tempfile import mkdtemp

os.environ["DATA_DIR"] = mkdtemp()
os.environ["PANEL_PASSWORD"] = "secret-test"
os.environ["DISCORD_TOKEN"] = ""
os.environ["DEFAULT_TIMEZONE"] = "UTC"
os.environ["PORT"] = "23435"

from PIL import Image
from fastapi.testclient import TestClient

from app.bot_client import bot, ensure_commands
from app.web import create_app


def _call(client: TestClient, method: str, url: str, **kwargs):
    kwargs.setdefault("follow_redirects", False)
    try:
        return getattr(client, method)(url, **kwargs)
    except TypeError:
        follow = kwargs.pop("follow_redirects")
        kwargs["allow_redirects"] = follow
        return getattr(client, method)(url, **kwargs)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    if not match:
        raise AssertionError("csrf token missing")
    return match.group(1)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (220, 120, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app())

    @classmethod
    def tearDownClass(cls):
        from app.db import get_db

        get_db().close()
        shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)

    def test_commands_register(self):
        ensure_commands()
        names = {command.name for command in bot.tree.get_commands()}
        from app.db import get_db

        pic = get_db().picture_command_name()
        self.assertEqual(names, {pic, "countdown"})

    def test_health_and_login_wall(self):
        health = _call(self.client, "get", "/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        stranger = TestClient(create_app())
        blocked = _call(stranger, "get", "/events")
        self.assertEqual(blocked.status_code, 303)
        self.assertIn("/login", blocked.headers["location"])

    def test_schedule_upload_and_weekly_photo(self):
        login_page = _call(self.client, "get", "/login")
        self.assertIn("Bot desk", login_page.text)
        token = _csrf(login_page.text)
        wrong = _call(
            self.client,
            "post",
            "/login",
            data={"password": "nope", "csrf": token, "nxt": "/"},
        )
        self.assertIn("wrong", wrong.text)
        token = _csrf(wrong.text)
        signed_in = _call(
            self.client,
            "post",
            "/login",
            data={"password": "secret-test", "csrf": token, "nxt": "/"},
        )
        self.assertEqual(signed_in.status_code, 303)

        home = _call(self.client, "get", "/")
        self.assertIn("No upcoming events", home.text)
        token = _csrf(home.text)

        missing = _call(
            self.client,
            "post",
            "/events",
            data={
                "csrf": token,
                "name": "Broken",
                "starts_at": "2026-12-01T18:30",
                "message": "Hello",
            },
        )
        self.assertIn("Choose a channel", missing.text)

        created = _call(
            self.client,
            "post",
            "/events",
            data={
                "csrf": token,
                "name": "Summer fair",
                "starts_at": "2026-12-01T18:30",
                "channel_override": "12345678901234567",
                "message": "Hello {name}",
                "countdown": "1",
            },
        )
        self.assertEqual(created.status_code, 303)
        events = _call(self.client, "get", "/events")
        self.assertIn("Summer fair", events.text)
        self.assertIn("Hello {name}", events.text)

        uploaded = _call(
            self.client,
            "post",
            "/library",
            data={"csrf": token, "name": "party"},
            files={"file": ("party.png", _png(), "image/png")},
        )
        self.assertEqual(uploaded.status_code, 303)
        library = _call(self.client, "get", "/library")
        self.assertIn("party", library.text)
        self.assertIn("/media/1", library.text)
        picture = _call(self.client, "get", "/media/1")
        self.assertEqual(picture.status_code, 200)
        self.assertTrue(picture.content.startswith(b"\x89PNG"))

        rejected = _call(
            self.client,
            "post",
            "/library",
            data={"csrf": token, "name": "notes"},
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertIn("PNG", rejected.text)

        weekly = _call(
            self.client,
            "post",
            "/avatar",
            data={"csrf": token, "image_id": "1", "weekday": "6", "clock": "09:00"},
        )
        self.assertEqual(weekly.status_code, 303)
        again = _call(
            self.client,
            "post",
            "/avatar",
            data={"csrf": token, "image_id": "1", "weekday": "6", "clock": "18:00"},
        )
        self.assertIn("Sunday already has", again.text)

        saved = _call(
            self.client,
            "post",
            "/settings",
            data={"csrf": token, "timezone_name": "UTC"},
            follow_redirects=True,
        )
        self.assertIn("Times now use UTC", saved.text)
        bad_zone = _call(
            self.client,
            "post",
            "/settings",
            data={"csrf": token, "timezone_name": "Not/AZone"},
            follow_redirects=True,
        )
        self.assertIn("Unknown timezone", bad_zone.text)
        self.assertTrue(Path(os.environ["DATA_DIR"], "uploads").exists())


if __name__ == "__main__":
    unittest.main()
