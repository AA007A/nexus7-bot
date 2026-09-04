"""Runtime hardening that does not alter strategy, risk, sizing or trading mode.

Two narrow fixes:
1) deterministic/idempotent DB schema bootstrap (tables -> migrations -> indexes),
2) Telegram transport that falls back to plain text on Markdown 400 and never
   blocks the trading coroutine for a long Telegram retry_after.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Set

import aiohttp


def install_database_schema_fix(log):
    from bot import database as db

    if getattr(db, "_schema_hardening_patched", False):
        return

    table_ddl = [
        """CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            timestamp TEXT, strategy TEXT, side TEXT,
            symbol TEXT, entry_price REAL, exit_price REAL,
            size REAL, leverage INTEGER, pnl REAL, fees REAL,
            duration_minutes REAL, score_entrada INTEGER,
            status TEXT DEFAULT 'open', score_features TEXT,
            risk_amount REAL, r_multiple REAL, direction TEXT, exit_reason TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            timestamp TEXT, strategy TEXT, direction TEXT,
            symbol TEXT, score_total INTEGER,
            score_tecnico INTEGER, score_orderflow INTEGER,
            score_macro INTEGER, score_news INTEGER,
            entrou INTEGER DEFAULT 0, motivo_rejeicao TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS risk_events (
            id SERIAL PRIMARY KEY, timestamp TEXT, tipo_evento TEXT,
            descricao TEXT, pnl_acumulado REAL
        )""",
        """CREATE TABLE IF NOT EXISTS news_events (
            id SERIAL PRIMARY KEY, timestamp TEXT, titulo TEXT, fonte TEXT,
            classificacao TEXT, score_confianca REAL, impacto_no_score REAL
        )""",
        """CREATE TABLE IF NOT EXISTS market_snapshots (
            id SERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT,
            open_interest REAL, funding_rate REAL, cvd REAL,
            btc_dominance REAL, fear_greed_index REAL
        )""",
        """CREATE TABLE IF NOT EXISTS performance (
            id SERIAL PRIMARY KEY, periodo TEXT, strategy TEXT,
            win_rate REAL, profit_factor REAL, sharpe_ratio REAL,
            sortino_ratio REAL, max_drawdown REAL, expectancy_por_trade REAL,
            total_trades INTEGER, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS consecutive_losses (
            id SERIAL PRIMARY KEY, count INTEGER DEFAULT 0, last_loss TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS key_value (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY, timestamp TEXT, symbol TEXT,
            type TEXT, score INTEGER, reason TEXT
        )""",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
        "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_id ON decisions(id)",
    ]
    trade_columns = {
        "score_features": "TEXT",
        "risk_amount": "REAL",
        "r_multiple": "REAL",
        "direction": "TEXT",
        "exit_reason": "TEXT",
    }

    async def _execute(stmt: str):
        if db._is_pg:
            await db._conn.execute(stmt)
        else:
            stmt = stmt.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            await db._conn.execute(stmt)
            await db._conn.commit()

    async def _trade_columns_present() -> Set[str]:
        if db._is_pg:
            rows = await db._conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='trades'"
            )
            return {str(r["column_name"]) for r in rows}
        async with db._conn.execute("PRAGMA table_info(trades)") as cur:
            rows = await cur.fetchall()
        return {str(r[1]) for r in rows}

    async def _create_tables_hardened():
        if not db._conn:
            return
        for stmt in table_ddl:
            await _execute(stmt)

        present = await _trade_columns_present()
        for column, sql_type in trade_columns.items():
            if column not in present:
                await _execute(f"ALTER TABLE trades ADD COLUMN {column} {sql_type}")

        for stmt in indexes:
            await _execute(stmt)

        backend = "PostgreSQL" if db._is_pg else "SQLite"
        log.info("✅ DB schema verificado (%s): tabelas/migrações/índices OK", backend)

    db._create_tables = _create_tables_hardened
    db._schema_hardening_patched = True


def install_telegram_fix(log):
    from bot import notifier
    from bot.config import cfg
    import bot.engine as engine_module

    if getattr(notifier, "_transport_hardening_patched", False):
        return

    lock = asyncio.Lock()
    last_send = [0.0]
    seen = {}
    pending = set()
    min_interval = 3.0
    dedup_window = 60.0

    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]

    async def _post(text: str, parse_mode=None):
        url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": cfg.TELEGRAM_CHAT, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                )
                body = None
                if resp.status != 200:
                    try:
                        body = await resp.json()
                    except Exception:
                        try:
                            body = {"description": (await resp.text())[:200]}
                        except Exception:
                            body = {}
                return resp.status, body or {}
        except Exception as exc:
            log.debug("Telegram transport: %s", type(exc).__name__)
            return 0, {}

    async def _delayed_retry(text: str, wait_s: int, key: str):
        try:
            await asyncio.sleep(max(1, int(wait_s)))
            status, _ = await _post(text, "Markdown")
            if status == 400:
                await _post(text, None)
        finally:
            pending.discard(key)

    async def robust_notify(text: str):
        if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT:
            return

        key = _key(text)
        now = time.monotonic()
        if now - seen.get(key, 0.0) < dedup_window:
            return
        seen[key] = now
        if len(seen) > 1000:
            cutoff = now - dedup_window * 2
            for k, ts in list(seen.items()):
                if ts < cutoff:
                    seen.pop(k, None)

        async with lock:
            delay = min_interval - (time.monotonic() - last_send[0])
            if 0 < delay <= min_interval:
                await asyncio.sleep(delay)

            status, data = await _post(text, "Markdown")
            if status == 200:
                last_send[0] = time.monotonic()
                return

            # Markdown entity failures: immediately retry as plain text.
            if status == 400:
                status2, _ = await _post(text, None)
                if status2 == 200:
                    last_send[0] = time.monotonic()
                    log.info("✅ Telegram fallback plain-text aplicado")
                else:
                    log.warning("Telegram fallback falhou HTTP %s", status2)
                return

            # Flood control: schedule retry in background; never sleep hundreds
            # of seconds in the trading coroutine.
            if status == 429:
                retry_after = int((data.get("parameters") or {}).get("retry_after", 10))
                if key not in pending and len(pending) < 20:
                    pending.add(key)
                    asyncio.create_task(_delayed_retry(text, retry_after, key))
                log.debug("Telegram 429: retry agendado em background (%ss)", retry_after)
                return

            if status in (401, 403):
                log.error("Telegram HTTP %s: credencial/chat recusado", status)
            elif status:
                log.debug("Telegram HTTP %s", status)

    notifier.notify = robust_notify
    # engine.py imported notify by value; replace that bound global as well.
    engine_module.notify = robust_notify
    notifier._transport_hardening_patched = True
