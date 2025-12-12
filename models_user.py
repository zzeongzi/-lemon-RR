# models_user.py
from typing import Optional

from db import get_db


async def get_or_create_user(discord_user_id: int) -> int:
    """
    discord_user_id 에 해당하는 users 레코드를 가져오거나 생성.
    반환: users.id (내부 PK)
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT id FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()

    if row is not None:
        return int(row["id"])

    await db.execute(
        "INSERT INTO users (discord_user_id, balance) VALUES (?, ?)",
        (discord_user_id, 0),
    )
    await db.commit()

    cur = await db.execute(
        "SELECT id FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row2 = await cur.fetchone()
    if row2 is None:
        raise RuntimeError("유저 생성 후에도 레코드를 찾을 수 없습니다.")
    return int(row2["id"])


async def get_balance(discord_user_id: int) -> int:
    """
    유저의 내부 잔액(sats)을 반환.
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT balance FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        await get_or_create_user(discord_user_id)
        return 0
    return int(row["balance"])


async def change_balance(discord_user_id: int, diff_sats: int) -> int:
    """
    유저 잔액을 diff_sats 만큼 증감시키고, 변경된 잔액을 반환.
    (음수 diff_sats 는 차감)
    """
    db = await get_db()
    await get_or_create_user(discord_user_id)

    cur = await db.execute(
        "SELECT balance FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        current = 0
    else:
        current = int(row["balance"])

    new_balance = current + diff_sats
    if new_balance < 0:
        raise ValueError("잔액이 부족합니다.")

    await db.execute(
        "UPDATE users SET balance = ? WHERE discord_user_id = ?",
        (new_balance, discord_user_id),
    )
    await db.commit()
    return new_balance


async def add_game_result(
    discord_user_id: int,
    spent_sats: int = 0,
    won_sats: int = 0,
    win: Optional[bool] = None,
) -> None:
    """
    게임 결과(사용/획득 sats, 승패)를 누적한다.
    """
    db = await get_db()
    await get_or_create_user(discord_user_id)

    # 총 사용/획득 sats 반영
    await db.execute(
        """
        UPDATE users
        SET total_spent = total_spent + ?,
            total_won = total_won + ?
        WHERE discord_user_id = ?
        """,
        (spent_sats, won_sats, discord_user_id),
    )

    # 승패 카운트 반영
    if win is True:
        await db.execute(
            "UPDATE users SET win_count = win_count + 1 WHERE discord_user_id = ?",
            (discord_user_id,),
        )
    elif win is False:
        await db.execute(
            "UPDATE users SET lose_count = lose_count + 1 WHERE discord_user_id = ?",
            (discord_user_id,),
        )

    await db.commit()
