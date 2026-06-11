from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class RotationRow:
    week_label: str
    items: list[str]


@dataclass(slots=True)
class CodaWeaponBonus:
    weapon: str
    bonus_text: str


@dataclass(slots=True)
class RotationSnapshot:
    fetched_at_utc: datetime
    normal_rows: list[RotationRow]
    steel_rows: list[RotationRow]
    coda_bonus_rows: list[CodaWeaponBonus]
    coda_batch_label: str | None
    coda_next_reset_utc: datetime | None
