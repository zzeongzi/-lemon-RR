# db.py
import aiosqlite
import os

DB_PATH = os.path.join("data", "rr.sqlite3")

INIT_SQL = """
PRAGMA journal_mode=WAL;

-- 유저 기본 정보 + 캐슈 잔액
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER UNIQUE NOT NULL,
    cashu_balance   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 캐슈 입출금 로그
CREATE TABLE IF NOT EXISTS cashu_transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    type        TEXT NOT NULL, -- 'DEPOSIT' or 'WITHDRAW'
    amount      INTEGER NOT NULL,
    token       TEXT,          -- 전체 토큰 문자열
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- 러시안 룰렛 게임 테이블
CREATE TABLE IF NOT EXISTS rr_games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL,
    host_user_id    INTEGER NOT NULL,
    entry_fee       INTEGER NOT NULL,
    max_players     INTEGER NOT NULL,
    bullet_count    INTEGER NOT NULL,
    status          TEXT NOT NULL, -- 'WAITING', 'RUNNING', 'FINISHED', 'CANCELLED'
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP
);

-- 게임 참가자
CREATE TABLE IF NOT EXISTS rr_players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    order_index INTEGER NOT NULL,  -- 턴 순서
    alive       INTEGER NOT NULL DEFAULT 1, -- 1: 살아있음, 0: 사망
    joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(game_id) REFERENCES rr_games(id)
);

-- 게임 상태 (총알 위치, 현재 턴 등)
CREATE TABLE IF NOT EXISTS rr_state (
    game_id         INTEGER PRIMARY KEY,
    current_turn    INTEGER NOT NULL DEFAULT 0, -- rr_players.order_index
    cylinder        TEXT NOT NULL,  -- 예: "001000" (1: 탄환, 0: 빈방)
    last_action_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(game_id) REFERENCES rr_games(id)
);
"""

async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await db.commit()

async def get_db():
    return await aiosqlite.connect(DB_PATH)
