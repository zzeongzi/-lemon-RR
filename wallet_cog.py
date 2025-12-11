# wallet_cog.py
import discord
from discord import app_commands
from discord.ext import commands

from models_user import get_balance, change_balance, log_cashu_tx
from cashu_client import redeem_token, mint_token, CashuError

class Wallet(commands.Cog):
    """캐슈 지갑 관련 슬래시 명령어 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="wallet_balance",
        description="내 캐슈 잔액을 확인합니다."
    )
    async def wallet_balance(self, interaction: discord.Interaction):
        balance = await get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"현재 캐슈 잔액: `{balance} sats`",
            ephemeral=True
        )

    @app_commands.command(
        name="wallet_deposit",
        description="캐슈 토큰을 봇에 예치하여 잔액을 충전합니다."
    )
    @app_commands.describe(token="Minibits 지갑에서 받은 캐슈 토큰 문자열")
    async def wallet_deposit(self, interaction: discord.Interaction, token: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            amount = redeem_token(token)
        except CashuError as e:
            await interaction.followup.send(
                f"❌ 토큰 상환에 실패했습니다:\n`{e}`",
                ephemeral=True
            )
            return

        new_balance = await change_balance(interaction.user.id, amount)
        await log_cashu_tx(interaction.user.id, "DEPOSIT", amount, token)

        await interaction.followup.send(
            f"✅ 토큰 상환 완료!\n"
            f"충전된 금액: `{amount} sats`\n"
            f"현재 잔액: `{new_balance} sats`",
            ephemeral=True
        )

    @app_commands.command(
        name="wallet_withdraw",
        description="내부 잔액을 캐슈 토큰으로 출금합니다."
    )
    @app_commands.describe(amount="출금할 금액 (sats 단위)")
    async def wallet_withdraw(self, interaction: discord.Interaction, amount: int):
        if amount <= 0:
            await interaction.response.send_message(
                "출금 금액은 1 sats 이상이어야 합니다.",
                ephemeral=True
            )
            return

        current_balance = await get_balance(interaction.user.id)
        if current_balance < amount:
            await interaction.response.send_message(
                f"❌ 잔액 부족입니다. 현재 잔액: `{current_balance} sats`",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            token = mint_token(amount)
        except CashuError as e:
            await interaction.followup.send(
                f"❌ 토큰 발행에 실패했습니다:\n`{e}`",
                ephemeral=True
            )
            return

        new_balance = await change_balance(interaction.user.id, -amount)
        await log_cashu_tx(interaction.user.id, "WITHDRAW", amount, token)

        dm_sent = False
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                f"요청하신 `{amount} sats` 출금 토큰입니다:\n"
                f"```text\n{token}\n```"
            )
            dm_sent = True
        except discord.Forbidden:
            dm_sent = False

        if dm_sent:
            extra = "DM(개인 메시지)로 토큰을 전송했습니다."
        else:
            extra = (
                "DM(개인 메시지)을 보낼 수 없어, 여기 토큰을 표시합니다.\n"
                f"```text\n{token}\n```"
            )

        await interaction.followup.send(
            f"✅ 출금 완료!\n"
            f"출금 금액: `{amount} sats`\n"
            f"출금 후 잔액: `{new_balance} sats`\n\n"
            f"{extra}",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Wallet(bot))
