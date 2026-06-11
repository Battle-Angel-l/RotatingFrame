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

        tail_cells = cells[1:]
        merged = " ".join(_clean(c.get_text(" ", strip=True)).lower() for c in tail_cells)
        if "schedule repeats" in merged:
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
    return rows


def _parse_coda_bonus_rows(soup: BeautifulSoup) -> list[CodaWeaponBonus]:
    # Primary source: the "Current Valence Bonuses" table on Coda Weapons page.
    for table in soup.select("table.wikitable"):
        headers = [_clean(h.get_text(" ", strip=True)).lower() for h in table.select("tr th")]
        header_blob = " | ".join(headers)
        if "weapon (batch" in header_blob and "bonus %" in header_blob:
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
                return rows

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

        normal_table = _find_table_by_hint(circuit_soup, "Normal Circuit Warframe Rotation")
        steel_table = _find_table_by_hint(circuit_soup, "Steel Path Incarnon Genesis Reward Rotation")

        return RotationSnapshot(
            fetched_at_utc=now_utc,
            normal_rows=_parse_rotation_rows(normal_table, steel_path=False),
            steel_rows=_parse_rotation_rows(steel_table, steel_path=True),
            coda_bonus_rows=_parse_coda_bonus_rows(coda_soup),
            coda_batch_label=_parse_coda_batch_label(coda_soup),
            coda_next_reset_utc=_next_coda_reset(now_utc, coda_anchor_utc),
        )
