import os
import unittest
from unittest.mock import Mock, patch

from bot.state_authority_observability import (
    configured_database_identity,
    database_authority_fingerprint,
    database_authority_id,
    log_database_authority,
)


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

    def test_stable_non_secret_authority_id_is_explicit(self):
        authority_id = "railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8"
        with patch.dict(
            os.environ, {"DB_AUTHORITY_ID": authority_id}, clear=True
        ):
            self.assertEqual(database_authority_id(), authority_id)

    def test_invalid_authority_id_cannot_inject_log_material(self):
        with patch.dict(
            os.environ, {"DB_AUTHORITY_ID": "service\npassword=secret"}, clear=True
        ):
            self.assertEqual(database_authority_id(), "INVALID")

    def test_configured_identity_omits_host_and_credentials(self):
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgresql://operator:secret@private.host:5432/railway"},
            clear=True,
        ):
            self.assertEqual(configured_database_identity(), ("postgres", "railway"))

    def test_startup_log_contains_complete_sanitized_identity(self):
        authority_id = "railway-service:85019c12-39a2-4ea7-953b-c1dbda898ef8"
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://operator:secret@private.host:5432/railway",
                "DB_AUTHORITY_ID": authority_id,
            },
            clear=True,
        ), patch("bot.state_authority_observability.hashlib.sha256") as digest:
            digest.return_value.hexdigest.return_value = "a" * 64
            logger = Mock()
            log_database_authority(
                logger,
                event="connected",
                backend="postgres",
                database="railway",
                schema="public",
            )

        rendered = str(logger.info.call_args)
        self.assertIn("backend=%s database=%s schema=%s", rendered)
        self.assertIn(authority_id, rendered)
        self.assertIn("railway", rendered)
        self.assertIn("public", rendered)
        self.assertNotIn("operator", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("private.host", rendered)


if __name__ == "__main__":
    unittest.main()
