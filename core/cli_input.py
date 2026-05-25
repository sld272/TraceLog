"""Unicode-aware terminal input for TraceLog CLI."""

from __future__ import annotations

import sys
import termios
import tty
import unicodedata
from shutil import get_terminal_size


def read_cli_input(prompt: str) -> str:
    """Read one input line, redrawing explicitly so CJK backspace stays sane."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)

    old_settings = termios.tcgetattr(sys.stdin)
    editor = _LineEditor(prompt)
    try:
        tty.setraw(sys.stdin.fileno())
        return editor.read()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


class _LineEditor:
    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self.buffer: list[str] = []
        self.cursor = 0
        self._rendered_rows = 0
        self._cursor_row = 0

    def read(self) -> str:
        self._redraw()
        while True:
            char = sys.stdin.read(1)
            if char in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(self.buffer)
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x04":
                if not self.buffer:
                    raise EOFError
                continue
            if char in ("\x7f", "\b"):
                if self.cursor > 0:
                    del self.buffer[self.cursor - 1]
                    self.cursor -= 1
                    self._redraw()
                continue
            if char == "\x15":
                del self.buffer[:self.cursor]
                self.cursor = 0
                self._redraw()
                continue
            if char == "\x01":
                self.cursor = 0
                self._redraw()
                continue
            if char == "\x05":
                self.cursor = len(self.buffer)
                self._redraw()
                continue
            if char == "\x1b":
                self._handle_escape()
                continue
            if char < " ":
                continue

            self.buffer.insert(self.cursor, char)
            self.cursor += 1
            self._redraw()

    def _handle_escape(self) -> None:
        second = sys.stdin.read(1)
        if second != "[":
            return
        third = sys.stdin.read(1)
        if third == "D" and self.cursor > 0:
            self.cursor -= 1
            self._redraw()
        elif third == "C" and self.cursor < len(self.buffer):
            self.cursor += 1
            self._redraw()
        elif third == "H":
            self.cursor = 0
            self._redraw()
        elif third == "F":
            self.cursor = len(self.buffer)
            self._redraw()
        elif third in ("3", "1", "4"):
            tail = sys.stdin.read(1)
            if third == "3" and tail == "~" and self.cursor < len(self.buffer):
                del self.buffer[self.cursor]
                self._redraw()
            elif third == "1" and tail == "~":
                self.cursor = 0
                self._redraw()
            elif third == "4" and tail == "~":
                self.cursor = len(self.buffer)
                self._redraw()

    def _redraw(self) -> None:
        text = "".join(self.buffer)
        width = max(1, get_terminal_size(fallback=(80, 24)).columns)
        total_width = _display_width(self.prompt + text)
        cursor_width = _display_width(self.prompt) + _display_width("".join(self.buffer[: self.cursor]))
        rendered_rows = max(1, (total_width // width) + 1)
        cursor_row = min(rendered_rows - 1, cursor_width // width)
        cursor_col = cursor_width % width

        self._clear_previous_render()
        sys.stdout.write(self.prompt + text)
        self._move_to_start()
        if cursor_row:
            sys.stdout.write(f"\x1b[{cursor_row}B")
        if cursor_col:
            sys.stdout.write(f"\x1b[{cursor_col}C")
        sys.stdout.flush()
        self._rendered_rows = rendered_rows
        self._cursor_row = cursor_row

    def _clear_previous_render(self) -> None:
        if self._rendered_rows == 0:
            return
        self._move_to_start()
        for row in range(self._rendered_rows):
            sys.stdout.write("\x1b[2K")
            if row < self._rendered_rows - 1:
                sys.stdout.write("\x1b[1B")
        sys.stdout.write("\r")
        if self._rendered_rows > 1:
            sys.stdout.write(f"\x1b[{self._rendered_rows - 1}A")

    def _move_to_start(self) -> None:
        sys.stdout.write("\r")
        if self._cursor_row:
            sys.stdout.write(f"\x1b[{self._cursor_row}A")


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width
