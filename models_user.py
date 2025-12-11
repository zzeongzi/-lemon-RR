# models_user.py
from db import get_db


async def get_or_create_user(discord_user_id: int) -> int:
    """유저가 없으면 생성하고, 항상 내부 users.id(int)를 반환"""
    db = await get_db()

    # 기존 유저 조회
    cur = await db.execute(
        "SELECT id FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is not None:
        user_id_raw = row[0]
        if user_id_raw is None:
            # 이 경우는 스키마상 거의 없겠지만, 타입 상 방어
            raise RuntimeError("User id is NULL in users table")
        return int(user_id_raw)

    # 없으면 새 유저 생성
    cur = await db.execute(
        "INSERT INTO users (discord_user_id) VALUES (?)",
        (discord_user_id,),
    )
    await db.commit()

    last_id = cur.lastrowid
    if last_id is None:
        raise RuntimeError("Failed to get lastrowid for users")
    return int(last_id)


async def get_balance(discord_user_id: int) -> int:
    """유저의 현금 잔고(없으면 0)"""
    db = await get_db()

    cur = await db.execute(
        "SELECT cashu_balance FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return 0
    balance_raw = row[0]
    if balance_raw is None:
        return 0
    return int(balance_raw)


async def change_balance(discord_user_id: int, delta: int) -> int:
    """
    유저 잔고를 delta 만큼 증/감.
    부족하면 ValueError.
    최종 잔고(int)를 반환.
    """
    db = await get_db()

    # 현재 유저/잔고 조회
    cur = await db.execute(
        "SELECT id, cashu_balance FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        # 유저 없으면 새로 생성 (잔고 0)
        cur = await db.execute(
            "INSERT INTO users (discord_user_id, cashu_balance) VALUES (?, 0)",
            (discord_user_id,),
        )
        last_id = cur.lastrowid
        if last_id is None:
            raise RuntimeError("Failed to get lastrowid for users")
        user_id = int(last_id)
        balance = 0
    else:
        user_id_raw, balance_raw = row
        if user_id_raw is None:
            raise RuntimeError("User id is NULL in users table")
        user_id = int(user_id_raw)
        balance = int(balance_raw or 0)

    new_balance = balance + delta
    if new_balance < 0:
        raise ValueError("Insufficient balance")

    await db.execute(
        """
        UPDATE users
           SET cashu_balance = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        (new_balance, user_id),
    )
    await db.commit()
    return new_balance


async def log_cashu_tx(
    discord_user_id: int,
    tx_type: str,
    amount: int,
    token: str | None,
) -> None:
    """캐슈 거래 로그 남기기"""
    db = await get_db()

    # 유저 id 조회
    cur = await db.execute(
        "SELECT id FROM users WHERE discord_user_id = ?",
        (discord_user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("User must exist before logging tx")

    user_id_raw = row[0]
    if user_id_raw is None:
        raise RuntimeError("User id is NULL in users table")
    user_id = int(user_id_raw)

    await db.execute(
        """
        INSERT INTO cashu_transactions (user_id, type, amount, token)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, tx_type, amount, token),
    )
    await db.commit()
