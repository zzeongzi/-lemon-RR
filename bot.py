# bot.py
import discord
from discord.ext import commands

from config import DISCORD_TOKEN
from db import init_db

INTENTS = discord.Intents.default()
INTENTS.members = True

class CashuRRBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=INTENTS,
        )

    async def setup_hook(self):
        await init_db()

        await self.load_extension("wallet_cog")
        await self.load_extension("rr_cog")

        await self.tree.sync()
        print("Slash commands synced.")

bot = CashuRRBot()

@bot.event
async def on_ready():
    if bot.user is None:
    # 이론상 거의 안 오지만, 타입 체크용
        print("Bot user is None")
        return
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
