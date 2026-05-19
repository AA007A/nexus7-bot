"""
KAKAZITO TRADE — Database Layer
Usa PostgreSQL em produção (Railway) e SQLite como fallback local.
Persiste: trades, PnL histórico, decisões do bot, métricas.
"""
import os, time, json, asyncio
from datetime import datetime, timezone
from typing import Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── SQLite fallback se não tiver PostgreSQL ──────────────────────
SQLITE_PATH = os.environ.get("SQLITE_PATH", "/app/data/kakazito.db")

_db = None   # conexão global


async def get_db():
    global _db
    if _db is not None:
        return _db
    if DATABASE_URL and DATABASE_URL.startswith("postgresql"):
        _db = await _init_postgres()
    else:
        _db = await _init_sqlite()
    return _db


async def _init_postgres():
    import asyncpg
    url = DATABASE_URL.replace("postgresql://", "postgresql://")
    conn = await asyncpg.connect(url)
    await _create_tables_pg(conn)
    from bot.logger import log
    log.info("✅ PostgreSQL conectado")
    return conn


async def _init_sqlite():
    import aiosqlite, os
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    conn = await aiosqlite.connect(SQLITE_PATH)
    await _create_tables_sqlite(conn)
    from bot.logger import log
    log.info(f"✅ SQLite conectado: {SQLITE_PATH}")
    return conn


# ── DDL ──────────────────────────────────────────────────────────
TABLES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    entry       REAL    NOT NULL,
    exit_price  REAL,
    qty         REAL    NOT NULL,
    pnl_gross   REAL    DEFAULT 0,
    pnl_net     REAL    DEFAULT 0,
    fees        REAL    DEFAULT 0,
    score       INTEGER DEFAULT 0,
    rr          REAL    DEFAULT 0,
    tf_4h       TEXT,
    tf_1h       TEXT,
    tf_15m      TEXT,
    reason      TEXT,
    status      TEXT    DEFAULT 'open',
    opened_at   TEXT    NOT NULL,
    closed_at   TEXT,
    hold_minutes REAL   DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL UNIQUE,
    pnl_gross   REAL    DEFAULT 0,
    pnl_net     REAL    DEFAULT 0,
    fees        REAL    DEFAULT 0,
    trades      INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    losses      INTEGER DEFAULT 0,
    win_rate    REAL    DEFAULT 0,
    max_dd      REAL    DEFAULT 0,
    sharpe      REAL    DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    score       INTEGER,
    reason      TEXT,
    regime      TEXT,
    rsi         REAL,
    volume_r    REAL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS consecutive_losses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    count       INTEGER DEFAULT 0,
    last_loss   TEXT,
    paused      INTEGER DEFAULT 0
);
"""

TABLES_PG = TABLES_SQL.replace(
    "INTEGER PRIMARY KEY AUTOINCREMENT",
    "SERIAL PRIMARY KEY"
)


async def _create_tables_sqlite(conn):
    import aiosqlite
    for stmt in TABLES_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            await conn.execute(stmt)
    await conn.commit()


async def _create_tables_pg(conn):
    for stmt in TABLES_PG.split(";"):
        stmt = stmt.strip()
        if stmt:
            await conn.execute(stmt)


# ── helpers ──────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc).isoformat()


def _is_pg():
    return DATABASE_URL and DATABASE_URL.startswith("postgresql")


async def _exec(sql, params=()):
    db = await get_db()
    if _is_pg():
        # asyncpg usa $1,$2... em vez de ?
        sql_pg = sql.replace("?", "$%d" % 1)
        i = 1
        new_sql = ""
        for ch in sql:
            if ch == "?":
                new_sql += f"${i}"; i += 1
            else:
                new_sql += ch
        await db.execute(new_sql, *params)
    else:
        await db.execute(sql, params)
        await db.commit()


async def _fetchone(sql, params=()):
    db = await get_db()
    if _is_pg():
        i = [1]
        def rep(ch):
            if ch == "?":
                v = f"${i[0]}"; i[0]+=1; return v
            return ch
        sql = "".join(rep(c) for c in sql)
        return await db.fetchrow(sql, *params)
    else:
        import aiosqlite
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()


async def _fetchall(sql, params=()):
    db = await get_db()
    if _is_pg():
        i = [1]
        def rep(ch):
            if ch == "?":
                v = f"${i[0]}"; i[0]+=1; return v
            return ch
        sql = "".join(rep(c) for c in sql)
        return await db.fetch(sql, *params)
    else:
        import aiosqlite
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


# ── API pública ──────────────────────────────────────────────────
async def save_trade_open(pos) -> int:
    """Persiste posição aberta."""
    sql = """INSERT INTO trades
        (symbol,direction,entry,qty,score,rr,tf_4h,tf_1h,tf_15m,reason,status,opened_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)"""
    params = (
        pos.symbol, pos.direction, pos.entry, pos.qty,
        getattr(pos,"score",0), getattr(pos,"rr",0),
        getattr(pos,"tf_4h",""), getattr(pos,"tf_1h",""), getattr(pos,"tf_15m",""),
        getattr(pos,"reason",""), _now(),
    )
    try:
        await _exec(sql, params)
        row = await _fetchone("SELECT MAX(id) FROM trades")
        return row[0] if row else 0
    except Exception as e:
        from bot.logger import log; log.error(f"DB save_trade_open: {e}")
        return 0


async def save_trade_close(trade_id: int, trade):
    """Atualiza trade fechado com PnL e métricas."""
    closed = _now()
    hold = 0
    try:
        row = await _fetchone("SELECT opened_at FROM trades WHERE id=?", (trade_id,))
        if row:
            from datetime import datetime
            opened = datetime.fromisoformat(row[0])
            hold = (datetime.now(timezone.utc) - opened).total_seconds() / 60
    except Exception:
        pass

    sql = """UPDATE trades SET
        exit_price=?, pnl_gross=?, pnl_net=?, fees=?,
        status='closed', closed_at=?, hold_minutes=?
        WHERE id=?"""
    params = (
        trade.exit_price, trade.pnl_gross, trade.pnl,
        trade.total_fees, closed, hold, trade_id,
    )
    try:
        await _exec(sql, params)
        await _update_daily_stats()
        await _update_loss_streak(trade.pnl)
    except Exception as e:
        from bot.logger import log; log.error(f"DB save_trade_close: {e}")


async def log_decision(symbol, action, score=0, reason="", regime="", rsi=0, vol_r=0):
    """Persiste decisão de análise (HOLD, SIGNAL, etc)."""
    sql = """INSERT INTO decisions
        (symbol,action,score,reason,regime,rsi,volume_r,created_at)
        VALUES (?,?,?,?,?,?,?,?)"""
    try:
        await _exec(sql, (symbol, action, score, reason, regime, round(rsi,1), round(vol_r,2), _now()))
    except Exception:
        pass


async def _update_loss_streak(pnl: float):
    """Atualiza contador de perdas consecutivas."""
    try:
        row = await _fetchone("SELECT count FROM consecutive_losses ORDER BY id DESC LIMIT 1")
        current = row[0] if row else 0
        if pnl < 0:
            new_count = current + 1
        else:
            new_count = 0
        await _exec(
            "INSERT INTO consecutive_losses (count, last_loss, paused) VALUES (?,?,?)",
            (new_count, _now(), 1 if new_count >= 3 else 0)
        )
    except Exception as e:
        from bot.logger import log; log.error(f"DB loss_streak: {e}")


async def get_consecutive_losses() -> int:
    """Retorna número de perdas consecutivas atuais."""
    try:
        row = await _fetchone("SELECT count FROM consecutive_losses ORDER BY id DESC LIMIT 1")
        return row[0] if row else 0
    except Exception:
        return 0


async def _update_daily_stats():
    """Recalcula métricas do dia com Sharpe, Win Rate, Max DD."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = await _fetchall(
            "SELECT pnl_net, pnl_gross, fees FROM trades WHERE status='closed' AND date(closed_at)=?",
            (today,)
        )
        if not rows:
            return

        pnl_nets   = [r[0] for r in rows]
        pnl_gross  = sum(r[1] for r in rows)
        pnl_net    = sum(pnl_nets)
        fees       = sum(r[2] for r in rows)
        trades     = len(rows)
        wins       = sum(1 for p in pnl_nets if p > 0)
        losses     = trades - wins
        win_rate   = wins / trades * 100 if trades > 0 else 0

        # Max Drawdown do dia
        cum = 0; peak = 0; max_dd = 0
        for p in pnl_nets:
            cum += p
            peak = max(peak, cum)
            dd = (peak - cum) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # Sharpe simplificado (média/desvio dos retornos)
        import numpy as np
        if len(pnl_nets) >= 2:
            arr = np.array(pnl_nets)
            sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0
        else:
            sharpe = 0

        sql = """INSERT INTO daily_stats
            (date,pnl_gross,pnl_net,fees,trades,wins,losses,win_rate,max_dd,sharpe,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
            pnl_gross=excluded.pnl_gross, pnl_net=excluded.pnl_net,
            fees=excluded.fees, trades=excluded.trades,
            wins=excluded.wins, losses=excluded.losses,
            win_rate=excluded.win_rate, max_dd=excluded.max_dd,
            sharpe=excluded.sharpe, updated_at=excluded.updated_at"""
        await _exec(sql, (
            today, round(pnl_gross,4), round(pnl_net,4), round(fees,4),
            trades, wins, losses, round(win_rate,1),
            round(max_dd,2), round(sharpe,3), _now()
        ))
    except Exception as e:
        from bot.logger import log; log.error(f"DB _update_daily_stats: {e}")


async def get_stats_summary() -> dict:
    """Retorna métricas completas para o dashboard."""
    try:
        # Hoje
        today = datetime.now(timezone.utc).date().isoformat()
        today_row = await _fetchone("SELECT * FROM daily_stats WHERE date=?", (today,))

        # Total histórico
        all_rows = await _fetchall("SELECT pnl_net, pnl_gross, fees, wins, losses FROM daily_stats")
        total_pnl_net   = sum(r[1] for r in all_rows) if all_rows else 0
        total_pnl_gross = sum(r[2] for r in all_rows) if all_rows else 0
        total_fees      = sum(r[3] for r in all_rows) if all_rows else 0
        total_wins      = sum(r[4] for r in all_rows) if all_rows else 0
        total_losses    = sum(r[5] for r in all_rows) if all_rows else 0
        total_trades    = total_wins + total_losses

        # Últimos 30 trades fechados
        recent = await _fetchall(
            """SELECT symbol,direction,entry,exit_price,pnl_net,fees,score,rr,hold_minutes,closed_at
               FROM trades WHERE status='closed'
               ORDER BY closed_at DESC LIMIT 30"""
        )

        consecutive = await get_consecutive_losses()

        return {
            "today": {
                "pnl_net":   round(today_row[2], 4) if today_row else 0,
                "pnl_gross": round(today_row[1], 4) if today_row else 0,
                "fees":      round(today_row[3], 4) if today_row else 0,
                "trades":    today_row[4] if today_row else 0,
                "wins":      today_row[5] if today_row else 0,
                "losses":    today_row[6] if today_row else 0,
                "win_rate":  round(today_row[7], 1) if today_row else 0,
                "max_dd":    round(today_row[8], 2) if today_row else 0,
                "sharpe":    round(today_row[9], 3) if today_row else 0,
            },
            "total": {
                "pnl_net":   round(total_pnl_net, 4),
                "pnl_gross": round(total_pnl_gross, 4),
                "fees":      round(total_fees, 4),
                "trades":    total_trades,
                "wins":      total_wins,
                "losses":    total_losses,
                "win_rate":  round(total_wins/total_trades*100, 1) if total_trades else 0,
            },
            "consecutive_losses": consecutive,
            "paused_by_losses":   consecutive >= 3,
            "recent_trades": [
                {
                    "symbol":    r[0], "direction": r[1],
                    "entry":     round(r[2],4), "exit": round(r[3],4) if r[3] else None,
                    "pnl_net":   round(r[4],4), "fees": round(r[5],4),
                    "score":     r[6], "rr": round(r[7],2),
                    "hold_min":  round(r[8],1) if r[8] else 0,
                    "closed_at": r[9],
                }
                for r in (recent or [])
            ],
        }
    except Exception as e:
        from bot.logger import log; log.error(f"DB get_stats_summary: {e}")
        return {"today": {}, "total": {}, "consecutive_losses": 0, "recent_trades": []}
