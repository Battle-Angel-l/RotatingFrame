from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import aiohttp
from bs4 import BeautifulSoup

from .models import CodaWeaponBonus, RotationRow, RotationSnapshot

CIRCUIT_URL = "https://wiki.warframe.com/w/The_Circuit"
CODA_WEAPONS_URL = "https://wiki.warframe.com/w/Coda_Weapons"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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


class WikiClient:
    def __init__(self) -> None:
        self._timeout = aiohttp.ClientTimeout(total=25)
        self._headers = {"User-Agent": "WarframeRotationDiscordBot/1.0"}

    async def _get_soup(self, session: aiohttp.ClientSession, url: str) -> BeautifulSoup:
        async with session.get(url, headers=self._headers) as response:
            response.raise_for_status()
            html = await response.text()
        return BeautifulSoup(html, "html.parser")

    async def fetch_snapshot(self) -> RotationSnapshot:
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            circuit_soup = await self._get_soup(session, CIRCUIT_URL)
            coda_soup = await self._get_soup(session, CODA_WEAPONS_URL)

        now_utc = datetime.now(tz=timezone.utc)
        coda_anchor_utc = _parse_coda_anchor_utc(coda_soup)
        coda_batch_label = _parse_coda_batch_label(coda_soup)

        normal_table = _find_table_by_hint(circuit_soup, "Normal Circuit Warframe Rotation")
        steel_table = _find_table_by_hint(circuit_soup, "Steel Path Incarnon Genesis Reward Rotation")

        return RotationSnapshot(
            fetched_at_utc=now_utc,
            normal_rows=_parse_rotation_rows(normal_table, steel_path=False),
            steel_rows=_parse_rotation_rows(steel_table, steel_path=True),
            coda_bonus_rows=_parse_coda_bonus_rows(coda_soup, preferred_batch=coda_batch_label),
            coda_batch_label=coda_batch_label,
            coda_next_reset_utc=_next_coda_reset(now_utc, coda_anchor_utc),
        )
