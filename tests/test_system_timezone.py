from __future__ import annotations

import unittest
from datetime import datetime

from core.system_timezone import SYSTEM_TIMEZONE, SYSTEM_TIMEZONE_NAME


class SystemTimezoneTest(unittest.TestCase):
    def test_timezone_matches_system_local_semantics(self) -> None:
        self.assertEqual(
            datetime.now().astimezone().utcoffset(),
            datetime.now(SYSTEM_TIMEZONE).utcoffset(),
        )

    def test_timezone_name_is_available_for_graph(self) -> None:
        self.assertTrue(SYSTEM_TIMEZONE_NAME)
        self.assertNotIn("\\", SYSTEM_TIMEZONE_NAME)


if __name__ == "__main__":
    unittest.main()
