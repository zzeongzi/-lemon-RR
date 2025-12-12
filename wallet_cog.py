# wallet_cog.py
import asyncio
import io
from typing import Optional, Union, Any, Dict

import discord
import qrcode
from discord import app_commands
from discord.ext import commands

from blink_client_rr import create_invoice, check_payment, pay_invoice, BlinkError
from models_user import get_balance, change_balance


# ─────────────────────────────────────────────
# BOLT11 금액(sats)만 파싱하는 간단 디코더
# ─────────────────────────────────────────────

def decode_bolt11_amount_sats(bolt11: str) -> Optional[int]:
    """
    BOLT11 인보이스에서 금액(sats)만 파싱.
    실패하면 None 반환.
    """
    try:
        ln = bolt11.lower()
        if not ln.startswith("ln"):
            return None

        # hrp / data 분리
        if "1" not in ln:
            return None
        pos = ln.rfind("1")
        hrp = ln[:pos]

        # hrp 에서 amount 추출 (lnbc, lntb, lntbs 등)
        # 예) lnbc1500u -> 1500 * 10^(-6) BTC
        amount_str = hrp[4:]  # 1500u, 20m, 1000n 등 또는 빈 문자열(금액 없음)

        if not amount_str:
            # 금액이 명시되지 않은 인보이스(수취인이 지정)
            return None

        # 단위 파싱
        unit = amount_str[-1]
        if unit.isdigit():
            # 단위 없는 경우 (BTC)
            amount_num_str = amount_str
            multiplier = 10**8  # BTC -> sats
        else:
            amount_num_str = amount_str[:-1]
            if unit == "m":  # milli-BTC
                multiplier = 10**5  # 1 mBTC = 0.001 BTC = 10^5 sats
            elif unit == "u":  # micro-BTC
                multiplier = 10**2  # 1 μBTC = 10^-6 BTC = 10^2 sats
            else:
                # n, p 단위 등은 현재 지원하지 않음
                return None

        if not amount_num_str:
            return None

        amount_num = int(amount_num_str)
        amount_sats = amount_num * multiplier
        return amount_sats
    except Exception:
        return None


class DepositView(discord.ui.View):
    def __init__(
        self,
        payment_hash: str,
        payment_request: str,
        amount_sats: int,
        user: Union[discord.User, discord.Member],
    ):
        super().__init__(timeout=120)
        self.payment_hash = payment_hash
        self.payment_request = payment_request
        self.amount_sats = amount_sats
        self.user = user
        self.message: Optional[discord.Message | discord.WebhookMessage] = None
        self.checking = False

    @discord.ui.button(label="📋 인보이스 복사", style=discord.ButtonStyle.secondary)
    async def copy_invoice(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button["DepositView"],
    ):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "이 인보이스는 다른 사용자의 것입니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "아래 인보이스를 지갑에 붙여 넣어 결제하세요.",
            ephemeral=True,
        )
        await interaction.followup.send(
            self.payment_request,
            ephemeral=True,
        )

    async def start_checking(self) -> None:
        if self.checking:
            return
        self.checking = True

        # 최대 2분 동안 2초 간격으로 결제 여부 확인
        for _ in range(60):
            await asyncio.sleep(2)

            try:
                paid = await check_payment(self.payment_request)
            except Exception as e:
                # Blink 쪽 에러로 루프 전체가 죽지 않도록 보호
                print("[DepositView] check_payment 예외:", e)
                paid = False

            if paid:
                # 결제 완료 → 내부 잔액 증가
                await change_balance(self.user.id, self.amount_sats)
                new_balance = await get_balance(self.user.id)

                if self.message:
                    try:
                        await self.message.edit(
                            content=(
                                f"✅ **입금 확인 완료!**\n"
                                f"+{self.amount_sats} sats 충전되었습니다.\n"
                                f"현재 잔액: **{new_balance} sats**"
                            ),
                            view=None,
                        )
                    except Exception as e:
                        print("[DepositView] message.edit 실패:", e)

                try:
                    await self.user.send(
                        f"⚡ 입금 완료!\n"
                        f"+{self.amount_sats} sats (현재 잔액: {new_balance} sats)"
                    )
                except Exception as e:
                    print("[DepositView] DM 전송 실패:", e)

                return

        # 타임아웃
        if self.message:
            try:
                await self.message.edit(
                    content=(
                        "⏰ **결제 시간 초과** (2분)\n"
                        "`/deposit` 명령어로 다시 시도해주세요."
                    ),
                    view=None,
                )
            except Exception as e:
                print("[DepositView] 타임아웃 message.edit 실패:", e)


class WalletCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="balance", description="현재 잔액을 확인합니다.")
    async def balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        balance = await get_balance(user_id)

        await interaction.followup.send(
            f"💰 현재 잔액: **{balance} sats**",
            ephemeral=True,
        )

    @app_commands.command(name="deposit", description="라이트닝으로 SATS를 입금합니다.")
    @app_commands.describe(
        amount="입금할 금액 (sats 단위)",
    )
    async def deposit(self, interaction: discord.Interaction, amount: int):
        if amount <= 0:
            await interaction.response.send_message(
                "입금 금액은 1 sats 이상이어야 합니다.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # 디스코드 닉네임(서버에서 보이는 이름) 사용
            display_name = interaction.user.display_name
            memo = f"RR Deposit by {display_name}"
            invoice: Dict[str, Any] = await create_invoice(amount, memo)
        except BlinkError as e:
            print("[/deposit] BlinkError:", e)
            await interaction.followup.send(
                "⚠️ 인보이스 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        payment_hash = invoice["payment_hash"]
        payment_request = invoice["payment_request"]
        amount_sats = invoice["amount"]

        # QR 코드 생성
        qr_img = qrcode.make(payment_request)
        buffer = io.BytesIO()
        qr_img.save(buffer, "PNG")
        buffer.seek(0)

        file = discord.File(buffer, filename="invoice.png")

        embed = discord.Embed(
            title="⚡ 라이트닝 입금 인보이스",
            description=(
                f"**{amount_sats} sats** 를 아래 QR 또는 인보이스로 결제해주세요.\n"
                f"2분 안에 결제가 확인되면 자동으로 잔액에 반영됩니다."
            ),
            color=discord.Color.yellow(),
        )
        embed.add_field(name="금액", value=f"{amount_sats} sats", inline=True)
        embed.set_image(url="attachment://invoice.png")

        view = DepositView(
            payment_hash=payment_hash,
            payment_request=payment_request,
            amount_sats=amount_sats,
            user=interaction.user,
        )

        message = await interaction.followup.send(
            embed=embed,
            file=file,
            view=view,
            ephemeral=True,
        )
        view.message = message

        # 결제 확인 루프 시작
        self.bot.loop.create_task(view.start_checking())

    @app_commands.command(name="withdraw", description="외부 BOLT11 인보이스로 출금합니다.")
    @app_commands.describe(
        bolt11="라이트닝 인보이스 (BOLT11)",
    )
    async def withdraw(self, interaction: discord.Interaction, bolt11: str):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        current_balance = await get_balance(user_id)

        if not bolt11.startswith("ln"):
            await interaction.followup.send(
                "유효한 BOLT11 인보이스를 입력해주세요.",
                ephemeral=True,
            )
            return

        if current_balance <= 0:
            await interaction.followup.send(
                "출금 가능한 잔액이 없습니다.",
                ephemeral=True,
            )
            return

        # BOLT11 에서 금액(sats) 디코딩
        amount_sats = decode_bolt11_amount_sats(bolt11)
        if amount_sats is None or amount_sats <= 0:
            await interaction.followup.send(
                "이 인보이스에서 출금 금액을 확인할 수 없습니다. "
                "금액이 포함된 BOLT11 인보이스를 사용해주세요.",
                ephemeral=True,
            )
            return

        if amount_sats > current_balance:
            await interaction.followup.send(
                f"요청한 인보이스 금액은 **{amount_sats} sats** 이지만,\n"
                f"현재 잔액은 **{current_balance} sats** 입니다.\n"
                f"잔액 이하의 금액으로 인보이스를 생성해주세요.",
                ephemeral=True,
            )
            return

        try:
            # 출금 메모에도 디스코드 닉네임 사용
            display_name = interaction.user.display_name
            result = await pay_invoice(bolt11, memo=f"RR Withdraw by {display_name}")
        except BlinkError as e:
            print("[/withdraw] BlinkError:", e)
            await interaction.followup.send(
                "⚠️ 출금 처리 중 Blink 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        if not result.get("success"):
            await interaction.followup.send(
                "⚠️ 출금 결제가 실패했습니다. 인보이스가 유효한지 확인해주세요.",
                ephemeral=True,
            )
            return

        # BOLT11 인보이스에 포함된 금액만큼만 잔액 차감
        await change_balance(user_id, -amount_sats)

        await interaction.followup.send(
            f"✅ **출금 완료!**\n"
            f"-{amount_sats} sats 출금되었습니다.\n"
            f"남은 잔액은 추후 `/balance` 명령어로 확인할 수 있습니다.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletCog(bot))
