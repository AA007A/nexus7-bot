"""Real isolated-PostgreSQL proof for the durable pilot cross-worker budget."""
import asyncio
import json
import multiprocessing as mp
import os
import unittest


def _worker(dsn, token, start, out):
    async def run():
        import asyncpg
        from bot import database as db
        from bot import pilot_submission_counter as counter
        conn = await asyncpg.connect(dsn)
        class Lock:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        db._conn, db._is_pg, db._io_lock = conn, True, Lock()
        db.configured_postgres_unavailable = lambda: False
        start.wait(10)
        try:
            result = await counter._reserve_db(token, 1)
            out.put(("ok", result))
        except Exception as exc:
            out.put(("error", type(exc).__name__))
        finally:
            await conn.close()
    asyncio.run(run())


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "isolated PostgreSQL required")
class PilotPostgresCrossWorkerProof(unittest.TestCase):
    def test_only_one_worker_consumes_last_slot_and_restart_preserves_it(self):
        import asyncpg
        dsn = os.environ["TEST_POSTGRES_DSN"]
        os.environ["PILOT_SESSION_ID"] = "release-proof-stable-session"

        async def setup():
            conn = await asyncpg.connect(dsn)
            await conn.execute("""CREATE TABLE IF NOT EXISTS key_value (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT
            )""")
            await conn.execute("DELETE FROM key_value WHERE key LIKE 'pilot_submission_budget_v1:%'")
            await conn.close()
        asyncio.run(setup())

        ctx = mp.get_context("spawn")
        start, out = ctx.Event(), ctx.Queue()
        a = ctx.Process(target=_worker, args=(dsn, "intent-A", start, out))
        b = ctx.Process(target=_worker, args=(dsn, "intent-B", start, out))
        a.start(); b.start(); start.set()
        results = [out.get(timeout=20), out.get(timeout=20)]
        a.join(20); b.join(20)
        self.assertEqual(a.exitcode, 0); self.assertEqual(b.exitcode, 0)
        allowed = [r[1][0] for r in results if r[0] == "ok"]
        self.assertEqual(sorted(allowed), [False, True])

        async def verify_restart():
            import asyncpg
            from bot import pilot_submission_counter as counter
            conn = await asyncpg.connect(dsn)
            row = await conn.fetchrow("SELECT value FROM key_value WHERE key=$1", counter._state_key())
            tokens = json.loads(row[0])["order_tokens"]
            await conn.close()
            return tokens
        tokens = asyncio.run(verify_restart())
        self.assertEqual(len(tokens), 1)


if __name__ == "__main__":
    unittest.main()
