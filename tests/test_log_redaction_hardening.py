import importlib
import logging
import os
import unittest

from bot import log_redaction_hardening as redaction


class LogRedactionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.get("KUCOIN_API_KEY")
        self.old_secret = os.environ.get("KUCOIN_API_SECRET")
        self.old_pass = os.environ.get("KUCOIN_API_PASSPHRASE")
        os.environ["KUCOIN_API_KEY"] = "key-123456789"
        os.environ["KUCOIN_API_SECRET"] = "secret-abcdefgh"
        os.environ["KUCOIN_API_PASSPHRASE"] = "pass-987654"

    def tearDown(self):
        for name, value in (
            ("KUCOIN_API_KEY", self.old_key),
            ("KUCOIN_API_SECRET", self.old_secret),
            ("KUCOIN_API_PASSPHRASE", self.old_pass),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_redacts_full_secret_values_and_partial_key_diagnostic(self):
        text = (
            "KuCoin API Key: key-12...6789 (13 chars) "
            "secret-abcdefgh pass-987654"
        )
        safe = redaction.redact_text(text)
        self.assertNotIn("key-12...6789", safe)
        self.assertNotIn("secret-abcdefgh", safe)
        self.assertNotIn("pass-987654", safe)
        self.assertIn("KuCoin API Key: [REDACTED]", safe)

    def test_redacts_signature_prefix(self):
        safe = redaction.redact_text("Assinando request -> sign[:12]=abcdef123456")
        self.assertEqual(safe, "Assinando request -> sign[:12]=[REDACTED]")

    def test_log_record_factory_redacts_format_args(self):
        previous = logging.getLogRecordFactory()
        try:
            redaction.install()
            record = logging.getLogRecordFactory()(
                "test", logging.INFO, __file__, 1,
                "credential=%s", ("secret-abcdefgh",), None
            )
            self.assertEqual(record.getMessage(), "credential=[REDACTED]")
        finally:
            logging.setLogRecordFactory(previous)


if __name__ == "__main__":
    unittest.main()
