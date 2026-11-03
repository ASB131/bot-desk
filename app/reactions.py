import logging

import discord

from app.activity import log_activity
from app.db import get_db
from app.discord_ops import reply_random_library
from app.logic import message_has_emoji

log = logging.getLogger("botdesk.reactions")


async def maybe_reply_with_emoji_pool(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return
    content = message.content
    if not content:
        return
    database = get_db()
    guild_id = str(message.guild.id)
    for rule in database.list_emoji_triggers(guild_id):
        if not message_has_emoji(content, rule.emoji):
            continue
        guild = message.guild.name if message.guild else ""
        channel = getattr(message.channel, "name", "") or ""
        try:
            image_name = await reply_random_library(message)
        except Exception as exc:
            log.exception("Emoji reaction failed for %s", rule.emoji)
            log_activity(
                "emoji",
                f"{rule.emoji} → library",
                detail=str(exc),
                guild=guild,
                channel=channel,
                ok=False,
            )
        else:
            detail = image_name or ""
            log_activity(
                "emoji",
                f"{rule.emoji} → library" + (f" ({detail})" if detail else ""),
                guild=guild,
                channel=channel,
            )
        return
