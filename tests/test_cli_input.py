from __future__ import annotations

import unittest

from core.cli_input import _display_width


class CliInputTest(unittest.TestCase):
    def test_display_width_counts_cjk_as_double_width(self) -> None:
        self.assertEqual(4, _display_width("你: "))
        self.assertEqual(7, _display_width("测试abc"))

    def test_display_width_ignores_combining_marks(self) -> None:
        self.assertEqual(1, _display_width("e\u0301"))


if __name__ == "__main__":
    unittest.main()
