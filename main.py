from __future__ import annotations

from dotenv import load_dotenv

from bot.config import load_settings
from bot.discord_bot import WarframeRotationBot


def main() -> None:
    load_dotenv()
    settings = load_settings()
    bot = WarframeRotationBot(settings)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
