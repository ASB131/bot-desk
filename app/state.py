from dataclasses import dataclass


@dataclass
class ChannelChoice:
    id: str
    label: str
    guild_id: str
    guild_name: str
    channel_name: str


@dataclass
class GuildSummary:
    id: str
    name: str
    member_count: int | None
    postable_channels: int


class State:
    def __init__(self) -> None:
        self.online = False
        self.status_line = "Starting"
        self.notice = ""
        self.user_label = ""
        self.application_id = ""
        self.guild_count = 0
        self.latency_ms = 0
        self.channels: list[ChannelChoice] = []
        self.guilds: list[GuildSummary] = []
        self.resync_commands = False

    def set_offline(self, message: str) -> None:
        self.online = False
        self.status_line = message
        self.user_label = ""
        self.latency_ms = 0
        self.guild_count = 0
        self.channels = []
        self.guilds = []


state = State()
