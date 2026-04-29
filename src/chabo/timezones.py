"""Timezone aliases + parser used by the bot timezone-setup flow.

Extracted from bot.py to keep that module focused on Telegram update
handling. Pure data + small parser; no DB / settings dependency.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_USER_TIMEZONE = "Asia/Shanghai"


TIMEZONE_ALIASES: dict[str, str] = {
    "beijing": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "北京时间": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "中国": "Asia/Shanghai",
    "manila": "Asia/Manila",
    "马尼拉": "Asia/Manila",
    "philippines": "Asia/Manila",
    "菲律宾": "Asia/Manila",
    "hongkong": "Asia/Hong_Kong",
    "hong kong": "Asia/Hong_Kong",
    "香港": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "台北": "Asia/Taipei",
    "taiwan": "Asia/Taipei",
    "台湾": "Asia/Taipei",
    "singapore": "Asia/Singapore",
    "新加坡": "Asia/Singapore",
    "tokyo": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "首尔": "Asia/Seoul",
    "bangkok": "Asia/Bangkok",
    "曼谷": "Asia/Bangkok",
    "dubai": "Asia/Dubai",
    "迪拜": "Asia/Dubai",
    "rome": "Europe/Rome",
    "罗马": "Europe/Rome",
    "london": "Europe/London",
    "伦敦": "Europe/London",
    "new york": "America/New_York",
    "newyork": "America/New_York",
    "纽约": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "losangeles": "America/Los_Angeles",
    "洛杉矶": "America/Los_Angeles",
}


def resolve_timezone(raw_value: str) -> str | None:
    """Resolve a free-text input to a valid IANA tz name, or return None."""
    value = raw_value.strip()
    if not value:
        return None
    normalized = value.lower().replace("_", " ")
    aliased = TIMEZONE_ALIASES.get(normalized) or TIMEZONE_ALIASES.get(normalized.replace(" ", ""))
    timezone_name = aliased or value
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    return timezone_name


def format_timezone_now(timezone_name: str) -> str:
    """Render '<tz>(YYYY-MM-DD HH:MM)' for the current local time."""
    now = datetime.now(ZoneInfo(timezone_name))
    return f"{timezone_name}（{now.strftime('%Y-%m-%d %H:%M')}）"
