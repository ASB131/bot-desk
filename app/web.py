import asyncio
import inspect
import io
import secrets
import uuid
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from app.bot_client import invite_url, refresh_channels
from app.config import get_settings
from app.db import EventRow, get_db
from app.activity import log_activity
from app.discord_ops import OpsError, apply_avatar, rename_bot, retire_countdown, send_preview
from app.logic import (
    COMMON_TIMEZONES,
    WEEKDAYS,
    avatar_is_due,
    clean_bot_username,
    clean_channel_id,
    clean_clock,
    clean_day_count,
    clean_emoji_trigger,
    clean_recurrence,
    clean_slash_command_name,
    clean_event_name,
    clean_fact_text,
    clean_interval_minutes,
    clean_message,
    clean_weekday,
    display_status,
    format_local,
    next_avatar_at,
    parse_local_datetime,
    recurrence_label,
    RECURRENCE_CHOICES,
    RECURRENCE_LABELS,
    to_local_input,
)
from app.state import ChannelChoice, state

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["weekday_name"] = lambda index: WEEKDAYS[index]
templates.env.globals["recurrence_labels"] = RECURRENCE_LABELS


class NeedLogin(Exception):
    def __init__(self, target: str) -> None:
        self.target = target


def create_app() -> FastAPI:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    get_db()
    if settings.application_id:
        state.application_id = settings.application_id
    app = FastAPI(title="Bot desk")
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="botdesk",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=False,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    @app.exception_handler(NeedLogin)
    async def redirect_login(request: Request, exc: NeedLogin):
        del request
        return RedirectResponse(f"/login?next={quote(exc.target)}", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        accepts = request.headers.get("accept", "")
        if "text/html" in accepts or exc.status_code == 403:
            safe = escape(detail)
            body = (
                "<!doctype html><meta charset='utf-8'>"
                f"<title>{safe}</title><body style='font-family:sans-serif;padding:2rem'>"
                f"<p>{safe}</p><p><a href='/'>Back to the panel</a></p></body>"
            )
            return HTMLResponse(body, status_code=exc.status_code)
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.get("/health")
    async def health():
        return {"ok": True, "bot": state.online}

    @app.get("/login")
    async def login_form(request: Request):
        if _logged_in(request):
            return RedirectResponse(_safe_next(request.query_params.get("next", "/")), status_code=303)
        return _render(
            request,
            "login.html",
            {
                "next": _safe_next(request.query_params.get("next", "/")),
                "password_set": bool(get_settings().panel_password),
                "csrf": _ensure_csrf(request),
            },
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(""), csrf: str = Form(""), nxt: str = Form("/")):
        _check_csrf(request, csrf)
        expected = get_settings().panel_password
        if not expected or not _same(password, expected):
            await asyncio.sleep(0.4)
            return _render(
                request,
                "login.html",
                {
                    "next": _safe_next(nxt),
                    "password_set": bool(expected),
                    "csrf": _ensure_csrf(request),
                    "flash": {"level": "error", "text": "That password is wrong."},
                },
            )
        request.session["user"] = True
        return RedirectResponse(_safe_next(nxt), status_code=303)

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("")):
        _check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.post("/server")
    async def choose_server(request: Request, csrf: str = Form(""), guild_id: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        from app.logic import clean_guild_id

        try:
            cleaned = clean_guild_id(guild_id)
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
        else:
            known = {guild.id for guild in state.guilds}
            if known and cleaned not in known:
                request.session["flash"] = {"level": "error", "text": "That server is not in the list."}
            else:
                request.session["guild_id"] = cleaned
                name = next((g.name for g in state.guilds if g.id == cleaned), cleaned)
                request.session["flash"] = {"level": "ok", "text": f"Editing settings for {name}."}
        return RedirectResponse(request.headers.get("referer", "/") or "/", status_code=303)

    @app.get("/")
    async def dashboard(request: Request):
        _require(request)
        database = get_db()
        zone = database.timezone()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        local_now = now.astimezone(zone)
        upcoming = []
        hero = None
        guild_id = _active_guild_id(request)
        channel_ids = _guild_channel_ids(guild_id)
        scoped_events = database.list_events(guild_id=guild_id, legacy_channel_ids=channel_ids)
        for event in scoped_events:
            if event.fired or event.missed:
                continue
            card = {
                "name": event.name,
                "when": format_local(event.starts_at, zone),
                "iso": event.starts_at.isoformat(),
                "status": display_status(event.starts_at, now, event.fired, event.missed, event.attempts),
            }
            upcoming.append(card)
            if hero is None or event.starts_at < hero["starts"]:
                hero = {**card, "starts": event.starts_at}
        jobs = []
        for job in database.list_avatar_jobs():
            due = avatar_is_due(job.weekday, job.hour, job.minute, job.last_run_on, job.created_at, local_now)
            when = next_avatar_at(job.weekday, job.hour, job.minute, job.last_run_on, job.created_at, local_now)
            jobs.append(
                {
                    "label": f"{WEEKDAYS[job.weekday]} {job.hour:02d}:{job.minute:02d}",
                    "image": job.image_name,
                    "due": due,
                    "when": "Due now" if due else format_local(when, zone),
                    "error": job.last_error,
                }
            )
        return _page(
            request,
            "dashboard.html",
            hero=hero,
            upcoming=upcoming[:5],
            jobs=jobs[:4],
            image_count=len(database.list_images()),
            event_count=len(scoped_events),
        )

    @app.get("/events")
    async def events_page(request: Request):
        _require(request)
        editing = None
        raw_id = request.query_params.get("edit", "")
        if raw_id.isdigit():
            editing = get_db().get_event(int(raw_id))
            if editing is None:
                request.session["flash"] = {"level": "error", "text": "That event no longer exists."}
        return _events(request, _form_from_event(editing) if editing else _blank_form(), editing.id if editing else None)

    @app.post("/events")
    async def save_event(
        request: Request,
        csrf: str = Form(""),
        event_id: str = Form(""),
        name: str = Form(""),
        starts_at: str = Form(""),
        channel_id: str = Form(""),
        channel_override: str = Form(""),
        message: str = Form(""),
        image_id: str = Form(""),
        pfp_image_id: str = Form(""),
        countdown: str = Form(""),
        allow_pings: str = Form(""),
        recurrence: str = Form("none"),
        daily_before_days: str = Form("0"),
        daily_before_message: str = Form("{days} days until {name}"),
        daily_after_days: str = Form("0"),
        daily_after_message: str = Form("{name} was {days} days ago"),
    ):
        _require(request)
        _check_csrf(request, csrf)
        submitted = {
            "event_id": event_id,
            "name": name,
            "starts_at": starts_at,
            "channel_id": channel_id,
            "channel_override": channel_override,
            "message": message,
            "image_id": image_id,
            "pfp_image_id": pfp_image_id,
            "countdown": countdown == "1",
            "allow_pings": allow_pings == "1",
            "recurrence": recurrence,
            "daily_before_days": daily_before_days,
            "daily_before_message": daily_before_message,
            "daily_after_days": daily_after_days,
            "daily_after_message": daily_after_message,
        }
        try:
            guild_id = _active_guild_id(request)
            payload = _event_payload(submitted, guild_id)
            database = get_db()
            if event_id.strip():
                if not event_id.strip().isdigit():
                    raise ValueError("That event no longer exists.")
                existing = database.get_event(int(event_id))
                if existing is None:
                    raise ValueError("That event no longer exists.")
                database.update_event(
                    existing.id,
                    clear_countdown_message=existing.channel_id != payload["channel_id"],
                    **payload,
                )
                request.session["flash"] = {"level": "ok", "text": "Event updated."}
            else:
                database.add_event(**payload)
                request.session["flash"] = {"level": "ok", "text": "Event scheduled."}
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            editing = int(event_id) if event_id.strip().isdigit() else None
            return _events(request, submitted, editing)
        return RedirectResponse("/events", status_code=303)

    @app.post("/events/{event_id}/delete")
    async def delete_event(request: Request, event_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        event = get_db().delete_event(event_id)
        if event is None:
            request.session["flash"] = {"level": "error", "text": "That event no longer exists."}
        else:
            await retire_countdown(event.channel_id, event.countdown_message_id)
            request.session["flash"] = {"level": "ok", "text": "Event removed."}
        return RedirectResponse("/events", status_code=303)

    @app.post("/events/{event_id}/preview")
    async def preview_event(request: Request, event_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        event = get_db().get_event(event_id)
        if event is None:
            request.session["flash"] = {"level": "error", "text": "That event no longer exists."}
        else:
            try:
                await send_preview(event)
            except OpsError as exc:
                request.session["flash"] = {"level": "error", "text": str(exc)}
            else:
                log_activity("panel", f"Preview “{event.name}”", channel=event.channel_id)
                request.session["flash"] = {"level": "ok", "text": "Preview posted in Discord."}
        return RedirectResponse("/events", status_code=303)

    @app.get("/logs")
    async def logs_page(request: Request):
        _require(request)
        database = get_db()
        zone = database.timezone()
        rows = []
        for item in database.list_activity(200):
            rows.append(
                {
                    "item": item,
                    "when": format_local(item.created_at, zone),
                }
            )
        return _page(request, "logs.html", rows=rows)

    @app.get("/commands")
    async def commands_page(request: Request):
        return RedirectResponse("/bot", status_code=302)

    @app.get("/bot")
    async def bot_page(request: Request):
        _require(request)
        database = get_db()
        current_name = ""
        if state.user_label:
            current_name = state.user_label.split("#", 1)[0]
        return _page(
            request,
            "bot.html",
            current_name=current_name,
            picture_command=database.picture_command_name(),
        )

    @app.post("/bot")
    async def save_bot(
        request: Request,
        csrf: str = Form(""),
        username: str = Form(""),
        picture_command: str = Form(""),
    ):
        _require(request)
        _check_csrf(request, csrf)
        database = get_db()
        messages: list[str] = []
        errors: list[str] = []
        new_cmd = picture_command.strip()
        if new_cmd:
            try:
                cleaned = clean_slash_command_name(new_cmd)
                if cleaned != database.picture_command_name():
                    database.set_picture_command_name(cleaned)
                    state.resync_commands = True
                    messages.append(f"Picture command is now /{cleaned}.")
                    log_activity("panel", f"Slash command renamed to /{cleaned}")
            except ValueError as exc:
                errors.append(str(exc))
        cleaned_username = username.strip()
        if cleaned_username:
            try:
                name = clean_bot_username(cleaned_username)
                await rename_bot(name)
                await refresh_channels()
                messages.append(f"Username is now {name}.")
                log_activity("panel", f"Bot renamed to {name}")
            except OpsError as exc:
                errors.append(str(exc))
                log_activity("panel", "Rename failed", detail=str(exc), ok=False)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            request.session["flash"] = {"level": "error", "text": " ".join(errors)}
        elif messages:
            request.session["flash"] = {"level": "ok", "text": " ".join(messages)}
        else:
            request.session["flash"] = {"level": "ok", "text": "Nothing to change."}
        return RedirectResponse("/bot", status_code=303)

    @app.get("/library")
    async def library_page(request: Request):
        _require(request)
        return _page(request, "library.html", images=get_db().list_images())

    @app.post("/library")
    async def upload_image(
        request: Request,
        csrf: str = Form(""),
        name: str = Form(""),
        file: UploadFile | None = File(None),
    ):
        _require(request)
        _check_csrf(request, csrf)
        try:
            if file is None or not file.filename:
                raise ValueError("Choose a picture.")
            raw = await file.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("That picture is over 10MB.")
            extension = await asyncio.to_thread(_sniff_image, raw)
            stored = f"{uuid.uuid4().hex}{extension}"
            path = get_db().image_file(stored)
            await asyncio.to_thread(path.write_bytes, raw)
            try:
                get_db().add_image(name, stored)
            except Exception:
                path.unlink(missing_ok=True)
                raise
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            return _page(request, "library.html", images=get_db().list_images())
        request.session["flash"] = {"level": "ok", "text": "Picture added to the library."}
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/{image_id}/delete")
    async def delete_image(request: Request, image_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        image = get_db().delete_image(image_id)
        if image is None:
            request.session["flash"] = {"level": "error", "text": "That picture is already gone."}
        else:
            get_db().image_file(image.stored_name).unlink(missing_ok=True)
            request.session["flash"] = {"level": "ok", "text": "Picture removed."}
        return RedirectResponse("/library", status_code=303)

    @app.post("/library/{image_id}/avatar")
    async def avatar_now(request: Request, image_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        try:
            await apply_avatar(image_id)
        except OpsError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
        else:
            log_activity("panel", "Profile photo updated from library")
            request.session["flash"] = {"level": "ok", "text": "Profile photo updated."}
        return RedirectResponse("/library", status_code=303)

    @app.get("/avatar")
    async def avatar_page(request: Request):
        _require(request)
        return _page(request, "avatar.html", jobs=_job_views(), images=get_db().list_images(), weekdays=WEEKDAYS)

    @app.post("/avatar")
    async def save_avatar(
        request: Request,
        csrf: str = Form(""),
        image_id: str = Form(""),
        weekday: str = Form(""),
        clock: str = Form(""),
    ):
        _require(request)
        _check_csrf(request, csrf)
        try:
            if not image_id.isdigit():
                raise ValueError("Choose a picture.")
            hour, minute = clean_clock(clock)
            get_db().add_avatar_job(
                image_id=int(image_id),
                weekday=clean_weekday(weekday),
                hour=hour,
                minute=minute,
            )
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            return _page(request, "avatar.html", jobs=_job_views(), images=get_db().list_images(), weekdays=WEEKDAYS)
        request.session["flash"] = {"level": "ok", "text": "Weekly profile photo saved."}
        return RedirectResponse("/avatar", status_code=303)

    @app.post("/avatar/{job_id}/delete")
    async def delete_avatar(request: Request, job_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        get_db().delete_avatar_job(job_id)
        request.session["flash"] = {"level": "ok", "text": "Weekly change removed."}
        return RedirectResponse("/avatar", status_code=303)

    @app.get("/settings")
    async def settings_page(request: Request):
        _require(request)
        current = get_db().timezone_name()
        zones = list(COMMON_TIMEZONES)
        if current not in zones:
            zones.insert(0, current)
        settings = get_settings()
        return _page(
            request,
            "settings.html",
            zones=zones,
            current_zone=current,
            application_id=settings.application_id or state.application_id,
            public_key=settings.public_key,
        )

    @app.post("/settings")
    async def save_settings(request: Request, csrf: str = Form(""), timezone_name: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        name = timezone_name.strip()
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError:
            request.session["flash"] = {
                "level": "error",
                "text": "Unknown timezone. Use a name like Europe/London or America/New_York.",
            }
            return RedirectResponse("/settings", status_code=303)
        get_db().set_setting("timezone", name)
        request.session["flash"] = {"level": "ok", "text": f"Times now use {name}."}
        return RedirectResponse("/settings", status_code=303)

    @app.get("/facts")
    async def facts_page(request: Request):
        _require(request)
        database = get_db()
        guild_id = _active_guild_id(request)
        config = database.get_facts_config(guild_id)
        zone = database.timezone()
        next_label = "Not scheduled"
        if config.next_at is not None:
            next_label = format_local(config.next_at, zone)
        known = {choice.id for choice in state.channels}
        channel_pick = config.channel_id if config.channel_id in known else ""
        channel_override = "" if channel_pick else config.channel_id
        editing = None
        edit_param = request.query_params.get("edit", "").strip()
        if edit_param.isdigit():
            editing = database.get_fact(int(edit_param), guild_id=guild_id)
        return _page(
            request,
            "facts.html",
            config=config,
            facts=database.list_facts(guild_id),
            channels=_guild_channels(guild_id),
            channel_pick=channel_pick,
            channel_override=channel_override,
            next_label=next_label,
            editing=editing,
        )

    @app.post("/facts")
    async def save_facts(
        request: Request,
        csrf: str = Form(""),
        enabled: str = Form(""),
        channel_id: str = Form(""),
        channel_override: str = Form(""),
        min_minutes: str = Form(""),
        max_minutes: str = Form(""),
        attach_library: str = Form(""),
        reset_next: str = Form(""),
    ):
        _require(request)
        _check_csrf(request, csrf)
        try:
            if not min_minutes.strip().isdigit() or not max_minutes.strip().isdigit():
                raise ValueError("Enter shortest and longest wait in minutes.")
            chosen = channel_override.strip() or channel_id.strip()
            on = enabled == "1"
            channel = clean_channel_id(chosen) if on else ""
            low, high = clean_interval_minutes(int(min_minutes), int(max_minutes))
            get_db().save_facts_config(
                _active_guild_id(request),
                enabled=on,
                channel_id=channel,
                min_minutes=low,
                max_minutes=high,
                attach_library_image=attach_library == "1",
                reschedule=on and reset_next == "1",
            )
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            return RedirectResponse("/facts", status_code=303)
        request.session["flash"] = {"level": "ok", "text": "Schedule saved."}
        return RedirectResponse("/facts", status_code=303)

    @app.post("/facts/add")
    async def add_fact(request: Request, csrf: str = Form(""), body: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        try:
            get_db().add_fact(_active_guild_id(request), clean_fact_text(body))
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
        else:
            request.session["flash"] = {"level": "ok", "text": "Fact added."}
        return RedirectResponse("/facts", status_code=303)

    @app.post("/facts/{fact_id}/update")
    async def update_fact_route(
        request: Request,
        fact_id: int,
        csrf: str = Form(""),
        body: str = Form(""),
    ):
        _require(request)
        _check_csrf(request, csrf)
        try:
            get_db().update_fact(fact_id, guild_id=_active_guild_id(request), body=clean_fact_text(body))
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            return RedirectResponse(f"/facts?edit={fact_id}", status_code=303)
        request.session["flash"] = {"level": "ok", "text": "Fact updated."}
        return RedirectResponse("/facts", status_code=303)

    @app.post("/facts/bulk")
    async def bulk_facts(request: Request, csrf: str = Form(""), bulk: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        added = 0
        errors = 0
        for line in bulk.replace("\r\n", "\n").split("\n"):
            text = line.strip()
            if not text:
                continue
            try:
                get_db().add_fact(_active_guild_id(request), clean_fact_text(text))
                added += 1
            except ValueError:
                errors += 1
        if added:
            request.session["flash"] = {"level": "ok", "text": f"Added {added} fact{'s' if added != 1 else ''}."}
        elif errors:
            request.session["flash"] = {"level": "error", "text": "No facts were added. Check the line length."}
        return RedirectResponse("/facts", status_code=303)

    @app.post("/facts/{fact_id}/delete")
    async def delete_fact_route(request: Request, fact_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        get_db().delete_fact(fact_id, guild_id=_active_guild_id(request))
        request.session["flash"] = {"level": "ok", "text": "Fact removed."}
        return RedirectResponse("/facts", status_code=303)

    @app.get("/scheduled-pictures")
    async def scheduled_pictures_page(request: Request):
        _require(request)
        database = get_db()
        guild_id = _active_guild_id(request)
        config = database.get_scheduled_pictures_config(guild_id)
        zone = database.timezone()
        next_label = "Not scheduled"
        if config.next_at is not None:
            next_label = format_local(config.next_at, zone)
        known = {choice.id for choice in state.channels}
        channel_pick = config.channel_id if config.channel_id in known else ""
        channel_override = "" if channel_pick else config.channel_id
        library_count = len(database.list_images())
        return _page(
            request,
            "scheduled_pictures.html",
            config=config,
            channels=_guild_channels(guild_id),
            channel_pick=channel_pick,
            channel_override=channel_override,
            next_label=next_label,
            library_count=library_count,
        )

    @app.post("/scheduled-pictures")
    async def save_scheduled_pictures(
        request: Request,
        csrf: str = Form(""),
        enabled: str = Form(""),
        channel_id: str = Form(""),
        channel_override: str = Form(""),
        min_minutes: str = Form(""),
        max_minutes: str = Form(""),
        reset_next: str = Form(""),
    ):
        _require(request)
        _check_csrf(request, csrf)
        try:
            if not min_minutes.strip().isdigit() or not max_minutes.strip().isdigit():
                raise ValueError("Enter shortest and longest wait in minutes.")
            chosen = channel_override.strip() or channel_id.strip()
            on = enabled == "1"
            channel = clean_channel_id(chosen) if on else ""
            low, high = clean_interval_minutes(int(min_minutes), int(max_minutes))
            get_db().save_scheduled_pictures_config(
                _active_guild_id(request),
                enabled=on,
                channel_id=channel,
                min_minutes=low,
                max_minutes=high,
                reschedule=on and reset_next == "1",
            )
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
            return RedirectResponse("/scheduled-pictures", status_code=303)
        request.session["flash"] = {"level": "ok", "text": "Schedule saved."}
        return RedirectResponse("/scheduled-pictures", status_code=303)

    @app.get("/prompt")
    async def prompt_redirect(request: Request):
        _require(request)
        return RedirectResponse("/bot", status_code=303)

    @app.get("/reactions")
    async def reactions_page(request: Request):
        _require(request)
        return _page(request, "reactions.html", rules=get_db().list_emoji_triggers(_active_guild_id(request)))

    @app.post("/reactions")
    async def add_emoji_trigger(request: Request, csrf: str = Form(""), emoji: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        try:
            get_db().add_emoji_trigger(_active_guild_id(request), clean_emoji_trigger(emoji))
        except ValueError as exc:
            request.session["flash"] = {"level": "error", "text": str(exc)}
        else:
            request.session["flash"] = {"level": "ok", "text": "Emoji saved. A random library picture will be posted."}
        return RedirectResponse("/reactions", status_code=303)

    @app.post("/reactions/{rule_id}/delete")
    async def delete_emoji_trigger(request: Request, rule_id: int, csrf: str = Form("")):
        _require(request)
        _check_csrf(request, csrf)
        get_db().delete_emoji_trigger(rule_id)
        request.session["flash"] = {"level": "ok", "text": "Emoji removed."}
        return RedirectResponse("/reactions", status_code=303)

    @app.get("/media/{image_id}")
    async def media(request: Request, image_id: int):
        if not _logged_in(request):
            raise HTTPException(status_code=401, detail="Login required")
        image = get_db().get_image(image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="Picture not found")
        path = get_db().image_file(image.stored_name)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Picture not found")
        return FileResponse(path)

    return app


def _events(request: Request, form: dict, editing: int | None):
    database = get_db()
    zone = database.timezone()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rows = []
    guild_id = _active_guild_id(request)
    channel_ids = _guild_channel_ids(guild_id)
    for event in database.list_events(guild_id=guild_id, legacy_channel_ids=channel_ids):
        rank = 0 if not event.fired and not event.missed else 1
        rows.append(
            {
                "event": event,
                "status": display_status(event.starts_at, now, event.fired, event.missed, event.attempts),
                "when": format_local(event.starts_at, zone),
                "iso": event.starts_at.isoformat(),
                "recurrence": recurrence_label(event.recurrence),
                "rank": rank,
            }
        )
    rows.sort(key=lambda item: (item["rank"], item["event"].starts_at))
    return _page(
        request,
        "events.html",
        form=form,
        rows=rows,
        images=database.list_images(),
        channels=_guild_channels(guild_id),
        editing=editing,
        recurrence_choices=RECURRENCE_CHOICES,
    )


def _job_views() -> list[dict]:
    database = get_db()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(database.timezone())
    views = []
    for job in database.list_avatar_jobs():
        due = avatar_is_due(job.weekday, job.hour, job.minute, job.last_run_on, job.created_at, local_now)
        when = next_avatar_at(job.weekday, job.hour, job.minute, job.last_run_on, job.created_at, local_now)
        views.append(
            {
                "job": job,
                "due": due,
                "when": "Due now" if due else format_local(when, database.timezone()),
                "clock": f"{job.hour:02d}:{job.minute:02d}",
            }
        )
    return views


def _active_guild_id(request: Request) -> str:
    valid = {guild.id for guild in state.guilds}
    stored = str(request.session.get("guild_id") or "")
    if stored in valid:
        return stored
    if valid:
        first = state.guilds[0].id
        request.session["guild_id"] = first
        return first
    return stored


def _guild_channels(guild_id: str) -> list[ChannelChoice]:
    if not guild_id:
        return list(state.channels)
    return [channel for channel in state.channels if channel.guild_id == guild_id]


def _guild_channel_ids(guild_id: str) -> set[str]:
    return {channel.id for channel in _guild_channels(guild_id)}


def _active_guild_name(guild_id: str) -> str:
    for guild in state.guilds:
        if guild.id == guild_id:
            return guild.name
    return guild_id or "Server"


def _event_payload(form: dict, guild_id: str) -> dict:
    database = get_db()
    message = clean_message(form["message"])
    image_id = _optional_image(form["image_id"])
    pfp_image_id = _optional_image(form["pfp_image_id"])
    if not message and image_id is None:
        raise ValueError("Add a message or choose a picture to post.")
    chosen = form.get("channel_override", "").strip() or form.get("channel_id", "").strip()
    channel_id = clean_channel_id(chosen)
    allowed = _guild_channel_ids(guild_id)
    if state.guilds:
        if not guild_id:
            raise ValueError("Choose which server you are configuring (use the server menu).")
        if allowed and channel_id not in allowed:
            raise ValueError("Choose a channel in the selected server.")
    elif not guild_id:
        guild_id = ""
    return {
        "name": clean_event_name(form["name"]),
        "guild_id": guild_id,
        "starts_at": parse_local_datetime(form["starts_at"], database.timezone()),
        "channel_id": channel_id,
        "message": message,
        "image_id": image_id,
        "pfp_image_id": pfp_image_id,
        "countdown": bool(form.get("countdown")),
        "allow_pings": bool(form.get("allow_pings")),
        "recurrence": clean_recurrence(str(form.get("recurrence", "none"))),
        "daily_before_days": clean_day_count(str(form.get("daily_before_days", "0")), field="Days before"),
        "daily_before_message": form.get("daily_before_message", "").replace("\r\n", "\n").strip(),
        "daily_after_days": clean_day_count(str(form.get("daily_after_days", "0")), field="Days after"),
        "daily_after_message": form.get("daily_after_message", "").replace("\r\n", "\n").strip(),
    }


def _optional_image(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError("Choose a picture from the library.")
    return int(cleaned)


def _blank_form() -> dict:
    return {
        "event_id": "",
        "name": "",
        "starts_at": "",
        "channel_id": "",
        "channel_override": "",
        "message": "",
        "image_id": "",
        "pfp_image_id": "",
        "countdown": False,
        "allow_pings": False,
        "recurrence": "none",
        "daily_before_days": "0",
        "daily_before_message": "{days} days until {name}",
        "daily_after_days": "0",
        "daily_after_message": "{name} was {days} days ago",
    }


def _form_from_event(event: EventRow | None) -> dict:
    if event is None:
        return _blank_form()
    known = {choice.id for choice in state.channels}
    channel_id = event.channel_id if event.channel_id in known else ""
    override = "" if channel_id else event.channel_id
    return {
        "event_id": str(event.id),
        "name": event.name,
        "starts_at": to_local_input(event.starts_at, get_db().timezone()),
        "channel_id": channel_id,
        "channel_override": override,
        "message": event.message,
        "image_id": "" if event.image_id is None else str(event.image_id),
        "pfp_image_id": "" if event.pfp_image_id is None else str(event.pfp_image_id),
        "countdown": event.countdown,
        "allow_pings": event.allow_pings,
        "recurrence": event.recurrence,
        "daily_before_days": str(event.daily_before_days),
        "daily_before_message": event.daily_before_message,
        "daily_after_days": str(event.daily_after_days),
        "daily_after_message": event.daily_after_message,
    }


async def _store_upload(name: str, file: UploadFile | None) -> int:
    if file is None or not file.filename:
        raise ValueError("Choose a picture.")
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("That picture is over 10MB.")
    extension = await asyncio.to_thread(_sniff_image, raw)
    stored = f"{uuid.uuid4().hex}{extension}"
    path = get_db().image_file(stored)
    await asyncio.to_thread(path.write_bytes, raw)
    try:
        return get_db().add_image(name, stored)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _sniff_image(raw: bytes) -> str:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            fmt = (image.format or "").upper()
            image.verify()
    except Exception as exc:
        raise ValueError("That file is not a PNG, JPEG, GIF, or WebP image.") from exc
    mapping = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
    if fmt not in mapping:
        raise ValueError("Use a PNG, JPEG, GIF, or WebP image.")
    return mapping[fmt]


def _page(request: Request, template: str, **context):
    context["flash"] = request.session.pop("flash", None)
    context["csrf"] = _ensure_csrf(request)
    context["path"] = request.url.path
    context["status"] = _public_status()
    context["tz_name"] = get_db().timezone_name()
    guild_id = _active_guild_id(request)
    context["active_guild_id"] = guild_id
    context["active_guild_name"] = _active_guild_name(guild_id)
    context["guild_channels"] = _guild_channels(guild_id)
    return _render(request, template, context)


def _public_status() -> dict:
    return {
        "online": state.online,
        "status_line": state.status_line,
        "notice": state.notice,
        "user_label": state.user_label,
        "latency_ms": state.latency_ms,
        "guild_count": state.guild_count,
        "guilds": state.guilds,
        "invite_url": invite_url(),
        "channel_count": len(state.channels),
        "picture_command": get_db().picture_command_name(),
    }


def _render(request: Request, name: str, context: dict):
    payload = dict(context)
    payload.setdefault("request", request)
    params = list(inspect.signature(templates.TemplateResponse).parameters)
    if params and params[0] == "request":
        return templates.TemplateResponse(request, name, payload)
    return templates.TemplateResponse(name, payload)


def _logged_in(request: Request) -> bool:
    return request.session.get("user") is True


def _require(request: Request) -> None:
    if not _logged_in(request):
        raise NeedLogin(request.url.path)


def _ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return str(token)


def _check_csrf(request: Request, token: str) -> None:
    expected = str(request.session.get("csrf") or "")
    if not expected or not _same(token, expected):
        raise HTTPException(status_code=403, detail="The form expired. Reload the page and try again.")


def _same(given: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(given, expected)
    except Exception:
        return False


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _session_secret() -> str:
    directory = get_settings().data_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "session.key"
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret
