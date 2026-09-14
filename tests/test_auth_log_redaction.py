import logging
import unittest

from bot import auth_log_redaction


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class AuthLogRedactionTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger(f"auth-redaction-test-{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.handler = _Capture()
        self.log.addHandler(self.handler)
        auth_log_redaction.install(self.log)
        self.handler.messages.clear()

    def tearDown(self):
        self.log.handlers.clear()

    def test_partial_api_key_is_not_persisted(self):
        self.log.info("🔑 KuCoin API Key: ABCDEF...WXYZ (24 chars)")
        self.assertEqual(
            self.handler.messages[-1],
            "[AUTH_DIAGNOSTIC] KuCoin API key configured=true",
        )
        self.assertNotIn("ABCDEF", self.handler.messages[-1])
        self.assertNotIn("WXYZ", self.handler.messages[-1])

    def test_secret_and_passphrase_lengths_are_not_persisted(self):
        self.log.info("🔑 API Secret: 36 chars | Passphrase: 10 chars | API version: v2")
        self.assertEqual(
            self.handler.messages[-1],
            "[AUTH_DIAGNOSTIC] KuCoin secret/passphrase configured=true",
        )
        self.assertNotIn("36", self.handler.messages[-1])
        self.assertNotIn("10", self.handler.messages[-1])

    def test_signature_material_is_not_persisted(self):
        self.log.info("🔏 Assinando: '123GET/api/v1/account-overview...' → sign[:12]=SECRETABC123")
        self.assertEqual(
            self.handler.messages[-1],
            "[AUTH_DIAGNOSTIC] KuCoin request signature generated",
        )
        self.assertNotIn("SECRETABC123", self.handler.messages[-1])
        self.assertNotIn("account-overview", self.handler.messages[-1])

    def test_auth_header_values_are_redacted(self):
        self.log.warning("KC-API-KEY=abcdef KC-API-SIGN=signature Authorization=BearerToken")
        msg = self.handler.messages[-1]
        self.assertNotIn("abcdef", msg)
        self.assertNotIn("signature", msg)
        self.assertNotIn("BearerToken", msg)
        self.assertIn("[REDACTED]", msg)


if __name__ == "__main__":
    unittest.main()
