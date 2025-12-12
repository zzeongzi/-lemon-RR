# bot.py
import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import DISCORD_TOKEN

load_dotenv()


intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # 멤버 관련 이벤트/정보 사용 시 필요


class LEMONBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="!",
            intents=intents,
        )

    async def setup_hook(self) -> None:
        # Cog 로드
        await self.load_extension("wallet_cog")
        await self.load_extension("rr_cog")

        # 슬래시 커맨드 동기화
        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self) -> None:
        user = self.user
        if user is None:
            print("Bot user is None (on_ready)")
            return
        print(f"Logged in as {user} (ID: {user.id})")


async def main() -> None:
    token = DISCORD_TOKEN or os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN 이 설정되지 않았습니다.")

    bot = LEMONBot()

    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
