# db.py
import aiosqlite
import os

from config import DB_PATH

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """
    싱글톤 형태로 aiosqlite DB 커넥션을 반환.
    """
    global _db
    if _db is None:
        if os.path.dirname(DB_PATH):
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await init_db(_db)
    return _db


async def init_db(db: aiosqlite.Connection) -> None:
    """
    필요한 테이블들을 생성한다.
    """
    # users: 유저별 내부 잔액 및 통계
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id INTEGER UNIQUE NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,          -- sats 단위 내부 잔액
            total_spent INTEGER NOT NULL DEFAULT 0,      -- 게임 등으로 사용한 총 sats
            total_won INTEGER NOT NULL DEFAULT 0,        -- 게임 등으로 획득한 총 sats
            win_count INTEGER NOT NULL DEFAULT 0,        -- 승리 횟수
            lose_count INTEGER NOT NULL DEFAULT 0,       -- 패배 횟수
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 러시안 룰렛 게임 테이블
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS rr_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            status TEXT NOT NULL,              -- waiting, playing, finished
            entry_fee INTEGER NOT NULL,        -- 참가비 (sats)
            max_players INTEGER NOT NULL,
            bullet_count INTEGER NOT NULL,
            current_turn_index INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 러시안 룰렛 참가자 테이블
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS rr_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            is_alive INTEGER NOT NULL DEFAULT 1,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (game_id) REFERENCES rr_games (id)
        )
        """
    )

    await db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
