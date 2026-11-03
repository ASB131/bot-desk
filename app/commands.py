import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from app.activity import log_activity
from app.db import get_db
from app.discord_ops import OpsError, countdown_snapshot, random_library_file

log = logging.getLogger("botdesk.commands")

_signature: str | None = None


def _place(interaction: discord.Interaction) -> tuple[str, str]:
    guild = interaction.guild.name if interaction.guild else ""
    channel = getattr(interaction.channel, "name", "") or ""
    return guild, channel


def attach(bot: commands.Bot) -> None:
    if not getattr(bot, "_desk_errors", False):

        @bot.tree.error
        async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error
            log.exception("Command failed", exc_info=cause)
            text = str(cause) if isinstance(cause, OpsError) else "That command failed."
            guild, channel = _place(interaction)
            log_activity("command", "Slash command failed", detail=text, guild=guild, channel=channel, ok=False)
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

        bot._desk_errors = True
    rebuild_commands(bot)


def rebuild_commands(bot: commands.Bot) -> str:
    global _signature
    picture_name = get_db().picture_command_name()
    bot.tree.clear_commands(guild=None)
    tree = bot.tree

    async def complete_events(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.lower()
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        guild_id = str(interaction.guild.id) if interaction.guild else ""
        channel_ids = (
            {str(channel.id) for channel in interaction.guild.text_channels} if interaction.guild else set()
        )
        for event in get_db().list_events(guild_id=guild_id, legacy_channel_ids=channel_ids):
            if event.name in seen:
                continue
            if needle in event.name.lower():
                seen.add(event.name)
                choices.append(app_commands.Choice(name=event.name, value=event.name))
            if len(choices) == 25:
                break
        return choices

    @tree.command(name=picture_name, description="Post a random picture from the library")
    @app_commands.describe(caption="Optional text sent with the picture")
    @app_commands.guild_only()
    async def random_picture(interaction: discord.Interaction, caption: Optional[str] = None) -> None:
        guild, channel = _place(interaction)
        try:
            attachment, image_name = random_library_file()
        except OpsError as exc:
            log_activity(
                "command",
                f"/{picture_name}",
                detail=str(exc),
                guild=guild,
                channel=channel,
                ok=False,
            )
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if caption and len(caption) > 2000:
            await interaction.response.send_message("That caption is too long for Discord.", ephemeral=True)
            return
        kwargs: dict = {"file": attachment}
        if caption:
            kwargs["content"] = caption
        await interaction.response.send_message(**kwargs)
        log_activity(
            "command",
            f"/{picture_name} → {image_name}",
            guild=guild,
            channel=channel,
        )

    @tree.command(name="countdown", description="Show the time left until an event")
    @app_commands.describe(name="Event name. Leave blank for the next event.")
    @app_commands.autocomplete(name=complete_events)
    @app_commands.guild_only()
    async def countdown(interaction: discord.Interaction, name: Optional[str] = None) -> None:
        guild, channel = _place(interaction)
        try:
            guild_id = str(interaction.guild.id) if interaction.guild else ""
            channel_ids = (
                {str(channel.id) for channel in interaction.guild.text_channels}
                if interaction.guild
                else set()
            )
            text = countdown_snapshot(name, guild_id=guild_id, channel_ids=channel_ids)
        except OpsError as exc:
            log_activity("command", "/countdown", detail=str(exc), guild=guild, channel=channel, ok=False)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(text)
        log_activity("command", "/countdown", guild=guild, channel=channel)

    _signature = picture_name
    return _signature
