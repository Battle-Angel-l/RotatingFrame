from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date


def _read_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_date(name: str, fallback: date) -> date:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback


@dataclass(slots=True)
class Settings:
    token: str
    guild_id: int | None
    rotations_channel_id: int | None
    rotations_message_id: int | None
    update_interval_minutes: int
    circuit_normal_epoch: date
    circuit_steel_epoch: date
    coda_epoch: date


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required.")

    return Settings(
        token=token,
        guild_id=_read_int("DISCORD_GUILD_ID"),
        rotations_channel_id=_read_int("ROTATIONS_CHANNEL_ID"),
        rotations_message_id=_read_int("ROTATIONS_MESSAGE_ID"),
        update_interval_minutes=max(_read_int("UPDATE_INTERVAL_MINUTES", 5) or 5, 1),
        circuit_normal_epoch=_read_date("CIRCUIT_NORMAL_EPOCH", date(2023, 5, 1)),
        circuit_steel_epoch=_read_date("CIRCUIT_STEEL_EPOCH", date(2023, 6, 5)),
        coda_epoch=_read_date("CODA_EPOCH", date(2025, 3, 18)),
    )
