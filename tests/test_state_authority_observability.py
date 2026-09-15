import os
import unittest
from unittest.mock import patch

from bot.state_authority_observability import database_authority_fingerprint


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

    def test_unconfigured_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(database_authority_fingerprint(), "UNCONFIGURED")


if __name__ == "__main__":
    unittest.main()
