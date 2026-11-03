import logging
from datetime import datetime, timedelta, timezone

import discord

from app.activity import log_activity
from app.bot_client import bot, refresh_channels
from app.db import get_db
from app.discord_ops import (
    OpsError,
    apply_avatar,
    deliver_event,
    mark_countdown_started,
    next_presence,
    send_fact_post,
    send_scheduled_library_image,
    update_presence,
    upsert_countdown,
)
from app.logic import avatar_is_due, classify_event, daily_announcement_text

log = logging.getLogger("botdesk.schedule")

_countdown_at: dict[int, datetime] = {}
_event_try_at: dict[int, datetime] = {}
_presence = {"text": None, "at": None}
_channels_at: datetime | None = None


async def scheduler_loop() -> None:
    await _sleep(2)
    while True:
        try:
            await tick()
        except Exception:
            log.exception("Scheduler tick failed")
        await _sleep(5)


async def tick() -> None:
    global _channels_at
    now = datetime.now(timezone.utc)
    if bot.is_ready():
        await bot.ensure_synced()
        if _channels_at is None or now - _channels_at >= timedelta(seconds=60):
            await refresh_channels()
            _channels_at = now
        await _avatars(now)
        await _events(now)
        await _countdowns(now)
        await _daily_announcements(now)
        await _facts(now)
        await _scheduled_pictures(now)
        await _presence_tick(now)


async def _avatars(now: datetime) -> None:
    database = get_db()
    local_now = now.astimezone(database.timezone())
    today = local_now.date().isoformat()
    for job in database.list_avatar_jobs():
        if not avatar_is_due(job.weekday, job.hour, job.minute, job.last_run_on, job.created_at, local_now):
            continue
        try:
            await apply_avatar(job.image_id)
        except OpsError as exc:
            log.warning("Avatar job %s failed: %s", job.id, exc)
            database.mark_avatar_ran(job.id, today, str(exc))
        else:
            database.mark_avatar_ran(job.id, today, "")


async def _events(now: datetime) -> None:
    database = get_db()
    for event in database.list_events():
        kind = classify_event(event.starts_at, now, event.fired, event.missed)
        if kind == "stale":
            database.mark_missed(event.id)
            continue
        if kind != "due":
            continue
        last = _event_try_at.get(event.id)
        if last is not None and now - last < timedelta(seconds=30):
            continue
        _event_try_at[event.id] = now
        try:
            await deliver_event(event)
        except (OpsError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, OpsError) else "Discord refused the message."
            log.warning("Event %s failed: %s", event.name, message)
            database.record_failure(event.id, message)
            continue
        pfp_error = ""
        if event.pfp_image_id is not None:
            try:
                await apply_avatar(event.pfp_image_id)
            except OpsError as exc:
                pfp_error = str(exc)
                log.warning("Event avatar %s failed: %s", event.name, exc)
        database.complete_event_delivery(event.id, pfp_error)
        log_activity("event", f"Sent “{event.name}”", channel=event.channel_id)
        if event.countdown:
            try:
                await mark_countdown_started(event)
            except (OpsError, discord.HTTPException):
                log.warning("Could not update the countdown for %s", event.name)


async def _countdowns(now: datetime) -> None:
    database = get_db()
    for event in database.list_events():
        if event.fired or event.missed or not event.countdown or event.starts_at <= now:
            continue
        last = _countdown_at.get(event.id)
        if last is not None and now - last < timedelta(seconds=60):
            continue
        _countdown_at[event.id] = now
        try:
            await upsert_countdown(event, now)
        except (OpsError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, OpsError) else "Could not update the countdown message."
            log.warning("Countdown %s: %s", event.name, message)
            database.set_event_error(event.id, message)


async def _daily_announcements(now: datetime) -> None:
    database = get_db()
    tz = database.timezone()
    local = now.astimezone(tz)
    today = local.date().isoformat()
    for event in database.list_events():
        if event.missed:
            continue
        if event.daily_before_days <= 0 and event.daily_after_days <= 0:
            continue
        post_at = event.starts_at.astimezone(tz)
        if (local.hour, local.minute) < (post_at.hour, post_at.minute):
            continue
        if event.daily_last_posted_on == today:
            database.clear_after_anchor_if_done(event.id, today)
            continue
        text = daily_announcement_text(event, local.date(), tz)
        if not text:
            database.clear_after_anchor_if_done(event.id, today)
            continue
        try:
            await send_channel_text(event.channel_id, text)
        except (OpsError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, OpsError) else "Could not send the daily message."
            log.warning("Daily message %s: %s", event.name, message)
            database.set_event_error(event.id, message)
            continue
        database.set_daily_last_posted(event.id, today)
        database.clear_after_anchor_if_done(event.id, today)
        log_activity("daily", f"“{event.name}”", detail=text[:120], channel=event.channel_id)


async def _facts(now: datetime) -> None:
    database = get_db()
    for guild_id in database.list_facts_guilds():
        config = database.get_facts_config(guild_id)
        if not config.channel_id:
            continue
        if config.next_at is None:
            database.reschedule_facts(guild_id, now)
            continue
        if now < config.next_at:
            continue
        fact = database.pick_random_fact(guild_id)
        if fact is None:
            database.set_facts_error(guild_id, "Add at least one fact on the panel.")
            database.reschedule_facts(guild_id, now)
            continue
        try:
            await send_fact_post(
                config.channel_id,
                fact.body,
                attach_library_image=config.attach_library_image,
            )
        except (OpsError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, OpsError) else "Discord refused the fact."
            log.warning("Fact post failed (%s): %s", guild_id, message)
            database.set_facts_error(guild_id, message)
        else:
            database.set_facts_error(guild_id, "")
            log.info("Posted a random fact in guild %s", guild_id)
            log_activity("fact", "Posted a random fact", channel=config.channel_id)
        database.reschedule_facts(guild_id, now)


async def _scheduled_pictures(now: datetime) -> None:
    database = get_db()
    for guild_id in database.list_scheduled_pictures_guilds():
        config = database.get_scheduled_pictures_config(guild_id)
        if not config.channel_id:
            continue
        if config.next_at is None:
            database.reschedule_scheduled_pictures(guild_id, now)
            continue
        if now < config.next_at:
            continue
        try:
            await send_scheduled_library_image(config.channel_id)
        except (OpsError, discord.HTTPException) as exc:
            message = str(exc) if isinstance(exc, OpsError) else "Discord refused the picture."
            log.warning("Scheduled picture failed (%s): %s", guild_id, message)
            database.set_scheduled_pictures_error(guild_id, message)
        else:
            database.set_scheduled_pictures_error(guild_id, "")
            log.info("Posted a scheduled library picture in guild %s", guild_id)
            log_activity("picture", "Posted a scheduled library picture", channel=config.channel_id)
        database.reschedule_scheduled_pictures(guild_id, now)


async def _presence_tick(now: datetime) -> None:
    text = next_presence(now)
    previous = _presence["text"]
    updated = _presence["at"]
    if text == previous and updated is not None and now - updated < timedelta(minutes=10):
        return
    try:
        await update_presence(text)
    except (OpsError, discord.HTTPException) as exc:
        log.warning("Could not update presence: %s", exc)
        return
    _presence["text"] = text
    _presence["at"] = now


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
