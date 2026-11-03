import asyncio
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from PIL import Image

from app.bot_client import bot
from app.db import EventRow, ImageRow, get_db
from app.logic import PROMPT_POOL_SLUG
from app.logic import (
    countdown_text,
    format_local,
    humanize_delta,
    presence_name,
    render_message,
    started_text,
)

log = logging.getLogger("botdesk.ops")
lock = asyncio.Lock()


class OpsError(Exception):
    pass


def require_ready() -> None:
    if not bot.is_ready() or bot.user is None:
        raise OpsError("The bot is not connected to Discord yet.")


def library_file(name: str) -> discord.File:
    image = get_db().get_image_by_name(name)
    if image is None:
        raise OpsError(f"No picture named {name}.")
    return image_row_file(image)


def image_row_file(image: ImageRow) -> discord.File:
    path = get_db().image_file(image.stored_name)
    if not path.exists():
        raise OpsError("That picture file is missing from the server.")
    return discord.File(path, filename=f"{image.name}{path.suffix}")


def random_library_file() -> tuple[discord.File, str]:
    try:
        image = get_db().pick_library_image()
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    return image_row_file(image), image.name


def random_pool_file(pool_slug: str) -> discord.File:
    database = get_db()
    pool = database.get_pool_by_slug(pool_slug)
    if pool is None:
        raise OpsError("That picture pool does not exist.")
    image = database.pick_random_pool_image(pool.id)
    if image is None:
        label = "Prompt" if pool_slug == PROMPT_POOL_SLUG else pool.label
        raise OpsError(f"{label} has no pictures yet. Add some on the panel.")
    return image_row_file(image)


async def send_channel_text(channel_id: str, content: str) -> None:
    await send_channel_post(channel_id, content)


async def send_fact_post(channel_id: str, content: str, *, attach_library_image: bool) -> None:
    image_id: int | None = None
    if attach_library_image:
        try:
            image_id = get_db().pick_library_image().id
        except ValueError:
            image_id = None
    await send_channel_post(channel_id, content, image_id=image_id)


async def send_scheduled_library_image(channel_id: str) -> None:
    try:
        image = get_db().pick_library_image()
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    await send_channel_post(channel_id, "", image_id=image.id)


async def send_channel_post(channel_id: str, content: str, *, image_id: int | None = None) -> None:
    channel = await _resolve_channel(channel_id)
    text = content.strip()
    file: discord.File | None = None
    if image_id is not None:
        image = get_db().get_image(image_id)
        if image is None:
            raise OpsError("The picture for this fact is no longer in the library.")
        file = image_row_file(image)
    if not text and file is None:
        raise OpsError("Nothing to post.")
    if text and len(text) > 2000:
        raise OpsError("That message is longer than Discord allows.")
    _check_send(channel, file is not None)
    await _send(channel, text or None, file, discord.AllowedMentions.none())


async def send_random_pool(channel_id: str, pool_slug: str) -> None:
    file = random_pool_file(pool_slug)
    channel = await _resolve_channel(channel_id)
    _check_send(channel, True)
    await _send(channel, None, file, discord.AllowedMentions.none())


async def reply_random_pool(message: discord.Message, pool_slug: str) -> None:
    require_ready()
    if message.author.bot or message.guild is None:
        return
    channel = message.channel
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        file = random_pool_file(pool_slug)
    except OpsError:
        return
    if isinstance(channel, discord.abc.GuildChannel):
        me = message.guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return
        if not channel.permissions_for(me).attach_files:
            return
    await _send(channel, None, file, discord.AllowedMentions.none())


async def reply_random_library(message: discord.Message) -> str:
    require_ready()
    if message.author.bot or message.guild is None:
        return ""
    channel = message.channel
    if not isinstance(channel, discord.abc.Messageable):
        return ""
    try:
        attachment, image_name = random_library_file()
    except OpsError:
        return ""
    if isinstance(channel, discord.abc.GuildChannel):
        me = message.guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return ""
        if not channel.permissions_for(me).attach_files:
            return ""
    await _send(channel, None, attachment, discord.AllowedMentions.none())
    return image_name


def countdown_snapshot(name: str | None = None, *, guild_id: str = "", channel_ids: set[str] | None = None) -> str:
    database = get_db()
    now = datetime.now(timezone.utc)
    zone = database.timezone()
    all_events = database.list_events(guild_id=guild_id, legacy_channel_ids=channel_ids or set())
    events = [event for event in all_events if not event.fired and not event.missed]
    if name:
        match = next((event for event in events if event.name.lower() == name.lower()), None)
        if match is None:
            match = next(
                (
                    event
                    for event in database.list_events()
                    if event.name.lower() == name.lower()
                ),
                None,
            )
        if match is None:
            raise OpsError(f"No event named {name}.")
        event = match
    elif events:
        event = min(events, key=lambda item: item.starts_at)
    else:
        return "No events are scheduled."
    remaining = humanize_delta(event.starts_at - now)
    if event.fired:
        return f"**{event.name}** already started.\n{format_local(event.starts_at, zone)}"
    if remaining == "now":
        return f"**{event.name}** is due now.\n{format_local(event.starts_at, zone)}"
    return f"**{event.name}** starts in {remaining}.\n{format_local(event.starts_at, zone)}"


def next_presence(now: datetime) -> str | None:
    upcoming = [event for event in get_db().list_events() if not event.fired and not event.missed]
    if not upcoming:
        return None
    nxt = min(upcoming, key=lambda item: item.starts_at)
    return presence_name(nxt.name, nxt.starts_at, now)


async def rename_bot(username: str) -> None:
    require_ready()
    async with lock:
        user = bot.user
        if user is None:
            raise OpsError("The bot is not connected to Discord yet.")
        try:
            await user.edit(username=username)
        except discord.HTTPException as exc:
            if exc.status == 429:
                raise OpsError("Discord is limiting username changes. Try again in an hour.") from exc
            raise OpsError("Discord refused that username. Check the rules and try again.") from exc


async def apply_avatar(image_id: int) -> None:
    require_ready()
    database = get_db()
    image = database.get_image(image_id)
    if image is None:
        raise OpsError("That picture is not in the library.")
    path = database.image_file(image.stored_name)
    if not path.exists():
        raise OpsError("That picture file is missing from the server.")
    try:
        payload = await asyncio.to_thread(_avatar_bytes, path)
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError("That picture could not be used as a profile photo.") from exc
    async with lock:
        user = bot.user
        if user is None:
            raise OpsError("The bot is not connected to Discord yet.")
        try:
            await user.edit(avatar=payload)
        except discord.HTTPException as exc:
            if exc.status == 429:
                raise OpsError(
                    "Discord is limiting profile photo changes right now. A weekly change is fine. Wait, then try again."
                ) from exc
            raise OpsError("Discord refused that profile photo. Use a PNG, JPEG, GIF, or WebP under 8MB.") from exc
    log.info("Profile photo set to %s", image.name)


async def _send(channel, content: str | None, file: discord.File | None, mentions: discord.AllowedMentions) -> None:
    kwargs: dict = {"allowed_mentions": mentions}
    if content:
        kwargs["content"] = content
    if file is not None:
        kwargs["file"] = file
    async with lock:
        await channel.send(**kwargs)


async def send_preview(event: EventRow) -> None:
    content, file, mentions = _announcement_parts(event, prefix="Preview")
    channel = await _resolve_channel(event.channel_id)
    _check_send(channel, file is not None)
    await _send(channel, content, file, mentions)


async def deliver_event(event: EventRow) -> None:
    content, file, mentions = _announcement_parts(event, prefix="")
    channel = await _resolve_channel(event.channel_id)
    _check_send(channel, file is not None)
    await _send(channel, content, file, mentions)
    log.info("Sent event %s", event.name)


async def upsert_countdown(event: EventRow, now: datetime) -> None:
    database = get_db()
    text = countdown_text(event.name, event.starts_at, now, database.timezone())
    channel = await _resolve_channel(event.channel_id)
    _check_send(channel, False)
    async with lock:
        if event.countdown_message_id:
            try:
                message = await channel.fetch_message(int(event.countdown_message_id))
            except discord.NotFound:
                message = None
            except discord.Forbidden as exc:
                raise OpsError(
                    "The bot cannot edit its countdown in that channel. Allow View Channel, Send Messages, and Read Message History."
                ) from exc
            else:
                if message.content != text:
                    await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
                return
        message = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        database.set_countdown_message(event.id, str(message.id))


async def mark_countdown_started(event: EventRow) -> None:
    if not event.countdown_message_id:
        return
    database = get_db()
    text = started_text(event.name, event.starts_at, database.timezone())
    channel = await _resolve_channel(event.channel_id)
    async with lock:
        try:
            message = await channel.fetch_message(int(event.countdown_message_id))
            await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Could not close the countdown for %s", event.name)


async def retire_countdown(channel_id: str, message_id: str | None) -> None:
    if not message_id or not bot.is_ready():
        return
    try:
        channel = await _resolve_channel(channel_id)
    except OpsError:
        return
    async with lock:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content="This event was removed.", allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return


async def update_presence(text: str | None) -> None:
    require_ready()
    activity = None
    if text:
        activity = discord.Activity(type=discord.ActivityType.watching, name=text)
    async with lock:
        await bot.change_presence(activity=activity)


def _announcement_parts(event: EventRow, *, prefix: str) -> tuple[str | None, discord.File | None, discord.AllowedMentions]:
    database = get_db()
    now = datetime.now(timezone.utc)
    zone = database.timezone()
    rendered = ""
    if event.message:
        rendered = render_message(
            event.message,
            name=event.name,
            when_label=format_local(event.starts_at, zone),
            countdown_label=humanize_delta(event.starts_at - now),
        )
    if prefix and rendered:
        content = f"{prefix}\n{rendered}"
    elif prefix:
        content = prefix
    else:
        content = rendered
    file = _event_file(event)
    if not content.strip() and file is None:
        raise OpsError("Add a message or a picture before this can be posted.")
    if len(content) > 2000:
        raise OpsError("That message is longer than Discord allows (2000 characters).")
    mentions = discord.AllowedMentions.all() if event.allow_pings else discord.AllowedMentions.none()
    return (content or None), file, mentions


def _event_file(event: EventRow) -> discord.File | None:
    if event.image_id is None:
        return None
    database = get_db()
    image = database.get_image(event.image_id)
    if image is None:
        raise OpsError("The picture for this event is no longer in the library.")
    path = database.image_file(image.stored_name)
    if not path.exists():
        raise OpsError("The picture file for this event is missing.")
    return discord.File(path, filename=f"{image.name}{path.suffix}")


async def _resolve_channel(channel_id: str) -> discord.abc.Messageable:
    require_ready()
    try:
        snowflake = int(channel_id)
    except ValueError as exc:
        raise OpsError("That channel ID is not valid.") from exc
    channel = bot.get_channel(snowflake)
    if channel is None:
        try:
            channel = await bot.fetch_channel(snowflake)
        except discord.NotFound as exc:
            raise OpsError("Discord cannot find that channel. Check the ID and that the bot is in the server.") from exc
        except discord.Forbidden as exc:
            raise OpsError("The bot cannot see that channel.") from exc
    if not isinstance(channel, discord.abc.Messageable):
        raise OpsError("That channel cannot receive messages.")
    return channel


def _check_send(channel: discord.abc.Messageable, needs_file: bool) -> None:
    guild = getattr(channel, "guild", None)
    me = guild.me if guild is not None else None
    if me is None or not hasattr(channel, "permissions_for"):
        return
    perms = channel.permissions_for(me)
    if not perms.view_channel or not perms.send_messages:
        raise OpsError("The bot cannot send messages in that channel.")
    if needs_file and not perms.attach_files:
        raise OpsError("The bot cannot attach files in that channel.")


def _avatar_bytes(path: Path) -> bytes:
    with Image.open(path) as image:
        if getattr(image, "is_animated", False):
            data = path.read_bytes()
            if len(data) > 8 * 1024 * 1024:
                raise OpsError("That GIF is over 8MB. Discord would reject it as a profile photo.")
            return data
        frame = image.convert("RGBA")
        side = min(frame.size)
        left = (frame.width - side) // 2
        top = (frame.height - side) // 2
        frame = frame.crop((left, top, left + side, top + side))
        frame = frame.resize((512, 512), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        return buffer.getvalue()
