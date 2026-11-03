import asyncio
import logging
import time

import discord
from discord.ext import commands

from app.config import get_settings
from app.state import ChannelChoice, GuildSummary, state

log = logging.getLogger("botdesk.bot")

INVITE_PERMISSIONS = 117760


class DeskBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, chunk_guilds_at_startup=False)
        self._synced: set[int] = set()
        self._sync_attempt = 0.0
        self._commands_ready = False

    async def setup_hook(self) -> None:
        self.ensure_commands()

    def ensure_commands(self) -> None:
        from app.commands import attach

        attach(self)
        self._commands_ready = True

    async def on_ready(self) -> None:
        await refresh_channels()
        await self.ensure_synced()
        log.info("Logged in as %s", self.user)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_one(guild)
        await refresh_channels()

    async def on_message(self, message: discord.Message) -> None:
        await self.process_commands(message)
        from app.reactions import maybe_reply_with_emoji_pool

        await maybe_reply_with_emoji_pool(message)

    async def ensure_synced(self) -> None:
        if not self.is_ready():
            return
        if state.resync_commands:
            await self.resync_all_commands()
            state.resync_commands = False
            return
        pending = [guild for guild in self.guilds if guild.id not in self._synced]
        if not pending:
            return
        now = time.monotonic()
        if now - self._sync_attempt < 60:
            return
        self._sync_attempt = now
        for guild in pending:
            try:
                await self.sync_one(guild)
            except Exception:
                log.exception("Command sync failed for %s", guild.name)
                state.notice = f"Could not register commands in {guild.name}. They will be tried again shortly."
        if all(guild.id in self._synced for guild in self.guilds):
            if state.notice.startswith("Could not register commands"):
                state.notice = ""

    async def sync_one(self, guild: discord.Guild) -> None:
        if guild.id in self._synced:
            return
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self._synced.add(guild.id)
        log.info("Commands ready in %s", guild.name)

    async def resync_all_commands(self) -> None:
        from app.commands import rebuild_commands

        name = rebuild_commands(self)
        self._synced.clear()
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                self._synced.add(guild.id)
                log.info("Commands updated in %s (/%s)", guild.name, name)
            except Exception:
                log.exception("Command resync failed for %s", guild.name)
                state.notice = f"Could not update commands in {guild.name}. They will be tried again shortly."


bot = DeskBot()
_commands_attached = False


def ensure_commands() -> None:
    global _commands_attached
    if _commands_attached:
        return
    bot.ensure_commands()
    _commands_attached = True


def invite_url() -> str:
    app_id = state.application_id or get_settings().application_id
    if not app_id:
        return ""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={app_id}"
        f"&permissions={INVITE_PERMISSIONS}"
        "&scope=bot%20applications.commands"
    )


async def refresh_channels() -> None:
    if not bot.is_ready() or bot.user is None:
        return
    choices: list[ChannelChoice] = []
    guilds: list[GuildSummary] = []
    for guild in bot.guilds:
        me = guild.me
        postable = 0
        for channel in guild.text_channels:
            if me is not None and not channel.permissions_for(me).send_messages:
                continue
            postable += 1
            choices.append(
                ChannelChoice(
                    str(channel.id),
                    f"{guild.name} · #{channel.name}",
                    str(guild.id),
                    guild.name,
                    channel.name,
                )
            )
        members = guild.member_count
        guilds.append(
            GuildSummary(
                id=str(guild.id),
                name=guild.name,
                member_count=members,
                postable_channels=postable,
            )
        )
    choices.sort(key=lambda item: item.label.lower())
    guilds.sort(key=lambda item: item.name.lower())
    state.channels = choices
    state.guilds = guilds
    state.guild_count = len(guilds)
    state.user_label = str(bot.user)
    state.application_id = str(bot.application_id or "")
    latency = bot.latency
    if latency is None or latency == float("inf"):
        state.latency_ms = 0
    else:
        state.latency_ms = max(0, int(latency * 1000))
    state.online = True
    state.status_line = f"Online as {bot.user}"


async def start_bot() -> None:
    token = get_settings().token
    if not token:
        state.set_offline("Add DISCORD_TOKEN to the .env file and restart the container.")
        log.warning("No DISCORD_TOKEN set. The panel is up and the bot is offline.")
        await _wait_forever()
        return
    while True:
        try:
            await bot.start(token)
        except asyncio.CancelledError:
            if not bot.is_closed():
                await bot.close()
            raise
        except discord.LoginFailure:
            state.set_offline("Discord rejected the token. Check DISCORD_TOKEN, then restart.")
            log.error("Discord rejected the token")
            await _wait_forever()
            return
        except discord.PrivilegedIntentsRequired:
            state.set_offline(
                "Turn on Message Content Intent for this bot in the Developer Portal, then restart."
            )
            log.error("Privileged intents were rejected")
            await _wait_forever()
            return
        except Exception:
            log.exception("Discord connection failed")
            state.set_offline("The bot lost its Discord connection. Retrying.")
            await asyncio.sleep(20)


async def _wait_forever() -> None:
    while True:
        await asyncio.sleep(3600)
