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
MAX_PLAYERS_DEFAULT = 6          # 항상 6으로 고정
BULLET_COUNT_DEFAULT = 1         # 각 라운드에서 실린더에 넣을 총알 수 (항상 1발)
GAME_TIMEOUT_SECONDS = 300       # 5분 동안 액션 없으면 자동 종료


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
        """게임 룸을 생성 (max_players는 항상 6으로 저장)"""
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
            INSERT INTO rr_players (game_id, user_id, order_index, alive)
            VALUES (?, ?, ?, 1)
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

    # ---------------- 라운드 기반 실린더 생성 ----------------

    async def _start_round(self, game_id: int) -> None:
        """
        새 라운드를 시작한다.
        - 실린더는 항상 6칸
        - 총알은 1발
        - alive=1인 플레이어들만 턴을 돌린다.
        """
        db = await get_db()

        # 현재 살아있는 플레이어 수
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM rr_players
            WHERE game_id = ? AND alive = 1
            """,
            (game_id,),
        )
        alive_row = await cur.fetchone()
        alive_count = int(alive_row[0]) if alive_row is not None else 0

        # 최소 인원 체크 (테스트용: 1명도 허용)
        MIN_PLAYERS = 1
        if alive_count < MIN_PLAYERS:
            raise ValueError(f"최소 {MIN_PLAYERS}명 이상 모여야 게임을 시작할 수 있습니다.")

        # 현재 rr_state에서 round_number 읽기
        cur = await db.execute(
            """
            SELECT round_number
            FROM rr_state
            WHERE game_id = ?
            """,
            (game_id,),
        )
        state_row = await cur.fetchone()
        if state_row is None:
            round_number = 0
        else:
            round_number = int(state_row[0] or 0)

        # 새 라운드 번호
        round_number += 1

        # 항상 6칸짜리 실린더 생성
        cylinder_size = MAX_PLAYERS_DEFAULT  # 항상 6
        bullet_count = BULLET_COUNT_DEFAULT  # 항상 1발
        bullet_count = min(bullet_count, cylinder_size)

        cylinder_list = [0] * cylinder_size
        bullet_positions = random.sample(range(cylinder_size), bullet_count)
        for pos in bullet_positions:
            cylinder_list[pos] = 1
        cylinder_str = "".join(str(x) for x in cylinder_list)

        # 새 라운드 시작: current_turn = 살아있는 사람 중 order_index가 가장 작은 사람
        cur = await db.execute(
            """
            SELECT order_index
            FROM rr_players
            WHERE game_id = ? AND alive = 1
            ORDER BY order_index ASC
            LIMIT 1
            """,
            (game_id,),
        )
        first_alive_row = await cur.fetchone()
        if first_alive_row is None:
            # 살아있는 사람이 없으면 게임을 종료하는 게 맞지만,
            # 여기서는 그냥 예외를 던진다.
            raise RuntimeError("살아있는 플레이어가 없습니다.")

        first_turn = int(first_alive_row[0])

        # rr_state 갱신: 새 라운드 + 샷 카운트 초기화
        await db.execute(
            """
            INSERT OR REPLACE INTO rr_state (
                game_id, current_turn, cylinder,
                round_number, shot_in_round, last_action_at
            )
            VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            """,
            (game_id, first_turn, cylinder_str, round_number),
        )
        # 게임 상태 RUNNING 으로 보장
        await db.execute(
            """
            UPDATE rr_games
            SET status = 'RUNNING', started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (game_id,),
        )
        await db.commit()

    async def _start_game(self, game_id: int) -> None:
        """
        게임 시작 시 호출.
        - 내부적으로는 첫 라운드를 시작한다.
        """
        db = await get_db()

        # 전체 참가자 수
        cur = await db.execute(
            "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
            (game_id,),
        )
        count_row = await cur.fetchone()
        total_players = int(count_row[0]) if count_row is not None else 0

        MIN_PLAYERS = 1  # 실제 라이브 서비스에서는 2로 변경 가능
        if total_players < MIN_PLAYERS:
            raise ValueError(f"최소 {MIN_PLAYERS}명 이상 모여야 게임을 시작할 수 있습니다.")

        # 모든 참가자를 alive=1로 보장
        await db.execute(
            """
            UPDATE rr_players
            SET alive = 1
            WHERE game_id = ?
            """,
            (game_id,),
        )
        await db.commit()

        # 첫 라운드 시작
        await self._start_round(game_id)

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

    async def _get_next_player_id(self, game_id: int, current_user_id: int) -> int | None:
        """
        현재 유저 기준으로 다음 턴 유저의 user_id 반환.
        - alive = 1 인 플레이어들만 대상으로 한다.
        - order_index 기준으로 다음, 없으면 가장 작은 order_index.
        """
        db = await get_db()

        # 현재 플레이어의 order_index 조회
        cur = await db.execute(
            """
            SELECT order_index
            FROM rr_players
            WHERE game_id = ? AND user_id = ? AND alive = 1
            """,
            (game_id, current_user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cur_order = int(row[0])

        # 살아있는 플레이어들의 order_index 목록
        cur = await db.execute(
            """
            SELECT user_id, order_index
            FROM rr_players
            WHERE game_id = ? AND alive = 1
            ORDER BY order_index ASC
            """,
            (game_id,),
        )
        rows = await cur.fetchall()
        alive_players = [(int(r[0]), int(r[1])) for r in rows]
        if not alive_players:
            return None

        # 현재 order_index 이후 첫 번째 alive 플레이어
        larger = [p for p in alive_players if p[1] > cur_order]
        if larger:
            next_user_id = min(larger, key=lambda x: x[1])[0]
            return next_user_id

        # 없다면(현재가 마지막이라면) 가장 order_index가 작은 alive 플레이어
        first_user_id = min(alive_players, key=lambda x: x[1])[0]
        return first_user_id

    async def _pull_trigger(
        self,
        game_id: int,
        user_id: int,
    ) -> tuple[bool, bool, int | None, int]:
        """
        방아쇠를 당기고, 생존 여부/승리 여부/상금 정보를 반환.
        반환: (shot, dead, winner_user_id, prize_amount)

        라운드 기반:
        - 각 라운드마다 6칸 실린더 + 1발
        - 한 라운드에서 누군가 죽으면 라운드 종료
        - 살아있는 사람이 1명 남으면 게임 종료
        - 2명 이상 남으면 새 라운드 시작
        """
        db = await get_db()
        # 상태 조회
        cur = await db.execute(
            """
            SELECT current_turn, cylinder, round_number, shot_in_round
            FROM rr_state
            WHERE game_id = ?
            """,
            (game_id,),
        )
        state_row = await cur.fetchone()
        if state_row is None:
            raise RuntimeError("게임 상태를 찾을 수 없습니다.")
        current_turn, cylinder, round_number, shot_in_round = state_row
        current_turn = int(current_turn)
        cylinder = str(cylinder)
        round_number = int(round_number or 0)
        shot_in_round = int(shot_in_round or 0)

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

        # 이번 라운드에서 몇 번째 발인지 계산
        shot_in_round += 1

        # cylinder에서 현재 칸 확인 (index = shot_in_round - 1)
        idx = shot_in_round - 1
        if idx < 0 or idx >= len(cylinder):
            # 실린더 범위를 넘어갔다는 것은 데이터 이상이므로, 안전하게 빈 클릭 처리
            shot = False
        else:
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

        # 전체 참가자 수
        cur = await db.execute(
            "SELECT COUNT(*) FROM rr_players WHERE game_id = ?",
            (game_id,),
        )
        total_players_row = await cur.fetchone()
        total_players = int(total_players_row[0]) if total_players_row is not None else 0

        # --- 혼자 테스트 모드: 참가자가 1명뿐이면 상금 지급/게임 종료 없이 계속 돌림 ---
        if total_players <= 1:
            # shot=True 이면 alive=0 이지만,
            # 다음 라운드 시작 시 다시 한 명만 남는 상태가 되므로
            # 그냥 "계속 진행" 로직으로 보냄
            pass
        else:
            # 멀티 플레이 모드: 1명만 살아남으면 게임 종료
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

                    # 상금 지급
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

        # 여기까지 왔다는 것은:
        # - shot=False 이거나
        # - shot=True 이지만 아직 2명 이상 살아있어서 게임 계속인 상태

        # shot=True 이면 라운드 종료 → 새 라운드를 시작
        if shot:
            # shot_in_round 업데이트 (마지막 발 기록)
            await db.execute(
                """
                UPDATE rr_state
                SET shot_in_round = ?, last_action_at = CURRENT_TIMESTAMP
                WHERE game_id = ?
                """,
                (shot_in_round, game_id),
            )
            await db.commit()

            # 새 라운드 시작
            await self._start_round(game_id)
            return shot, dead, None, 0

        # shot=False (빈 클릭) 이면, 동일 라운드에서 다음 턴으로 넘어간다.
        # 다음 턴 계산: alive=1 인 사람들 중 현재 사람 다음 order_index
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

        if not order_list:
            # 모두 죽어있는 이상한 상태 -> 그냥 종료 처리
            await db.execute(
                """
                UPDATE rr_games
                SET status = 'FINISHED', finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (game_id,),
            )
            await db.commit()
            return shot, dead, None, 0

        if current_turn not in order_list:
            # (이론상 shot=False 이므로 current_turn 은 alive 목록에 있어야 함)
            # 혹시라도 없으면, 가장 order_index 작은 사람부터
            next_turn = min(order_list)
        else:
            idx_in_alive = order_list.index(current_turn)
            next_turn = order_list[(idx_in_alive + 1) % len(order_list)]

        # rr_state 갱신: current_turn, shot_in_round 증가
        await db.execute(
            """
            UPDATE rr_state
            SET current_turn = ?, shot_in_round = ?, last_action_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
            """,
            (next_turn, shot_in_round, game_id),
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
    )
    async def rr_create(
        self,
        interaction: discord.Interaction,
        entry_fee: int = ENTRY_FEE_DEFAULT,
    ) -> None:
        """게임 생성 (max_players는 항상 6으로 고정)"""
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
                max_players=MAX_PLAYERS_DEFAULT,      # 항상 6
                bullet_count=BULLET_COUNT_DEFAULT,
            )

            await interaction.response.send_message(
                f"🎲 러시안 룰렛 게임을 생성했어요! (ID: `{game_id}`)\n"
                f"- 참가비: **{entry_fee} sats**\n"
                f"- 최대 인원: **{MAX_PLAYERS_DEFAULT}명**\n"
                f"- 탄환 수(라운드당): **{BULLET_COUNT_DEFAULT}발**\n\n"
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
            # game_id 를 선택하지 않은 경우: 이 채널의 WAITING 게임 목록
            waiting_games = await self._get_waiting_games(interaction.channel.id)
            if not waiting_games:
                await interaction.response.send_message(
                    "이 채널에는 대기 중인 러시안 룰렛 게임이 없습니다.\n"
                    "`/rr_create` 로 새 게임을 먼저 만들어 주세요.",
                    ephemeral=True,
                )
                return

            if game_id is None:
                # 가장 최근 게임에 자동 참가
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

            # ---------- 썸네일로 사용할 이미지 URL들 ----------
            BASE = "https://raw.githubusercontent.com/zzeongzi/-lemon-RR/master/assets"
            IMAGE_URL_BANG = f"{BASE}/bang_dead.png"           # 사망
            IMAGE_URL_CLICK = f"{BASE}/empty_click.png"        # 생존

            msg: str
            thumb_url: str | None = None   # 썸네일용

            if winner_user_id is not None:
                # 게임 종료 + 승자 확정
                if shot and dead and winner_user_id != interaction.user.id:
                    msg = (
                        f"💥 **탕! 사망 판정**\n"
                        f"• 사망자: <@{interaction.user.id}>\n"
                        f"• 최후의 생존자: <@{winner_user_id}>\n"
                        f"• 상금: **{prize_amount} sats**"
                    )
                    thumb_url = IMAGE_URL_BANG
                else:
                    msg = (
                        f"🏁 **러시안 룰렛 종료**\n"
                        f"• 최후의 생존자: <@{winner_user_id}>\n"
                        f"• 상금: **{prize_amount} sats**"
                    )
            else:
                # 게임 계속 진행 중
                if shot and dead:
                    msg = (
                        f"💥 **탕! 사망 판정**\n"
                        f"• 사망자: <@{interaction.user.id}>\n"
                        f"• 게임은 계속 진행됩니다...\n"
                        f"(새 라운드가 시작됩니다)"
                    )
                    thumb_url = IMAGE_URL_BANG
                else:
                    # 다음 플레이어 ID 조회
                    next_user_id = await self._get_next_player_id(game_id, interaction.user.id)

                    if next_user_id is not None:
                        msg = (
                            f"🫨 **철컥! 생존**\n"
                            f"• 생존자: <@{interaction.user.id}>\n"
                            f"<@{next_user_id}> 님!\n"
                            f"트리거를 당겨주세요!"
                        )
                    else:
                        msg = (
                            f"🫨 **철컥! 생존**\n"
                            f"• 생존자: <@{interaction.user.id}>\n"
                            f"다음 플레이어 정보를 가져올 수 없습니다."
                        )
                    thumb_url = IMAGE_URL_CLICK

            embed = discord.Embed(
                description=msg,
                color=discord.Color.dark_gold(),
            )

            if thumb_url is not None:
                embed.set_thumbnail(url=thumb_url)

            await interaction.response.send_message(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
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
