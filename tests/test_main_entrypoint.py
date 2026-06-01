import unittest
from unittest.mock import patch

import main


class MainEntrypointTest(unittest.TestCase):
    def test_default_starts_web(self) -> None:
        with patch("main.web_app.main", return_value=0) as web_main:
            code = main.main([])

        self.assertEqual(0, code)
        web_main.assert_called_once_with([])

    def test_web_command_starts_web_and_passes_args(self) -> None:
        with patch("main.web_app.main", return_value=0) as web_main:
            code = main.main(["web", "--frontend-port", "5174"])

        self.assertEqual(0, code)
        web_main.assert_called_once_with(["--frontend-port", "5174"])

    def test_cli_command_starts_cli(self) -> None:
        with patch("main.cli_app.main") as cli_main:
            code = main.main(["cli"])

        self.assertEqual(0, code)
        cli_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
