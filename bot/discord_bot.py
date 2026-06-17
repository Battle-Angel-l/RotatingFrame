from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from discord.errors import Forbidden, HTTPException

from .config import Settings
from .rotations import RotationState, build_rotation_state
from .wiki_client import WikiClient

logger = logging.getLogger("warframe-bot.commands")


@dataclass(slots=True)
class PublishState:
    channel_id: int | None
    message_id: int | None
    last_weekly_reset_ts: int | None
    last_coda_reset_ts: int | None


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
                last_weekly_reset_ts=int(data.get("last_weekly_reset_ts"))
                if data.get("last_weekly_reset_ts")
                else None,
                last_coda_reset_ts=int(data.get("last_coda_reset_ts")) if data.get("last_coda_reset_ts") else None,
            )
        except (ValueError, json.JSONDecodeError):
            pass
    return PublishState(
        channel_id=default_channel_id,
        message_id=default_message_id,
        last_weekly_reset_ts=None,
        last_coda_reset_ts=None,
    )


def _save_publish_state(state: PublishState) -> None:
    payload = {
        "channel_id": state.channel_id,
        "message_id": state.message_id,
        "last_weekly_reset_ts": state.last_weekly_reset_ts,
        "last_coda_reset_ts": state.last_coda_reset_ts,
    }
    _state_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _chunk(lines: list[str], size: int) -> list[list[str]]:
    return [lines[i : i + size] for i in range(0, len(lines), size)]


def _next_weekly_reset_utc(now_utc: datetime) -> datetime:
    monday_zero = datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc)
    days_until_next_monday = (7 - now_utc.weekday()) % 7
    candidate = monday_zero + timedelta(days=days_until_next_monday)
    if candidate <= now_utc:
        candidate += timedelta(days=7)
    return candidate


def _next_coda_reset_utc(now_utc: datetime, coda_epoch_date) -> datetime:
    coda_anchor = datetime.combine(coda_epoch_date, time.min, tzinfo=timezone.utc)
    period = timedelta(days=4)
    if now_utc < coda_anchor:
        return coda_anchor
    steps = int((now_utc - coda_anchor) // period) + 1
    return coda_anchor + (period * steps)


def _build_embeds(state: RotationState) -> list[discord.Embed]:
    weekly_reset_ts = int(state.next_reset_utc.timestamp())
    coda_lines: list[str] = []
    if state.coda_batch_label:
        coda_lines.append(f"Current coda rotation: **Batch {state.coda_batch_label}**")
    if state.coda_next_reset_utc:
        coda_reset_ts = int(state.coda_next_reset_utc.timestamp())
        coda_lines.append(f"Coda next reset: <t:{coda_reset_ts}:F>")
        coda_lines.append(f"Coda time remaining: **<t:{coda_reset_ts}:R>**")
    coda_block = "\n".join(coda_lines)

    overview = discord.Embed(
        title="Warframe Rotations",
        description=(
            f"Next weekly reset: <t:{weekly_reset_ts}:F>\n"
            f"Time remaining: **<t:{weekly_reset_ts}:R>**"
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

    alerts_embed = discord.Embed(
        title="Alerts",
        color=discord.Color.gold(),
    )
    if state.alerts:
        lines: list[str] = []
        max_alerts = 8
        for alert in state.alerts[:max_alerts]:
            marker = " [Gift from the Lotus]" if alert.is_lotus_gift else ""
            reward_text = ", ".join(alert.rewards) if alert.rewards else "Unknown reward"
            if alert.expires_at_utc:
                expires_ts = int(alert.expires_at_utc.timestamp())
                lines.append(
                    f"- **{alert.mission}**{marker}\n"
                    f"  Reward: {reward_text}\n"
                    f"  Expires: <t:{expires_ts}:F> (<t:{expires_ts}:R>)"
                )
            else:
                lines.append(f"- **{alert.mission}**{marker}\n  Reward: {reward_text}")
        if len(state.alerts) > max_alerts:
            lines.append(f"...and {len(state.alerts) - max_alerts} more alerts.")
        alerts_embed.description = "\n\n".join(lines)[:4096]
    else:
        alerts_embed.description = "No active alerts right now."
    alerts_embed.set_footer(text="Source: hub.warframe.us")
    embeds.append(alerts_embed)

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
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            logger.exception("App command failed: %s", error)
            message = f"Command failed: {error.__class__.__name__}. Check Railway logs."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                logger.exception("Failed to send app command error response")

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
            self.publish_state.last_weekly_reset_ts = None
            self.publish_state.last_coda_reset_ts = None
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
            self.publish_state.last_weekly_reset_ts = None
            self.publish_state.last_coda_reset_ts = None
            _save_publish_state(self.publish_state)
            await interaction.response.send_message("This channel is now the auto-rotation channel.", ephemeral=True)
            await self._publish_to_configured_channel()

        @self.tree.command(name="refresh_rotations", description="Force refresh the auto-posted rotation message now.")
        async def refresh_rotations(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.publish_state.channel_id is None and interaction.channel_id is not None:
                self.publish_state.channel_id = interaction.channel_id
                self.publish_state.message_id = None
                self.publish_state.last_weekly_reset_ts = None
                self.publish_state.last_coda_reset_ts = None
                _save_publish_state(self.publish_state)

            ok, reason = await self._publish_to_configured_channel()
            if ok:
                await interaction.followup.send("Rotation message refreshed.", ephemeral=True)
            else:
                if reason == "no_channel":
                    await interaction.followup.send(
                        "No auto channel is configured yet. Run /set_channel in the target channel first.",
                        ephemeral=True,
                    )
                elif reason == "channel_unavailable":
                    await interaction.followup.send(
                        "Configured channel is unavailable. Run /set_channel again in the target channel.",
                        ephemeral=True,
                    )
                elif reason == "missing_permissions":
                    await interaction.followup.send(
                        "I do not have permission to post in the configured channel. "
                        "Grant Send Messages + Embed Links, then run /set_channel again there.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "Refresh failed due to a transient error. Try again in a few seconds.",
                        ephemeral=True,
                    )

    async def _compute_state(self, future_weeks: int) -> RotationState:
        snapshot = await self.wiki.fetch_snapshot()
        return build_rotation_state(
            snapshot=snapshot,
            normal_epoch=self.settings.circuit_normal_epoch,
            steel_epoch=self.settings.circuit_steel_epoch,
            coda_epoch=self.settings.coda_epoch,
            coda_epoch_batch=self.settings.coda_epoch_batch,
            force_coda_batch=self.settings.force_coda_batch,
            future_weeks=future_weeks,
        )

    async def _publish_to_configured_channel(self) -> tuple[bool, str]:
        channel_id = self.publish_state.channel_id
        if not channel_id:
            return False, "no_channel"
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return False, "channel_unavailable"
        if not isinstance(channel, discord.abc.Messageable):
            return False, "channel_unavailable"

        try:
            state = await self._compute_state(future_weeks=6)
            embeds = _build_embeds(state)
        except Exception:
            return False, "build_failed"

        if self.publish_state.message_id:
            try:
                message = await channel.fetch_message(self.publish_state.message_id)  # type: ignore[attr-defined]
                await message.edit(content="Auto-updated Warframe rotations", embeds=embeds, attachments=[])
                self.publish_state.last_weekly_reset_ts = int(state.next_reset_utc.timestamp())
                self.publish_state.last_coda_reset_ts = (
                    int(state.coda_next_reset_utc.timestamp()) if state.coda_next_reset_utc is not None else None
                )
                _save_publish_state(self.publish_state)
                return True, "updated"
            except Forbidden:
                return False, "missing_permissions"
            except HTTPException:
                return False, "channel_unavailable"
            except Exception:
                self.publish_state.message_id = None

        try:
            sent = await channel.send(content="Auto-updated Warframe rotations", embeds=embeds)  # type: ignore[attr-defined]
        except Forbidden:
            return False, "missing_permissions"
        except HTTPException:
            return False, "channel_unavailable"
        self.publish_state.message_id = sent.id
        self.publish_state.last_weekly_reset_ts = int(state.next_reset_utc.timestamp())
        self.publish_state.last_coda_reset_ts = (
            int(state.coda_next_reset_utc.timestamp()) if state.coda_next_reset_utc is not None else None
        )
        _save_publish_state(self.publish_state)
        return True, "created"

    @tasks.loop(minutes=1)
    async def auto_publish(self) -> None:
        now_utc = datetime.now(tz=timezone.utc)
        weekly_target = int(_next_weekly_reset_utc(now_utc).timestamp())
        coda_target = int(_next_coda_reset_utc(now_utc, self.settings.coda_epoch).timestamp())
        weekly_crossed = self.publish_state.last_weekly_reset_ts != weekly_target
        coda_crossed = self.publish_state.last_coda_reset_ts != coda_target
        if weekly_crossed or coda_crossed:
            await self._publish_to_configured_channel()
            return

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
