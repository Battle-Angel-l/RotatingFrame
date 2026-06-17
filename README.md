# Warframe Rotation Discord Bot (Python)

Discord bot that posts and serves current Warframe rotation info from `wiki.warframe.com`, including:

- Circuit normal rotation
- Circuit Steel Path rotation
- Weekly reset timers
- Future Circuit rotations
- Technocyte Coda weapon bonus values (from wiki rewards tables)
- Alerts feed (with Gifts from the Lotus marked)

## Features

- Slash commands:
  - `/rotations` - current rotations + timer + coda bonus table + alerts feed
  - `/future weeks:<1-12>` - upcoming weekly rotations
  - `/setup_channel name:<channel-name>` - creates a dedicated channel and starts auto-posting there
  - `/set_channel` - uses the current channel for auto-posting
  - `/refresh_rotations` - force refresh now
- Auto-updating message in configured channel.
- Uses wiki page scraping at runtime (no hardcoded reward lists).

## Setup

1. Create a Discord application + bot in the Discord Developer Portal.
2. Enable `applications.commands` and invite the bot with permissions:
   - Send Messages
   - Embed Links
   - Manage Channels (only required if you want `/setup_channel`)
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and set:

- `DISCORD_TOKEN`
- (optional) `DISCORD_GUILD_ID` for faster slash command sync in one server
- (optional) `ROTATIONS_CHANNEL_ID` if you already know your target channel id
- (optional) `UPDATE_INTERVAL_MINUTES` (default 5)

5. Run:

```bash
python main.py
```

## Notes

- Weekly reset is assumed as Monday 00:00 UTC.
- Current week index is computed from epoch dates:
  - `CIRCUIT_NORMAL_EPOCH`
  - `CIRCUIT_STEEL_EPOCH`
- Current default anchors:
  - `CIRCUIT_NORMAL_EPOCH=2023-05-01`
  - `CIRCUIT_STEEL_EPOCH=2023-05-29`
- If wiki/game rotation alignment changes, update those two values in `.env`.
- Data source pages:
  - `https://wiki.warframe.com/w/The_Circuit`
  - `https://wiki.warframe.com/w/Coda_Weapons`
  - `https://hub.warframe.us/pc/alerts` (with fallback to `https://api.warframestat.us/pc/alerts`)

## Optional Improvements

- Add role mentions on weekly reset.
- Add more rotations (e.g. additional systems/items) by extending `wiki_client.py`.
- Persist command sync/state into a small SQLite DB instead of JSON.
