import calendar
import random
import re
from datetime import date, datetime, timedelta, timezone, tzinfo

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

COMMON_TIMEZONES = [
    "UTC",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Toronto",
    "Australia/Sydney",
    "Pacific/Auckland",
    "Asia/Tokyo",
    "Asia/Singapore",
]

MISSED_AFTER = timedelta(hours=12)
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,40}")
POOL_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
PROMPT_POOL_SLUG = "prompt"
SLASH_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
BOT_USERNAME_RE = re.compile(r"^[a-z0-9_.]{2,32}$", re.IGNORECASE)


def clean_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not NAME_RE.fullmatch(cleaned):
        raise ValueError("Use a short name: letters, numbers, spaces, and hyphens.")
    return cleaned


def clean_event_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned or len(cleaned) > 80:
        raise ValueError("Give the event a name up to 80 characters.")
    return cleaned


def clean_guild_id(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.isdigit() or len(cleaned) < 17 or len(cleaned) > 20:
        raise ValueError("Choose a server from the list.")
    return cleaned


def clean_channel_id(value: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(r"\d{17,20}", cleaned):
        raise ValueError("Choose a channel, or paste a numeric channel ID.")
    return cleaned


def clean_message(value: str) -> str:
    text = value.replace("\r\n", "\n").strip()
    if len(text) > 1800:
        raise ValueError("The message is too long. Keep it under 1800 characters.")
    return text


def clean_clock(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value.strip())
    if not match:
        raise ValueError("Choose a time.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Choose a time.")
    return hour, minute


def clean_weekday(value: str) -> int:
    if value not in {str(index) for index in range(7)}:
        raise ValueError("Choose a day.")
    return int(value)


def parse_local_datetime(value: str, tz: tzinfo) -> datetime:
    raw = value.strip()
    if "T" not in raw:
        raise ValueError("Choose a date and time.")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Choose a date and time.") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc)


def format_local(when: datetime, tz: tzinfo) -> str:
    return when.astimezone(tz).strftime("%A %d %b %Y, %H:%M")


def to_local_input(when: datetime, tz: tzinfo) -> str:
    return when.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def humanize_delta(delta: timedelta, *, with_seconds: bool = False) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "now"
    if not with_seconds and total < 60:
        return "under a minute"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if with_seconds and not days and seconds:
        parts.append(f"{seconds}s")
    if not parts:
        return "under a minute"
    return " ".join(parts)


def render_message(template: str, *, name: str, when_label: str, countdown_label: str) -> str:
    return (
        template.replace("{name}", name)
        .replace("{time}", when_label)
        .replace("{countdown}", countdown_label)
    )


def countdown_text(name: str, starts_at: datetime, now: datetime, tz: tzinfo) -> str:
    remaining = humanize_delta(starts_at - now)
    return f"**{name}**\nStarts in {remaining}\n{format_local(starts_at, tz)}"


def started_text(name: str, starts_at: datetime, tz: tzinfo) -> str:
    return f"**{name}** has started.\n{format_local(starts_at, tz)}"


RECURRENCE_CHOICES = ("none", "daily", "weekly", "monthly", "yearly")
RECURRENCE_LABELS = {
    "none": "Once",
    "daily": "Every day",
    "weekly": "Every week",
    "monthly": "Every month",
    "yearly": "Every year",
}


def clean_recurrence(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned not in RECURRENCE_CHOICES:
        raise ValueError("Choose how often the event repeats.")
    return cleaned


def clean_day_count(value: str, *, field: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        return 0
    if not cleaned.isdigit():
        raise ValueError(f"{field} must be a number of days.")
    days = int(cleaned)
    if days < 0 or days > 366:
        raise ValueError(f"{field} must be between 0 and 366.")
    return days


def render_days_message(template: str, *, name: str, days: int) -> str:
    text = template.replace("{name}", name).replace("{days}", str(days))
    if days == 1:
        text = text.replace("{days_label}", "1 day")
    else:
        text = text.replace("{days_label}", f"{days} days")
    return text


def next_occurrence(starts_at: datetime, recurrence: str) -> datetime:
    if recurrence == "none":
        return starts_at
    if recurrence == "daily":
        return starts_at + timedelta(days=1)
    if recurrence == "weekly":
        return starts_at + timedelta(weeks=1)
    if recurrence == "monthly":
        return _add_months(starts_at, 1)
    if recurrence == "yearly":
        return _add_years(starts_at, 1)
    return starts_at


def _add_months(when: datetime, months: int) -> datetime:
    month = when.month - 1 + months
    year = when.year + month // 12
    month = month % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def _add_years(when: datetime, years: int) -> datetime:
    year = when.year + years
    day = when.day
    if when.month == 2 and when.day == 29 and calendar.monthrange(year, 2)[1] == 28:
        day = 28
    return when.replace(year=year, day=day)


def daily_announcement_text(event, today: date, tz: tzinfo) -> str | None:
    name = event.name
    if event.daily_before_days > 0 and event.daily_before_message.strip():
        if not event.fired:
            target = event.starts_at.astimezone(tz).date()
            days_until = (target - today).days
            if 1 <= days_until <= event.daily_before_days:
                return render_days_message(event.daily_before_message, name=name, days=days_until)
    if event.daily_after_days > 0 and event.daily_after_message.strip() and event.after_anchor_on:
        try:
            anchor = date.fromisoformat(event.after_anchor_on)
        except ValueError:
            anchor = None
        if anchor is not None:
            days_ago = (today - anchor).days
            if 1 <= days_ago <= event.daily_after_days:
                return render_days_message(event.daily_after_message, name=name, days=days_ago)
    return None


def recurrence_label(value: str) -> str:
    return RECURRENCE_LABELS.get(value, value)


def classify_event(starts_at: datetime, now: datetime, fired: bool, missed: bool) -> str:
    if fired:
        return "sent"
    if missed:
        return "missed"
    if starts_at > now:
        return "scheduled"
    if now - starts_at <= MISSED_AFTER:
        return "due"
    return "stale"


def display_status(starts_at: datetime, now: datetime, fired: bool, missed: bool, attempts: int) -> str:
    kind = classify_event(starts_at, now, fired, missed)
    if kind == "sent":
        return "Sent"
    if kind == "missed":
        return "Missed"
    if attempts and kind == "due":
        return "Retrying"
    if kind == "due":
        return "Due"
    if kind == "stale":
        return "Missed"
    return "Scheduled"


def avatar_is_due(
    weekday: int,
    hour: int,
    minute: int,
    last_run_on: str | None,
    created_at: datetime,
    now: datetime,
) -> bool:
    """Both datetimes must be timezone-aware. `now` is in the panel timezone."""
    if now.weekday() != weekday:
        return False
    if last_run_on == now.date().isoformat():
        return False
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False
    if last_run_on is None and created_at > scheduled:
        return False
    return True


def next_avatar_at(
    weekday: int,
    hour: int,
    minute: int,
    last_run_on: str | None,
    created_at: datetime,
    now: datetime,
) -> datetime:
    if avatar_is_due(weekday, hour, minute, last_run_on, created_at, now):
        return now
    for offset in range(0, 14):
        day = (now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if day.weekday() != weekday or day <= now:
            continue
        return day
    return (now + timedelta(days=7)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def clean_pool_slug(value: str) -> str:
    cleaned = value.strip().lower().replace(" ", "_")
    if not POOL_SLUG_RE.fullmatch(cleaned):
        raise ValueError("Use a short pool id: lowercase letters, numbers, underscores, and hyphens.")
    if cleaned == PROMPT_POOL_SLUG:
        raise ValueError("That id is reserved for the prompt pool.")
    return cleaned


def clean_pool_label(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned or len(cleaned) > 60:
        raise ValueError("Give the pool a name up to 60 characters.")
    return cleaned


def clean_fact_text(value: str) -> str:
    text = value.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("Write a fact.")
    if len(text) > 1800:
        raise ValueError("That fact is too long. Keep it under 1800 characters.")
    return text


def clean_interval_minutes(minutes: int, maximum: int) -> tuple[int, int]:
    if minutes < 1 or maximum < 1:
        raise ValueError("Intervals must be at least 1 minute.")
    if minutes > 60 * 24 * 14 or maximum > 60 * 24 * 14:
        raise ValueError("Intervals cannot be longer than 14 days.")
    if maximum < minutes:
        raise ValueError("The longest wait must be at least as long as the shortest.")
    return minutes, maximum


def clean_emoji_trigger(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 64:
        raise ValueError("Paste one emoji, or a custom emoji like <:name:123>.")
    if CUSTOM_EMOJI_RE.fullmatch(cleaned):
        return cleaned
    if len(cleaned) <= 8 and not cleaned.isspace():
        return cleaned
    raise ValueError("Paste one emoji, or a custom emoji like <:name:123>.")


RESERVED_SLASH_COMMANDS = frozenset({"countdown", "pictures", "prompt"})


def clean_slash_command_name(value: str, *, reserved: frozenset[str] | None = None) -> str:
    cleaned = value.strip().lower().replace(" ", "_")
    if not SLASH_NAME_RE.fullmatch(cleaned):
        raise ValueError("Use 1–32 lowercase letters, numbers, underscores, or hyphens.")
    blocked = RESERVED_SLASH_COMMANDS | (reserved or frozenset())
    if cleaned in blocked:
        raise ValueError("That name is used by another command. Pick a different one.")
    return cleaned


def clean_bot_username(value: str) -> str:
    cleaned = value.strip()
    if not BOT_USERNAME_RE.fullmatch(cleaned):
        raise ValueError("Usernames are 2–32 characters: letters, numbers, underscore, period.")
    if cleaned.startswith(".") or cleaned.endswith("."):
        raise ValueError("Usernames cannot start or end with a period.")
    return cleaned


def message_has_emoji(content: str, trigger: str) -> bool:
    if trigger.startswith("<") and trigger.endswith(">"):
        return trigger in content
    return trigger in content


def random_minutes_between(min_minutes: int, max_minutes: int) -> int:
    return random.randint(min_minutes, max_minutes)


def presence_name(name: str, starts_at: datetime, now: datetime) -> str:
    remaining = humanize_delta(starts_at - now)
    if remaining == "now":
        label = f"{name} now"
    else:
        label = f"{name} in {remaining}"
    return label[:128]
