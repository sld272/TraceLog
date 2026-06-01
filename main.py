"""TraceLog top-level entrypoint."""

from __future__ import annotations

import sys

from core.cli import app as cli_app
from core.web import app as web_app


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "cli":
        cli_app.main()
        return 0
    if args and args[0] == "web":
        args = args[1:]
    return web_app.main(args)


if __name__ == "__main__":
    sys.exit(main())
