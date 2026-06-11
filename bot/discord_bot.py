from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.errors import Forbidden

from .config import Settings
from .rotations import RotationState, build_rotation_state, format_remaining
from .wiki_client import WikiClient


@dataclass(slots=True)
class PublishState:
    channel_id: int | None
    message_id: int | None


def _state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "state.json"


def _load_publish_state(default_channel_id: int | None, default_message_id: int | None) -> PublishState:
    path = _state_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PublishState(
                channel_id=int(data.get("channel_id")) if data.get("channel_id") else default_channel_id,
                message_id=int(data.get("message_id")) if data.get("message_id") else default_message_id,
            )
        except (ValueError, json.JSONDecodeError):
            pass
    return PublishState(channel_id=default_channel_id, message_id=default_message_id)


def _save_publish_state(state: PublishState) -> None:
    payload = {"channel_id": state.channel_id, "message_id": state.message_id}
    _state_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _chunk(lines: list[str], size: int) -> list[list[str]]:
    return [lines[i : i + size] for i in range(0, len(lines), size)]


def _build_embeds(state: RotationState) -> list[discord.Embed]:
    coda_lines: list[str] = []
    if state.coda_batch_label:
        coda_lines.append(f"Coda batch: **{state.coda_batch_label}**")
    if state.coda_next_reset_utc:
        coda_lines.append(f"Coda next reset: <t:{int(state.coda_next_reset_utc.timestamp())}:F>")
    if state.coda_time_until_reset is not None:
        coda_lines.append(f"Coda time remaining: **{format_remaining(state.coda_time_until_reset)}**")
    coda_block = "\n".join(coda_lines)

    overview = discord.Embed(
        title="Warframe Rotations",
        description=(
            f"Next weekly reset: <t:{int(state.next_reset_utc.timestamp())}:F>\n"
            f"Time remaining: **{format_remaining(state.time_until_reset)}**"
        ),
        color=discord.Color.blue(),
    )
    overview.set_footer(text="Source: wiki.warframe.com")

    normal_embed = discord.Embed(
        title=f"Circuit Normal ({state.normal_current.week_label})",
        description="\n".join(f"- {item}" for item in state.normal_current.items)[:4096] or "No data",
        color=discord.Color.blurple(),
    )
    steel_embed = discord.Embed(
        title=f"Circuit Steel Path ({state.steel_current.week_label})",
        description="\n".join(f"- {item}" for item in state.steel_current.items)[:4096] or "No data",
        color=discord.Color.blurple(),
    )
    normal_embed.set_footer(text="Source: wiki.warframe.com")
    steel_embed.set_footer(text="Source: wiki.warframe.com")

    embeds = [overview, normal_embed, steel_embed]

    if state.coda_bonus_rows:
        lines = [f"- {row.weapon}: {row.bonus_text}" for row in state.coda_bonus_rows]
        for idx, block in enumerate(_chunk(lines, 20), start=1):
            description_parts: list[str] = []
            if idx == 1 and coda_block:
                description_parts.append(coda_block)
            description_parts.append("\n".join(block)[:4096])
            embed = discord.Embed(
                title=f"Coda Weapon Bonus Values ({idx})",
                description="\n\n".join(description_parts)[:4096],
                color=discord.Color.dark_teal(),
            )
            embed.set_footer(text="Source: wiki.warframe.com")
            embeds.append(embed)
    return embeds


def _build_future_embed(state: RotationState, weeks: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"Future Rotations ({weeks} weeks)",
        description="Weekly reset happens Monday 00:00 UTC.",
        color=discord.Color.dark_purple(),
    )
    normal_lines = [f"{idx}. {row.week_label}: {', '.join(row.items)}" for idx, row in enumerate(state.normal_future, 1)]
    steel_lines = [f"{idx}. {row.week_label}: {', '.join(row.items)}" for idx, row in enumerate(state.steel_future, 1)]
    embed.add_field(
        name="Circuit Normal",
        value=("\n".join(normal_lines)[:1024] if normal_lines else "No data"),
        inline=False,
    )
    embed.add_field(
        name="Circuit Steel Path",
        value=("\n".join(steel_lines)[:1024] if steel_lines else "No data"),
        inline=False,
    )
    embed.set_footer(text="Source: wiki.warframe.com")
    return embed


class WarframeRotationBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.wiki = WikiClient()
        self.publish_state = _load_publish_state(
            default_channel_id=settings.rotations_channel_id,
            default_message_id=settings.rotations_message_id,
        )

    async def setup_hook(self) -> None:
        self._register_commands()
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except Forbidden:
                # Keep the bot online even if the configured guild ID is invalid
                # or the bot lacks access to that guild. Global sync can still work.
                await self.tree.sync()
            else:
                # Remove stale global command copies to avoid duplicate entries
                # in command menus when both global and guild commands exist.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
        else:
            await self.tree.sync()
        self.auto_publish.start()

    def _register_commands(self) -> None:
        @self.tree.command(name="rotations", description="Show current Warframe rotations and timers.")
        async def rotations(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            state = await self._compute_state(future_weeks=6)
            embeds = _build_embeds(state)
            await interaction.followup.send(embeds=embeds)

        @self.tree.command(name="future", description="Show future Circuit rotations.")
        @app_commands.describe(weeks="How many upcoming weeks to show (1-12).")
        async def future(interaction: discord.Interaction, weeks: app_commands.Range[int, 1, 12] = 6) -> None:
            await interaction.response.defer(thinking=True)
            state = await self._compute_state(future_weeks=weeks)
            await interaction.followup.send(embed=_build_future_embed(state, weeks))

        @self.tree.command(name="setup_channel", description="Create and register a dedicated rotations channel.")
        @app_commands.describe(name="Text channel name (default: warframe-rotations).")
        async def setup_channel(
            interaction: discord.Interaction, name: app_commands.Range[str, 1, 90] = "warframe-rotations"
        ) -> None:
            if not interaction.guild:
                await interaction.response.send_message("Run this command in a server.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            channel = await interaction.guild.create_text_channel(name=name)
            self.publish_state.channel_id = channel.id
            self.publish_state.message_id = None
            _save_publish_state(self.publish_state)
            await interaction.followup.send(f"Created channel {channel.mention} and set it as rotation channel.", ephemeral=True)
            await self._publish_to_configured_channel()

        @self.tree.command(name="set_channel", description="Set current channel as the bot's rotation channel.")
        async def set_channel(interaction: discord.Interaction) -> None:
            if not interaction.channel_id:
                await interaction.response.send_message("Channel not available in this context.", ephemeral=True)
                return
            self.publish_state.channel_id = interaction.channel_id
            self.publish_state.message_id = None
            _save_publish_state(self.publish_state)
            await interaction.response.send_message("This channel is now the auto-rotation channel.", ephemeral=True)
            await self._publish_to_configured_channel()

        @self.tree.command(name="refresh_rotations", description="Force refresh the auto-posted rotation message now.")
        async def refresh_rotations(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._publish_to_configured_channel()
            await interaction.followup.send("Rotation message refreshed.", ephemeral=True)

    async def _compute_state(self, future_weeks: int) -> RotationState:
        snapshot = await self.wiki.fetch_snapshot()
        return build_rotation_state(
            snapshot=snapshot,
            normal_epoch=self.settings.circuit_normal_epoch,
            steel_epoch=self.settings.circuit_steel_epoch,
            coda_epoch=self.settings.coda_epoch,
            future_weeks=future_weeks,
        )

    async def _publish_to_configured_channel(self) -> None:
        channel_id = self.publish_state.channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.abc.Messageable):
            return

        try:
            state = await self._compute_state(future_weeks=6)
            embeds = _build_embeds(state)
        except Exception:
            return

        if self.publish_state.message_id:
            try:
                message = await channel.fetch_message(self.publish_state.message_id)  # type: ignore[attr-defined]
                await message.edit(content="Auto-updated Warframe rotations", embeds=embeds, attachments=[])
                return
            except Exception:
                self.publish_state.message_id = None

        sent = await channel.send(content="Auto-updated Warframe rotations", embeds=embeds)  # type: ignore[attr-defined]
        self.publish_state.message_id = sent.id
        _save_publish_state(self.publish_state)

    @tasks.loop(minutes=1)
    async def auto_publish(self) -> None:
        if self.settings.update_interval_minutes <= 1:
            await self._publish_to_configured_channel()
            return
        minute_now = discord.utils.utcnow().minute
        if minute_now % self.settings.update_interval_minutes == 0:
            await self._publish_to_configured_channel()

    @auto_publish.before_loop
    async def before_publish(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(3)
