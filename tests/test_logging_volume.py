"""Poll logging must not drown out the app's own diagnostics.

httpx at INFO logged one line per provider request. At a 1 Hz poll with several
requests per poll that was 99.5% of production log output -- 10,224 of 10,272
lines in an hour -- and a 663 MB journal that could not be grepped in
reasonable time while diagnosing a live incident.
"""
from __future__ import annotations

import logging
import unittest

import app


class QuietPollLoggingTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: logging.getLogger(name).level
                       for name in app.QUIET_LOGGERS + ("observatory", "")}

    def tearDown(self):
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_http_client_loggers_are_quietened(self):
        for name in app.QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)
        app.configure_logging()
        for name in app.QUIET_LOGGERS:
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(),
                                    logging.WARNING,
                                    f"{name} would still log every request")

    def test_per_request_lines_are_suppressed(self):
        # assertNoLogs cannot be used here: it sets the logger's level itself
        # for the duration of the block, so it would test the test framework
        # rather than the configuration. isEnabledFor is the real predicate
        # httpx consults before emitting a request line.
        app.configure_logging()
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO),
                         "httpx would still emit a line per request")
        self.assertFalse(logging.getLogger("httpcore").isEnabledFor(logging.DEBUG))

    def test_client_warnings_still_get_through(self):
        # Quietening must not mean silencing: real client problems still log.
        app.configure_logging()
        with self.assertLogs("httpx", level=logging.WARNING) as captured:
            logging.getLogger("httpx").warning("connection pool exhausted")
        self.assertIn("connection pool exhausted", captured.output[0])

    def test_application_logger_is_untouched(self):
        # slow API warnings and collector failures must survive.
        app.configure_logging()
        observatory = logging.getLogger("observatory")
        self.assertLessEqual(observatory.getEffectiveLevel(), logging.INFO)
        with self.assertLogs("observatory", level=logging.WARNING) as captured:
            observatory.warning("slow API sessions: 1641.8 ms (500 rows)")
        self.assertIn("slow API sessions", captured.output[0])

    def test_only_http_client_loggers_are_listed(self):
        # Guard against someone quietening the app's own loggers here later.
        for name in app.QUIET_LOGGERS:
            self.assertIn(name, ("httpx", "httpcore"),
                          "only HTTP client libraries belong in QUIET_LOGGERS")


if __name__ == "__main__":
    unittest.main()
