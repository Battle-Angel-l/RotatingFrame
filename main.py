from __future__ import annotations

import logging

from dotenv import load_dotenv

from bot.config import load_settings
from bot.discord_bot import WarframeRotationBot


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = load_settings()
    logging.getLogger("warframe-bot").info(
        (
            "Startup config loaded: "
            "guild_id=%s update_interval_minutes=%s "
            "circuit_normal_epoch=%s circuit_steel_epoch=%s coda_epoch=%s"
        ),
        settings.guild_id,
        settings.update_interval_minutes,
        settings.circuit_normal_epoch.isoformat(),
        settings.circuit_steel_epoch.isoformat(),
        settings.coda_epoch.isoformat(),
    )
    bot = WarframeRotationBot(settings)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
