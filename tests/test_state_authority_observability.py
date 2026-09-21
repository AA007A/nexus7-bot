import os
import unittest
from unittest.mock import patch

from bot.state_authority_observability import database_authority_fingerprint, database_authority_id


class StateAuthorityObservabilityTests(unittest.TestCase):
    def test_same_endpoint_different_credentials_has_same_fingerprint(self):
        a = "postgresql://alice:secret-a@db.internal:5432/nexus"
        b = "postgresql://bob:secret-b@db.internal:5432/nexus"
        with patch.dict(os.environ, {"DATABASE_URL": a}, clear=False):
            fa = database_authority_fingerprint()
        with patch.dict(os.environ, {"DATABASE_URL": b}, clear=False):
            fb = database_authority_fingerprint()
        self.assertEqual(fa, fb)
        self.assertEqual(len(fa), 16)
        self.assertNotIn("secret", fa)
        self.assertNotIn("alice", fa)

    def test_different_database_authority_changes_fingerprint(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@db-a:5432/nexus"}, clear=False):
            fa = database_authority_fingerprint()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://u:p@db-b:5432/nexus"}, clear=False):
            fb = database_authority_fingerprint()
        self.assertNotEqual(fa, fb)

    def test_authority_id_is_explicit_and_non_secret(self):
        with patch.dict(
            os.environ,
            {"DB_AUTHORITY_ID": "railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8"},
            clear=False,
        ):
            self.assertEqual(
                database_authority_id(),
                "railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8",
            )

    def test_unconfigured_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(database_authority_fingerprint(), "UNCONFIGURED")
            self.assertEqual(database_authority_id(), "UNCONFIGURED")


if __name__ == "__main__":
    unittest.main()
