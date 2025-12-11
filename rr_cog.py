# rr_cog.py
import random
import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from db import get_db
from models_user import get_balance, change_balance

ENTRY_FEE_DEFAULT = 100
MAX_PLAYERS_DEFAULT = 6
BULLET_COUNT_DEFAULT = 1
GAME_TIMEOUT_SECONDS = 300  # 5분 동안 액션 없으면 자동 종료


class RussianRoulette(commands.Cog):
    """캐슈 잔액을 사용한 러시안 룰렛 게임 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        # channel_id -> timeout task
        self._timeout_tasks: dict[int, asyncio.Task[Any]] = {}

    # 내부 헬퍼: 현재 채널의 진행중 / 대기중 게임 가져오기 (가장 최근 1개)
    async def _get_active_game(self, channel_id: int) -> tuple[int, str] | None:
        db = await get_db()
        cur = await db.execute(
            """
            SELECT id, status FROM rr_games
            WHERE channel_id = ? AND status IN ('WAITING', 'RUNNING')
            ORDER BY id DESC LIMIT 1
            """,
            (channel_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        game_id, status = row
        return int(game_id), str(status)

    # 내부 헬퍼: 현재 채널의 모든 대기중 게임 목록 (게임 선택용)
    async def _get_waiting_games(self, channel_id: int) -> list[tuple[int, int, int]]:
        """
        반환: [(game_id, host_user_id, entry_fee), ...]
        """
        db = await get_db()
        cur = await db.execute(
            """
            SELECT id, host_user_id, entry_fee
            FROM rr_games
            WHERE channel_id = ? AND status = 'WAITING'
            ORDER BY id ASC
            """,
            (channel_id,),
        )
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    async def _create_game(
        self,
        channel_id: int,
        host_user_id: int,
        entry_fee: int = ENTRY_FEE_DEFAULT,
        max_players: int = MAX_PLAYERS_DEFAULT,
        bullet_count: int = BULLET_COUNT_DEFAULT,
    ) -> int:
        db = await get_db()
        cur = await db.execute(
            """
            INSERT INTO rr_games (
                channel_id, host_user_id, entry_fee,
                max_players, bullet_count, status
            )
            VALUES (?, ?, ?, ?, ?, 'WAITING')
            """,
            (channel_id, host_user_id, entry_fee, max_players, bullet_count),
        )
        await db.commit()

        last_id = cur.lastrowid
        if last_id is None:
            raise RuntimeError("Failed to get lastrowid for rr_games")
        return int(last_id)

    async def _add_player(self, game_id: int, user_id: int) -> int:
        db = await get_db()
        # 이미 참가했는지 확인
        cur = await db.execute(
            "SELECT id FROM rr_players WHERE game_id = ? AND user_id = ?",
            (game_id, user_id),
        )
        row = await cur.fetchone()
        if row is not None:
            raise ValueError("이미 이 게임에 참가했습니다.")

        # 현재 인원 수
        cur = await db.execute(
            "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
            (game_id,),
        )
        count_row = await cur.fetchone()
        current_count = int(count_row[0]) if count_row is not None else 0

        # order_index = 현재 인원 수 + 1
        order_index = current_count + 1
        await db.execute(
            """
            INSERT INTO rr_players (game_id, user_id, order_index)
            VALUES (?, ?, ?)
            """,
            (game_id, user_id, order_index),
        )
        await db.commit()
        return order_index

    async def _get_players(self, game_id: int) -> list[tuple[int, int, int]]:
        db = await get_db()
        cur = await db.execute(
            """
            SELECT user_id, order_index, alive
            FROM rr_players
            WHERE game_id = ?
            ORDER BY order_index ASC
            """,
            (game_id,),
        )
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    async def _start_game(self, game_id: int) -> None:
        db = await get_db()
        # 게임 정보
        cur = await db.execute(
            "SELECT max_players, bullet_count FROM rr_games WHERE id = ?",
            (game_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("게임 정보를 찾을 수 없습니다.")

        max_players_raw, bullet_count_raw = row

        if max_players_raw is None:
            raise RuntimeError("게임 설정(max_players)이 잘못되었습니다.")
        if bullet_count_raw is None:
            raise RuntimeError("게임 설정(bullet_count)이 잘못되었습니다.")

        max_players = int(max_players_raw)
        bullet_count = int(bullet_count_raw)

        # 현재 참가자 수
        cur = await db.execute(
            "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
            (game_id,),
        )
        count_row = await cur.fetchone()

        if count_row is None:
            player_count = 0
        else:
            count_value = count_row[0]
            player_count = int(count_value or 0)

        # --- 테스트용: 혼자도 시작 가능하게 허용 ---
        MIN_PLAYERS = 1  # 실제 서비스에서는 2로 변경
        if player_count < MIN_PLAYERS:
            raise ValueError(f"최소 {MIN_PLAYERS}명 이상 모여야 게임을 시작할 수 있습니다.")

        # 실린더(max_players 칸) 생성
        cylinder_size = max_players
        bullet_count = min(bullet_count, cylinder_size)
        cylinder_list = [0] * cylinder_size
        bullet_positions = random.sample(range(cylinder_size), bullet_count)
        for pos in bullet_positions:
            cylinder_list[pos] = 1
        cylinder_str = "".join(str(x) for x in cylinder_list)

        # 상태 기록
        await db.execute(
            """
            INSERT OR REPLACE INTO rr_state (game_id, current_turn, cylinder)
            VALUES (?, ?, ?)
            """,
            (game_id, 1, cylinder_str),
        )
        await db.execute(
            """
            UPDATE rr_games
            SET status = 'RUNNING', started_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (game_id,),
        )
        await db.commit()

    async def _update_last_action(self, game_id: int) -> None:
        db = await get_db()
        await db.execute(
            """
            UPDATE rr_state
            SET last_action_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
            """,
            (game_id,),
        )
        await db.commit()

    async def _pull_trigger(
        self,
        game_id: int,
        user_id: int,
    ) -> tuple[bool, bool, int | None, int]:
        """
        방아쇠를 당기고, 생존 여부/승리 여부/상금 정보를 반환.
        반환: (shot, dead, winner_user_id, prize_amount)
        """
        db = await get_db()
        # 상태 조회
        cur = await db.execute(
            """
            SELECT current_turn, cylinder
            FROM rr_state
            WHERE game_id = ?
            """,
            (game_id,),
        )
        state_row = await cur.fetchone()
        if state_row is None:
            raise RuntimeError("게임 상태를 찾을 수 없습니다.")
        current_turn, cylinder = state_row
        current_turn = int(current_turn)
        cylinder = str(cylinder)

        # 현재 턴의 플레이어
        cur = await db.execute(
            """
            SELECT user_id, order_index, alive
            FROM rr_players
            WHERE game_id = ? AND order_index = ?
            """,
            (game_id, current_turn),
        )
        player_row = await cur.fetchone()
        if player_row is None:
            raise RuntimeError("현재 차례인 플레이어를 찾을 수 없습니다.")

        turn_user_id, _, alive = player_row
        turn_user_id = int(turn_user_id)
        alive = int(alive)

        if not alive:
            raise RuntimeError("현재 플레이어는 이미 사망 처리되었습니다.")

        if turn_user_id != user_id:
            raise ValueError("지금은 당신의 차례가 아닙니다.")

        # cylinder에서 현재 칸 확인 (index = current_turn - 1)
        idx = current_turn - 1
        if idx < 0 or idx >= len(cylinder):
            raise RuntimeError("실린더 인덱스가 올바르지 않습니다.")

        shot = (cylinder[idx] == "1")
        dead = False
        winner_user_id: int | None = None
        prize_amount = 0

        if shot:
            # 사망 처리
            dead = True
            await db.execute(
                """
                UPDATE rr_players
                SET alive = 0
                WHERE game_id = ? AND user_id = ?
                """,
                (game_id, user_id),
            )

        # 살아있는 플레이어 수 확인
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM rr_players
            WHERE game_id = ? AND alive = 1
            """,
            (game_id,),
        )
        alive_count_row = await cur.fetchone()
        alive_count = int(alive_count_row[0]) if alive_count_row is not None else 0

        # 전체 참가자 수도 같이 조회 (혼자 모드 판별용)
        cur = await db.execute(
            "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
            (game_id,),
        )
        total_players_row = await cur.fetchone()
        total_players = int(total_players_row[0]) if total_players_row is not None else 0

        # --- 테스트 모드: 참가자가 1명뿐이면, 죽어도 게임을 종료하지 않고 계속 돌린다 ---
        if total_players <= 1:
            # 혼자 테스트 모드:
            # - shot=True 이면 alive=0 이지만, 다음 턴 계산 때 다시 그 사람만 남아서 계속 돌아감
            # - 상금도 지급하지 않음
            # 그냥 아래 "아직 게임 계속" 로직으로 진행
            pass
        else:
            # 실제 멀티 플레이 모드: 1명만 살아남으면 게임 종료
            if alive_count <= 1:
                # 게임 종료 -> 승자에게 상금 지급
                cur = await db.execute(
                    """
                    SELECT user_id FROM rr_players
                    WHERE game_id = ? AND alive = 1
                    """,
                    (game_id,),
                )
                winner_row = await cur.fetchone()
                if winner_row is not None:
                    winner_user_id = int(winner_row[0])

                    # 상금풀 = 참가자 수 * entry_fee
                    cur = await db.execute(
                        "SELECT entry_fee FROM rr_games WHERE id = ?",
                        (game_id,),
                    )
                    fee_row = await cur.fetchone()
                    entry_fee = int(fee_row[0]) if fee_row is not None else 0

                    prize_amount = entry_fee * total_players

                    # 상금 지급 (discord_user_id 사용)
                    await change_balance(winner_user_id, prize_amount)

                await db.execute(
                    """
                    UPDATE rr_games
                    SET status = 'FINISHED', finished_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (game_id,),
                )
                await db.commit()
                return shot, dead, winner_user_id, prize_amount


        # 아직 게임 계속
        # 다음 턴 계산: 살아있는 사람 중 현재 턴 다음 order_index
        cur = await db.execute(
            """
            SELECT order_index FROM rr_players
            WHERE game_id = ? AND alive = 1
            ORDER BY order_index ASC
            """,
            (game_id,),
        )
        alive_rows = await cur.fetchall()
        order_list = [int(r[0]) for r in alive_rows]
        if current_turn not in order_list:
            # 방금 죽었으면, 리스트에서 자기보다 큰 첫 order_index
            # 없으면 가장 작은 order_index
            larger = [o for o in order_list if o > current_turn]
            if larger:
                next_turn = min(larger)
            else:
                next_turn = min(order_list)
        else:
            # 살아있는 상태에서 방아쇠를 당겼고 shot=False 인 경우
            idx_in_alive = order_list.index(current_turn)
            next_turn = order_list[(idx_in_alive + 1) % len(order_list)]

        await db.execute(
            """
            UPDATE rr_state
            SET current_turn = ?, last_action_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
            """,
            (next_turn, game_id),
        )
        await db.commit()

        return shot, dead, None, 0

    async def _schedule_timeout(self, channel: discord.TextChannel, game_id: int) -> None:
        async def timeout_task() -> None:
            await asyncio.sleep(GAME_TIMEOUT_SECONDS)
            async with self._lock:
                db = await get_db()
                cur = await db.execute(
                    """
                    SELECT status FROM rr_games
                    WHERE id = ?
                    """,
                    (game_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return
                status = str(row[0])
                if status not in ("WAITING", "RUNNING"):
                    return

                await db.execute(
                    """
                    UPDATE rr_games
                    SET status = 'CANCELLED', finished_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (game_id,),
                )
                await db.commit()

                await channel.send(
                    "⏰ 5분 동안 움직임이 없어 러시안 룰렛 게임이 자동 종료되었습니다."
                )

        task: asyncio.Task[Any] = asyncio.create_task(timeout_task())
        self._timeout_tasks[channel.id] = task

    # /rr_create
    @app_commands.command(
        name="rr_create",
        description="러시안 룰렛 게임을 생성합니다.",
    )
    @app_commands.describe(
        entry_fee="참가비 (sats 단위, 기본값 100)",
        max_players="최대 인원 수 (기본값 6명)",
    )
    async def rr_create(
        self,
        interaction: discord.Interaction,
        entry_fee: int = ENTRY_FEE_DEFAULT,
        max_players: int = MAX_PLAYERS_DEFAULT,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "텍스트 채널에서만 사용할 수 있는 명령어입니다.",
                ephemeral=True,
            )
            return

        async with self._lock:
            existing = await self._get_active_game(interaction.channel.id)
            if existing is not None:
                await interaction.response.send_message(
                    "이 채널에는 이미 진행 중이거나 대기 중인 러시안 룰렛 게임이 있습니다.\n"
                    "현재 게임이 끝난 뒤에 새로 생성할 수 있어요.",
                    ephemeral=True,
                )
                return

            game_id = await self._create_game(
                interaction.channel.id,
                interaction.user.id,
                entry_fee=entry_fee,
                max_players=max_players,
                bullet_count=BULLET_COUNT_DEFAULT,
            )

            await interaction.response.send_message(
                f"🎲 러시안 룰렛 게임을 생성했어요! (ID: `{game_id}`)\n"
                f"- 참가비: **{entry_fee} sats**\n"
                f"- 최대 인원: **{max_players}명**\n"
                f"- 탄환 수: **{BULLET_COUNT_DEFAULT}발**\n\n"
                f"참가하려면 `/rr_join` 명령어를 사용해 주세요.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

            await self._schedule_timeout(interaction.channel, game_id)

    # /rr_join  (여러 게임 중 선택 가능)
    @app_commands.command(
        name="rr_join",
        description="러시안 룰렛 게임에 참가합니다.",
    )
    @app_commands.describe(
        game_id="참가할 게임 ID (선택하지 않으면 가장 최근 대기중 게임에 참가합니다)",
    )
    async def rr_join(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "텍스트 채널에서만 사용할 수 있는 명령어입니다.",
                ephemeral=True,
            )
            return

        async with self._lock:
            # game_id 를 선택하지 않은 경우: 이 채널의 WAITING 게임 목록 보여주기 느낌
            waiting_games = await self._get_waiting_games(interaction.channel.id)
            if not waiting_games:
                await interaction.response.send_message(
                    "이 채널에는 대기 중인 러시안 룰렛 게임이 없습니다.\n"
                    "`/rr_create` 로 새 게임을 먼저 만들어 주세요.",
                    ephemeral=True,
                )
                return

            if game_id is None:
                # 가장 최근 게임에 자동 참가 (선택 UI 대신 단순화)
                game_id = waiting_games[-1][0]

            # 선택한 game_id 가 이 채널의 WAITING 게임인지 검증
            if all(g[0] != game_id for g in waiting_games):
                await interaction.response.send_message(
                    "선택한 게임을 찾을 수 없거나 이미 시작/종료된 게임입니다.",
                    ephemeral=True,
                )
                return

            db = await get_db()
            cur = await db.execute(
                "SELECT entry_fee, max_players FROM rr_games WHERE id = ?",
                (game_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await interaction.response.send_message(
                    "게임 정보를 찾을 수 없습니다. 잠시 후 다시 시도해 주세요.",
                    ephemeral=True,
                )
                return
            entry_fee, max_players = row
            entry_fee = int(entry_fee)
            max_players = int(max_players)

            # 현재 참가자 수
            cur = await db.execute(
                "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
                (game_id,),
            )
            count_row = await cur.fetchone()
            current_count = int(count_row[0]) if count_row is not None else 0
            if current_count >= max_players:
                await interaction.response.send_message(
                    "이미 최대 인원에 도달한 게임입니다.",
                    ephemeral=True,
                )
                return

        # 잔액 확인 및 참가비 차감
        balance = await get_balance(interaction.user.id)
        if balance < entry_fee:
            await interaction.response.send_message(
                f"잔액이 부족합니다.\n"
                f"- 참가비: **{entry_fee} sats**\n"
                f"- 현재 잔액: **{balance} sats**",
                ephemeral=True,
            )
            return

        try:
            await change_balance(interaction.user.id, -entry_fee)
        except ValueError:
            await interaction.response.send_message(
                "잔액 부족으로 인해 참가에 실패했습니다. 잔액을 다시 확인해 주세요.",
                ephemeral=True,
            )
            return

        # 참가 등록
        try:
            order_index = await self._add_player(game_id, interaction.user.id)
        except ValueError as e:
            await interaction.response.send_message(
                str(e),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ 러시안 룰렛 게임(ID: `{game_id}`)에 참가했습니다!\n"
            f"당신의 순번은 **{order_index}번** 입니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # /rr_start
    @app_commands.command(
        name="rr_start",
        description="러시안 룰렛 게임을 시작합니다.",
    )
    async def rr_start(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "텍스트 채널에서만 사용할 수 있는 명령어입니다.",
                ephemeral=True,
            )
            return

        async with self._lock:
            active = await self._get_active_game(interaction.channel.id)
            if active is None:
                await interaction.response.send_message(
                    "이 채널에는 대기 중인 러시안 룰렛 게임이 없습니다.",
                    ephemeral=True,
                )
                return

            game_id, status = active
            if status != "WAITING":
                await interaction.response.send_message(
                    "이미 시작되었거나 종료된 게임입니다.",
                    ephemeral=True,
                )
                return

            # 게임 생성자만 시작 가능하도록 하려면 여기서 host_user_id 검사 추가도 가능
            try:
                await self._start_game(game_id)
            except ValueError as e:
                await interaction.response.send_message(
                    f"게임을 시작할 수 없습니다.\n➡ {e}",
                    ephemeral=True,
                )
                return
            except RuntimeError as e:
                await interaction.response.send_message(
                    f"게임을 시작하는 중 오류가 발생했습니다.\n➡ {e}",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"🔫 러시안 룰렛 게임(ID: `{game_id}`)을 시작합니다!\n"
                f"`/rr_pull` 명령어로 자신의 차례에 방아쇠를 당겨 주세요.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    # /rr_pull
    @app_commands.command(
        name="rr_pull",
        description="내 차례라면 방아쇠를 당깁니다.",
    )
    async def rr_pull(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "텍스트 채널에서만 사용할 수 있는 명령어입니다.",
                ephemeral=True,
            )
            return

        async with self._lock:
            active = await self._get_active_game(interaction.channel.id)
            if active is None:
                await interaction.response.send_message(
                    "이 채널에는 진행 중인 러시안 룰렛 게임이 없습니다.",
                    ephemeral=True,
                )
                return

            game_id, status = active
            if status != "RUNNING":
                await interaction.response.send_message(
                    "아직 시작되지 않았거나 이미 종료된 게임입니다.",
                    ephemeral=True,
                )
                return

            try:
                shot, dead, winner_user_id, prize_amount = await self._pull_trigger(
                    game_id, interaction.user.id
                )
            except ValueError as e:
                await interaction.response.send_message(
                    f"❌ 진행할 수 없습니다.\n➡ {e}",
                    ephemeral=True,
                )
                return
            except RuntimeError as e:
                await interaction.response.send_message(
                    f"오류가 발생했습니다.\n➡ {e}",
                    ephemeral=True,
                )
                return

            await self._update_last_action(game_id)

            # 기본 메시지 텍스트 & 사용할 이미지 경로
            image_path: str | None = None

            if winner_user_id is not None:
                # 게임 종료 + 승자
                if shot and dead and winner_user_id != interaction.user.id:
                    msg = (
                        f"💥 탕! <@{interaction.user.id}> 님이 사망했습니다...\n\n"
                        f"🏆 마지막 생존자: <@{winner_user_id}> 님!\n"
                        f"상금 **{prize_amount} sats** 가 지급되었습니다."
                    )
                    image_path = "assets/bang_dead.png"
                else:
                    msg = (
                        f"🏆 러시안 룰렛이 종료되었습니다!\n"
                        f"마지막 생존자: <@{winner_user_id}> 님\n"
                        f"상금 **{prize_amount} sats** 가 지급되었습니다."
                    )
            else:
                # 게임 계속
                if shot and dead:
                    msg = f"💥 탕! <@{interaction.user.id}> 님이 사망했습니다..."
                    image_path = "assets/bang_dead.png"
                else:
                    msg = f"🫨 철컥! <@{interaction.user.id}> 님은 살아남았습니다."
                    image_path = "assets/empty_click.png"

            # 이미지 첨부 여부에 따라 전송
            if image_path is not None:
                file = discord.File(image_path, filename=image_path.split("/")[-1])
                await interaction.response.send_message(
                    msg,
                    file=file,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            else:
                await interaction.response.send_message(
                    msg,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )

    # /rr_close : 게임 생성자만 대기 중 게임을 폐쇄
    @app_commands.command(
        name="rr_close",
        description="대기 중인 러시안 룰렛 게임을 종료합니다. (게임 생성자 전용)",
    )
    @app_commands.describe(
        game_id="종료할 게임 ID (선택하지 않으면 이 채널의 가장 최근 대기중 게임)",
    )
    async def rr_close(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "텍스트 채널에서만 사용할 수 있는 명령어입니다.",
                ephemeral=True,
            )
            return

        async with self._lock:
            db = await get_db()

            if game_id is None:
                # 이 채널의 가장 최근 WAITING 게임 1개 찾기
                cur = await db.execute(
                    """
                    SELECT id, host_user_id, status
                    FROM rr_games
                    WHERE channel_id = ? AND status = 'WAITING'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (interaction.channel.id,),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT id, host_user_id, status
                    FROM rr_games
                    WHERE id = ? AND channel_id = ?
                    """,
                    (game_id, interaction.channel.id),
                )

            row = await cur.fetchone()
            if row is None:
                await interaction.response.send_message(
                    "이 채널에서 종료할 수 있는 대기 중 게임을 찾지 못했습니다.",
                    ephemeral=True,
                )
                return

            found_game_id, host_user_id, status = int(row[0]), int(row[1]), str(row[2])

            if status != "WAITING":
                await interaction.response.send_message(
                    "이미 시작되었거나 종료된 게임은 폐쇄할 수 없습니다.",
                    ephemeral=True,
                )
                return

            if host_user_id != interaction.user.id:
                await interaction.response.send_message(
                    "이 게임의 생성자만 게임을 폐쇄할 수 있습니다.",
                    ephemeral=True,
                )
                return

            await db.execute(
                """
                UPDATE rr_games
                SET status = 'CANCELLED', finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (found_game_id,),
            )
            await db.commit()

            await interaction.response.send_message(
                f"🛑 러시안 룰렛 게임(ID: `{found_game_id}`)이 생성자에 의해 폐쇄되었습니다.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    # /rr_debug_add_balance : 디버그용 잔액 충전 (관리자/개발용)
    @app_commands.command(
        name="rr_debug_add_balance",
        description="(테스트용) 내 캐슈 잔액을 임의로 충전합니다.",
    )
    @app_commands.describe(
        amount="추가할 금액 (sats 단위)",
    )
    async def rr_debug_add_balance(
        self,
        interaction: discord.Interaction,
        amount: int,
    ) -> None:
        # 필요하면 특정 사용자 ID만 허용하도록 조건 추가 가능
        if amount <= 0:
            await interaction.response.send_message(
                "0보다 큰 금액만 입력할 수 있습니다.",
                ephemeral=True,
            )
            return

        await change_balance(interaction.user.id, amount)
        new_balance = await get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"✅ 테스트용으로 **{amount} sats** 를 충전했습니다.\n"
            f"현재 잔액: **{new_balance} sats**",
            ephemeral=True,
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RussianRoulette(bot))
