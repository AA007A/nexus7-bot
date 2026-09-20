import json, unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from bot import execution_ownership as eo

class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self,*a): return False
class Conn:
    def __init__(self): self.value=None
    def transaction(self): return Tx()
    async def execute(self,sql,*args):
        if "INSERT INTO key_value" in sql: self.value=args[1]
        return "OK"
    async def fetchrow(self,sql,*args):
        return (self.value,) if self.value else None

class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn=Conn()
        self.patches=[
          patch.object(eo.db,"_conn",self.conn),patch.object(eo.db,"_is_pg",True),
          patch.object(eo.db,"configured_postgres_unavailable",return_value=False),
          patch.dict("os.environ",{"EXECUTION_CAPABILITY":"LIVE"},clear=False)]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        for p in reversed(self.patches):p.stop()

    async def test_takeover_monotonically_fences_stale_owner(self):
        a=await eo.acquire_execution_ownership("A")
        state=json.loads(self.conn.value); state["expires_at"]="2000-01-01T00:00:00+00:00"; self.conn.value=json.dumps(state)
        b=await eo.acquire_execution_ownership("B")
        self.assertEqual(b.fencing_token,a.fencing_token+1)
        with self.assertRaises(eo.StaleExecutionFence):
            await eo.validate_execution_ownership(a)
        await eo.validate_execution_ownership(b)

    async def test_shadow_cannot_acquire_live_ownership(self):
        with patch.dict("os.environ",{"EXECUTION_CAPABILITY":"READ_ONLY"},clear=False):
            with self.assertRaises(eo.ExecutionOwnershipUnavailable):
                await eo.acquire_execution_ownership("shadow")

if __name__=="__main__": unittest.main()
