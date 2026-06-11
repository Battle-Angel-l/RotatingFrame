from __future__ import annotations

import io
import math
import textwrap
from dataclasses import dataclass

import aiohttp
from PIL import Image, ImageDraw, ImageFont

WIKI_API_URL = "https://wiki.warframe.com/api.php"


def _sanitize_title(name: str) -> str:
    title = name.strip()
    if "," in title:
        title = title.split(",", 1)[0].strip()
    title = title.replace(" Incarnon Genesis", "").strip()
    return title


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass(slots=True)
class PortraitAssets:
    normal_png: bytes | None
    steel_png: bytes | None
    coda_png: bytes | None


class PortraitRenderer:
    def __init__(self) -> None:
        self._timeout = aiohttp.ClientTimeout(total=25)
        self._headers = {"User-Agent": "WarframeRotationDiscordBot/1.0"}
        self._thumb_url_cache: dict[str, str | None] = {}
        self._image_cache: dict[str, bytes] = {}
        self._font = ImageFont.load_default()

    async def _fetch_thumb_url(self, session: aiohttp.ClientSession, title: str) -> str | None:
        cache_key = title.lower()
        if cache_key in self._thumb_url_cache:
            return self._thumb_url_cache[cache_key]

        params = {
            "action": "query",
            "titles": title,
            "prop": "pageimages",
            "pithumbsize": "256",
            "format": "json",
        }
        try:
            async with session.get(WIKI_API_URL, params=params, headers=self._headers) as response:
                response.raise_for_status()
                payload = await response.json()
        except Exception:
            self._thumb_url_cache[cache_key] = None
            return None

        pages = payload.get("query", {}).get("pages", {})
        for page in pages.values():
            source = page.get("thumbnail", {}).get("source")
            if source:
                self._thumb_url_cache[cache_key] = source
                return source

        self._thumb_url_cache[cache_key] = None
        return None

    async def _fetch_image(self, session: aiohttp.ClientSession, title: str) -> bytes | None:
        cache_key = title.lower()
        if cache_key in self._image_cache:
            return self._image_cache[cache_key]

        thumb_url = await self._fetch_thumb_url(session, title)
        if not thumb_url:
            return None
        try:
            async with session.get(thumb_url, headers=self._headers) as response:
                response.raise_for_status()
                content = await response.read()
        except Exception:
            return None

        self._image_cache[cache_key] = content
        return content

    def _draw_placeholder(self, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
        x0, y0, x1, y1 = box
        draw.rectangle(box, fill=(32, 38, 48), outline=(74, 86, 104), width=2)
        draw.text((x0 + 10, y0 + ((y1 - y0) // 2) - 8), "No Image", fill=(170, 180, 200), font=self._font)

    def _render_section(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        title: str,
        items: list[tuple[str, bytes | None]],
        y_start: int,
    ) -> int:
        draw.text((24, y_start), title, fill=(240, 245, 255), font=self._font)
        y = y_start + 24
        card_w = 150
        card_h = 200
        image_box = 120
        gap_x = 16
        gap_y = 20
        cols = 5

        for row_idx, row in enumerate(_chunk(items, cols)):
            for col_idx, (label, image_bytes) in enumerate(row):
                x = 24 + col_idx * (card_w + gap_x)
                cy = y + row_idx * (card_h + gap_y)
                draw.rounded_rectangle((x, cy, x + card_w, cy + card_h), radius=10, fill=(24, 30, 40), outline=(58, 70, 90))
                ix0 = x + 15
                iy0 = cy + 12
                ix1 = ix0 + image_box
                iy1 = iy0 + image_box

                if image_bytes:
                    try:
                        src = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                        src.thumbnail((image_box, image_box))
                        px = ix0 + (image_box - src.width) // 2
                        py = iy0 + (image_box - src.height) // 2
                        canvas.paste(src, (px, py))
                        draw.rectangle((ix0, iy0, ix1, iy1), outline=(90, 105, 130), width=1)
                    except Exception:
                        self._draw_placeholder(draw, (ix0, iy0, ix1, iy1))
                else:
                    self._draw_placeholder(draw, (ix0, iy0, ix1, iy1))

                text_lines = textwrap.wrap(label, width=17)[:2]
                text_y = iy1 + 10
                for line in text_lines:
                    draw.text((x + 10, text_y), line, fill=(222, 228, 240), font=self._font)
                    text_y += 13

        rows = math.ceil(len(items) / cols) if items else 0
        return y + rows * (card_h + gap_y)

    @staticmethod
    def _section_height(item_count: int) -> int:
        # title + spacing + rows of cards
        card_h = 200
        gap_y = 20
        rows = max(math.ceil(item_count / 5), 1)
        return 24 + rows * (card_h + gap_y)

    async def build_portrait_assets(
        self,
        normal_items: list[str],
        steel_items: list[str],
        coda_items: list[str],
    ) -> PortraitAssets:
        normal_titles = [_sanitize_title(x) for x in normal_items]
        steel_titles = [_sanitize_title(x) for x in steel_items]
        coda_titles = [_sanitize_title(x) for x in coda_items]

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async def collect(titles: list[str]) -> list[tuple[str, bytes | None]]:
                out: list[tuple[str, bytes | None]] = []
                for t in titles:
                    out.append((t, await self._fetch_image(session, t)))
                return out

            normal_data = await collect(normal_titles)
            steel_data = await collect(steel_titles)
            coda_data = await collect(coda_titles)

        normal_png = self._render_single_section("Circuit Normal", normal_data)
        steel_png = self._render_single_section("Circuit Steel Path", steel_data)
        coda_png = self._render_coda(coda_data)
        return PortraitAssets(normal_png=normal_png, steel_png=steel_png, coda_png=coda_png)

    def _render_overview(self, normal_data: list[tuple[str, bytes | None]], steel_data: list[tuple[str, bytes | None]]) -> bytes | None:
        width = 900
        top = 18
        between_sections = 8
        bottom = 24
        normal_h = self._section_height(len(normal_data))
        steel_h = self._section_height(len(steel_data))
        height = top + normal_h + between_sections + steel_h + bottom
        img = Image.new("RGB", (width, height), (16, 20, 28))
        draw = ImageDraw.Draw(img)
        y = self._render_section(img, draw, "Circuit Normal", normal_data, y_start=top)
        self._render_section(img, draw, "Circuit Steel Path", steel_data, y_start=y + 8)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _render_single_section(self, title: str, items: list[tuple[str, bytes | None]]) -> bytes | None:
        width = 900
        top = 18
        bottom = 24
        height = top + self._section_height(len(items)) + bottom
        img = Image.new("RGB", (width, height), (16, 20, 28))
        draw = ImageDraw.Draw(img)
        self._render_section(img, draw, title, items, y_start=top)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _render_coda(self, coda_data: list[tuple[str, bytes | None]]) -> bytes | None:
        if not coda_data:
            return None
        width = 900
        rows = math.ceil(len(coda_data) / 5)
        height = 70 + rows * 220
        img = Image.new("RGB", (width, height), (16, 20, 28))
        draw = ImageDraw.Draw(img)
        self._render_section(img, draw, "Coda Weapons", coda_data, y_start=18)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
