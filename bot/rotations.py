from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .models import CodaWeaponBonus, RotationRow, RotationSnapshot


@dataclass(slots=True)
class RotationState:
    now_utc: datetime
    next_reset_utc: datetime
    time_until_reset: timedelta
    coda_next_reset_utc: datetime | None
    coda_time_until_reset: timedelta | None
    coda_batch_label: str | None
    normal_current: RotationRow
    steel_current: RotationRow
    normal_future: list[RotationRow]
    steel_future: list[RotationRow]
    coda_bonus_rows: list[CodaWeaponBonus]


def _next_weekly_reset(now_utc: datetime) -> datetime:
    monday_zero = datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc)
    days_until_next_monday = (7 - now_utc.weekday()) % 7
    candidate = monday_zero + timedelta(days=days_until_next_monday)
    if candidate <= now_utc:
        candidate += timedelta(days=7)
    return candidate


def _current_cycle_index(now_utc: datetime, epoch_week1: date, cycle_len: int) -> int:
    epoch_dt = datetime.combine(epoch_week1, time.min, tzinfo=timezone.utc)
    weeks = int((now_utc - epoch_dt).total_seconds() // (7 * 24 * 3600))
    if weeks < 0:
        weeks = 0
    return weeks % cycle_len


def _pick_current_and_future(
    rows: list[RotationRow], now_utc: datetime, epoch_week1: date, future_weeks: int
) -> tuple[RotationRow, list[RotationRow]]:
    if not rows:
        raise ValueError("Rotation rows are empty.")
    idx = _current_cycle_index(now_utc, epoch_week1, len(rows))
    current = rows[idx]
    future: list[RotationRow] = []
    for step in range(1, max(future_weeks, 0) + 1):
        future.append(rows[(idx + step) % len(rows)])
    return current, future


def build_rotation_state(
    snapshot: RotationSnapshot,
    normal_epoch: date,
    steel_epoch: date,
    coda_epoch: date,
    future_weeks: int = 6,
) -> RotationState:
    now_utc = datetime.now(tz=timezone.utc)
    next_reset_utc = _next_weekly_reset(now_utc)
    normal_current, normal_future = _pick_current_and_future(
        snapshot.normal_rows, now_utc, normal_epoch, future_weeks
    )
    steel_current, steel_future = _pick_current_and_future(
        snapshot.steel_rows, now_utc, steel_epoch, future_weeks
    )
    coda_next_reset_utc = snapshot.coda_next_reset_utc
    if coda_next_reset_utc is None:
        coda_anchor = datetime.combine(coda_epoch, time.min, tzinfo=timezone.utc)
        period = timedelta(days=4)
        if now_utc < coda_anchor:
            coda_next_reset_utc = coda_anchor
        else:
            steps = int((now_utc - coda_anchor) // period) + 1
            coda_next_reset_utc = coda_anchor + (period * steps)
    coda_time_until_reset: timedelta | None = None
    if coda_next_reset_utc is not None:
        coda_time_until_reset = coda_next_reset_utc - now_utc
    return RotationState(
        now_utc=now_utc,
        next_reset_utc=next_reset_utc,
        time_until_reset=next_reset_utc - now_utc,
        coda_next_reset_utc=coda_next_reset_utc,
        coda_time_until_reset=coda_time_until_reset,
        coda_batch_label=snapshot.coda_batch_label,
        normal_current=normal_current,
        steel_current=steel_current,
        normal_future=normal_future,
        steel_future=steel_future,
        coda_bonus_rows=snapshot.coda_bonus_rows,
    )


def format_remaining(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    return f"{days}d {hours}h {mins}m"
