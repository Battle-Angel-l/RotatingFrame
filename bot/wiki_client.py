from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import aiohttp
from bs4 import BeautifulSoup

from .models import CodaWeaponBonus, LotusGift, RotationRow, RotationSnapshot

CIRCUIT_URL = "https://wiki.warframe.com/w/The_Circuit"
CODA_WEAPONS_URL = "https://wiki.warframe.com/w/Coda_Weapons"
LOTUS_ALERTS_URL = "https://hub.warframe.us/pc/alerts"
LOTUS_ALERTS_FALLBACK_URL = "https://api.warframestat.us/pc/alerts"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _short_item_name(raw: str) -> str:
    text = _clean(raw)
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = text.replace("_", " ")
    return text


def _extract_list_from_cell(cell) -> list[str]:
    items: list[str] = []
    for link in cell.select("a"):
        txt = _clean(link.get_text(" ", strip=True))
        if txt and txt not in items:
            items.append(txt)
    if items:
        return items
    text = _clean(cell.get_text(" ", strip=True))
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _extract_first_link_or_text(cell) -> str | None:
    first_link = cell.select_one("a")
    if first_link is not None:
        label = _clean(first_link.get_text(" ", strip=True))
        if label:
            return label
    text = _clean(cell.get_text(" ", strip=True))
    return text or None


def _find_table_by_hint(soup: BeautifulSoup, hint: str):
    hint_lc = hint.lower()
    for table in soup.select("table.wikitable"):
        text = _clean(table.get_text(" ", strip=True)).lower()
        if hint_lc in text:
            return table
    return None


def _parse_rotation_rows(table, *, steel_path: bool = False) -> list[RotationRow]:
    rows: list[RotationRow] = []
    if table is None:
        return rows

    for tr in table.select("tr")[1:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        week_label = _clean(cells[0].get_text(" ", strip=True))
        if not week_label.lower().startswith("week"):
            continue
        week_num_match = re.search(r"week\s*(\d+)", week_label, flags=re.IGNORECASE)
        if not week_num_match:
            continue
        week_num = int(week_num_match.group(1))

        tail_cells = cells[1:]
        merged = " ".join(_clean(c.get_text(" ", strip=True)).lower() for c in tail_cells)
        if "schedule repeats" in merged:
            continue
        if steel_path:
            # Steel Path Circuit rotation is an 8-week cycle (A-H).
            if not (1 <= week_num <= 8):
                continue
        else:
            # Normal Circuit reward list currently has 11 concrete weeks.
            if not (1 <= week_num <= 11):
                continue

        deduped: list[str] = []
        if steel_path:
            items: list[str] = []
            for cell in tail_cells:
                items.extend(_extract_list_from_cell(cell))
            for item in items:
                if item not in deduped and not item.lower().startswith("week"):
                    deduped.append(item)
        else:
            # Each normal row has one reward cell per Warframe slot; each slot can
            # contain extra links (e.g. augment mods). Keep only the first link.
            for cell in tail_cells:
                first_item = _extract_first_link_or_text(cell)
                if not first_item or first_item.lower().startswith("week"):
                    continue
                if first_item not in deduped:
                    deduped.append(first_item)

        if steel_path:
            # In the Steel Path table, variant links (e.g. Prime/Vandal) can appear
            # in addition to the real weekly offerings. Keep only Incarnon entries.
            incarnons = [name for name in deduped if "incarnon genesis" in name.lower()]
            if incarnons:
                deduped = incarnons[:5]
            elif len(deduped) > 5:
                blacklist = {"prime", "vandal", "dex", "wraith", "prisma"}
                filtered = [name for name in deduped if name.strip().lower() not in blacklist]
                deduped = (filtered or deduped)[:5]
        else:
            deduped = deduped[:3]

        if deduped:
            rows.append(RotationRow(week_label=week_label, items=deduped))
    rows.sort(
        key=lambda row: int(re.search(r"week\s*(\d+)", row.week_label, flags=re.IGNORECASE).group(1))
    )
    return rows


def _parse_coda_bonus_rows(soup: BeautifulSoup, preferred_batch: str | None = None) -> list[CodaWeaponBonus]:
    # Primary source: "Current Valence Bonuses" tables. Some page renders may
    # include multiple batch variants; prefer the active batch when available.
    rows_by_batch: dict[str, list[CodaWeaponBonus]] = {}
    for table in soup.select("table.wikitable"):
        header_cells = [_clean(h.get_text(" ", strip=True)) for h in table.select("tr th")]
        header_blob = " | ".join(h.lower() for h in header_cells)
        if "weapon (batch" in header_blob and "bonus %" in header_blob:
            batch_match = re.search(r"weapon\s*\(batch\s*([a-z])\)", " ".join(header_cells), flags=re.IGNORECASE)
            batch = batch_match.group(1).upper() if batch_match else "?"

            rows: list[CodaWeaponBonus] = []
            for tr in table.select("tr")[1:]:
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                weapon = _clean(cells[0].get_text(" ", strip=True))
                if not weapon:
                    continue
                bonus_blob = " ".join(_clean(c.get_text(" ", strip=True)) for c in cells[1:])
                bonus_match = re.search(r"\d+(?:\.\d+)?\s*%", bonus_blob)
                if not bonus_match:
                    continue
                rows.append(CodaWeaponBonus(weapon=weapon, bonus_text=bonus_match.group(0)))
            if rows:
                rows_by_batch[batch] = rows

    if preferred_batch and preferred_batch in rows_by_batch:
        return rows_by_batch[preferred_batch]
    if rows_by_batch:
        return next(iter(rows_by_batch.values()))

    # Fallback parser for alternative layouts.
    results: list[CodaWeaponBonus] = []
    for table in soup.select("table.wikitable"):
        table_text = _clean(table.get_text(" ", strip=True)).lower()
        if "weapon" not in table_text or "%" not in table_text:
            continue

        for tr in table.select("tr")[1:]:
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            weapon = _clean(cells[0].get_text(" ", strip=True))
            if not weapon:
                continue
            joined = " ".join(_clean(c.get_text(" ", strip=True)) for c in cells[1:])
            if "%" not in joined:
                continue
            bonus_match = re.search(r"(\d+\s*%\s*[-–]\s*\d+\s*%|\d+\s*%)", joined)
            bonus_text = bonus_match.group(1) if bonus_match else joined
            results.append(CodaWeaponBonus(weapon=weapon, bonus_text=_clean(bonus_text)))

    deduped: list[CodaWeaponBonus] = []
    seen: set[str] = set()
    for row in results:
        key = row.weapon.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _parse_coda_batch_label(soup: BeautifulSoup) -> str | None:
    # The page often contains both A/B variants in static HTML; the active
    # variant is typically the last "currently selling" status sentence.
    text = _clean(soup.get_text(" ", strip=True))
    status_matches = re.findall(
        r"Eleanor is currently selling Batch\s*([A-Z])\s*weapons\.\s*Time left until Batch\s*([A-Z])\s*:",
        text,
        flags=re.IGNORECASE,
    )
    if status_matches:
        current_batch, _next_batch = status_matches[-1]
        return current_batch.upper()

    for table in soup.select("table.wikitable"):
        headers = [_clean(h.get_text(" ", strip=True)) for h in table.select("tr th")]
        for header in headers:
            match = re.search(r"Weapon\s*\(Batch\s*([A-Z])\)", header, flags=re.IGNORECASE)
            if match:
                return match.group(1).upper()
    return None


def _parse_coda_anchor_utc(soup: BeautifulSoup) -> datetime | None:
    text = _clean(soup.get_text(" ", strip=True))
    match = re.search(
        r"([A-Za-z]+\s+\d{1,2},\s+\d{4}\s+0:00:00\s+UTC)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        anchor = datetime.strptime(match.group(1), "%B %d, %Y %H:%M:%S UTC")
        return anchor.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _next_coda_reset(now_utc: datetime, anchor_utc: datetime | None) -> datetime | None:
    if anchor_utc is None:
        return None
    period = timedelta(days=4)
    if now_utc < anchor_utc:
        return anchor_utc
    steps = int((now_utc - anchor_utc) // period) + 1
    return anchor_utc + (period * steps)


def _current_coda_batch_from_anchor(now_utc: datetime, anchor_utc: datetime | None) -> str | None:
    """Derive active A/B batch by 4-day alternation from known anchor.

    Wiki text often renders both A and B status strings at once, so direct
    string parsing can be ambiguous. This uses deterministic cycle math.
    """
    if anchor_utc is None:
        return None
    period = timedelta(days=4)
    if now_utc < anchor_utc:
        return "A"
    intervals = int((now_utc - anchor_utc) // period)
    return "A" if intervals % 2 == 0 else "B"


def _parse_datetime_any(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        if txt.isdigit():
            return _parse_datetime_any(int(txt))
        try:
            return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    if isinstance(value, dict):
        # Older worldstate schema variants
        nested = value.get("$date") if isinstance(value.get("$date"), (str, int, float, dict)) else None
        if nested is not None:
            return _parse_datetime_any(nested)
        nested_long = value.get("$numberLong")
        if nested_long is not None:
            return _parse_datetime_any(nested_long)
    return None


def _extract_reward_lines(alert: dict) -> list[str]:
    reward = alert.get("reward") or alert.get("missionReward") or alert.get("MissionReward") or {}
    lines: list[str] = []

    as_string = reward.get("asString") if isinstance(reward, dict) else None
    if isinstance(as_string, str) and as_string.strip():
        return [_clean(as_string)]

    if isinstance(reward, dict):
        credits = reward.get("credits")
        if isinstance(credits, (int, float)) and credits > 0:
            lines.append(f"{int(credits):,} Credits")

        for key in ("items", "countedItems"):
            raw_items = reward.get(key)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if isinstance(item, str):
                    lines.append(_short_item_name(item))
                    continue
                if not isinstance(item, dict):
                    continue
                name = (
                    item.get("name")
                    or item.get("itemType")
                    or item.get("ItemType")
                    or item.get("type")
                    or item.get("Type")
                )
                count = item.get("count") or item.get("ItemCount") or item.get("quantity") or item.get("counted")
                label = _short_item_name(str(name)) if name else "Reward Item"
                if isinstance(count, (int, float)) and int(count) > 1:
                    label = f"{int(count)}x {label}"
                lines.append(label)

    deduped: list[str] = []
    for line in lines:
        if line and line not in deduped:
            deduped.append(line)
    return deduped


def _extract_mission_text(alert: dict) -> str:
    mission = alert.get("mission") or alert.get("MissionInfo") or {}
    node = (
        alert.get("location")
        or mission.get("node")
        or mission.get("location")
        or alert.get("Node")
        or "Unknown Node"
    )
    mission_type = mission.get("type") or mission.get("missionType") or alert.get("missionType") or "Mission"
    min_lvl = mission.get("minEnemyLevel") or alert.get("minEnemyLevel")
    max_lvl = mission.get("maxEnemyLevel") or alert.get("maxEnemyLevel")
    if isinstance(min_lvl, (int, float)) and isinstance(max_lvl, (int, float)):
        return f"{node} - {mission_type} ({int(min_lvl)}-{int(max_lvl)})"
    return f"{node} - {mission_type}"


def _parse_lotus_gifts(alerts_payload) -> list[LotusGift]:
    alerts: list[dict] = []
    if isinstance(alerts_payload, list):
        alerts = [x for x in alerts_payload if isinstance(x, dict)]
    elif isinstance(alerts_payload, dict):
        nested = alerts_payload.get("alerts")
        if isinstance(nested, dict):
            data = nested.get("data")
            if isinstance(data, list):
                alerts = [x for x in data if isinstance(x, dict)]
        elif isinstance(nested, list):
            alerts = [x for x in nested if isinstance(x, dict)]

    gifts: list[LotusGift] = []
    for alert in alerts:
        tag = (alert.get("tag") or alert.get("Tag") or "").strip()
        mission_blob = _clean(str(alert.get("MissionInfo", "")))
        if "lotusgift" not in tag.lower() and "lotusgift" not in mission_blob.lower():
            continue
        expires = _parse_datetime_any(alert.get("expiry") or alert.get("Expiry") or alert.get("end"))
        gifts.append(
            LotusGift(
                mission=_extract_mission_text(alert),
                rewards=_extract_reward_lines(alert),
                expires_at_utc=expires,
            )
        )

    gifts.sort(key=lambda g: g.expires_at_utc.timestamp() if g.expires_at_utc else 0)
    return gifts


class WikiClient:
    def __init__(self) -> None:
        self._timeout = aiohttp.ClientTimeout(total=25)
        self._headers = {"User-Agent": "WarframeRotationDiscordBot/1.0"}

    async def _get_soup(self, session: aiohttp.ClientSession, url: str) -> BeautifulSoup:
        async with session.get(url, headers=self._headers) as response:
            response.raise_for_status()
            html = await response.text()
        return BeautifulSoup(html, "html.parser")

    async def _get_json(self, session: aiohttp.ClientSession, url: str):
        async with session.get(url, headers=self._headers) as response:
            response.raise_for_status()
            return await response.json()

    async def fetch_snapshot(self) -> RotationSnapshot:
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            circuit_soup = await self._get_soup(session, CIRCUIT_URL)
            coda_soup = await self._get_soup(session, CODA_WEAPONS_URL)
            lotus_payload = None
            try:
                lotus_payload = await self._get_json(session, LOTUS_ALERTS_URL)
            except Exception:
                try:
                    lotus_payload = await self._get_json(session, LOTUS_ALERTS_FALLBACK_URL)
                except Exception:
                    lotus_payload = []

        now_utc = datetime.now(tz=timezone.utc)
        coda_anchor_utc = _parse_coda_anchor_utc(coda_soup)
        coda_batch_label = _current_coda_batch_from_anchor(now_utc, coda_anchor_utc) or _parse_coda_batch_label(
            coda_soup
        )

        normal_table = _find_table_by_hint(circuit_soup, "Normal Circuit Warframe Rotation")
        steel_table = _find_table_by_hint(circuit_soup, "Steel Path Incarnon Genesis Reward Rotation")

        return RotationSnapshot(
            fetched_at_utc=now_utc,
            normal_rows=_parse_rotation_rows(normal_table, steel_path=False),
            steel_rows=_parse_rotation_rows(steel_table, steel_path=True),
            coda_bonus_rows=_parse_coda_bonus_rows(coda_soup, preferred_batch=coda_batch_label),
            coda_batch_label=coda_batch_label,
            coda_next_reset_utc=_next_coda_reset(now_utc, coda_anchor_utc),
            lotus_gifts=_parse_lotus_gifts(lotus_payload),
        )
