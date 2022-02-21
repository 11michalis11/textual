from __future__ import annotations


import os
import re
from typing import Any, Callable, Generator

from . import log
from . import events
from ._types import MessageTarget
from ._parser import Awaitable, Parser, TokenCallback
from ._ansi_sequences import ANSI_SEQUENCES


_re_mouse_event = re.compile("^" + re.escape("\x1b[") + r"(<?[\d;]+[mM]|M...)\Z")


class XTermParser(Parser[events.Event]):

    _re_sgr_mouse = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def __init__(self, sender: MessageTarget, more_data: Callable[[], bool]) -> None:
        self.sender = sender
        self.more_data = more_data
        self.last_x = 0
        self.last_y = 0

        self._debug_log_file = (
            open("keys.log", "wt") if "TEXTUAL_DEBUG" in os.environ else None
        )

        super().__init__()

    def debug_log(self, *args: Any) -> None:
        if self._debug_log_file is not None:
            self._debug_log_file.write(" ".join(args) + "\n")
            self._debug_log_file.flush()

    def parse_mouse_code(self, code: str, sender: MessageTarget) -> events.Event | None:
        sgr_match = self._re_sgr_mouse.match(code)
        if sgr_match:
            _buttons, _x, _y, state = sgr_match.groups()
            buttons = int(_buttons)
            button = (buttons + 1) & 3
            x = int(_x) - 1
            y = int(_y) - 1
            delta_x = x - self.last_x
            delta_y = y - self.last_y
            self.last_x = x
            self.last_y = y
            event: events.Event
            if buttons & 64:
                event = (
                    events.MouseScrollDown if button == 1 else events.MouseScrollUp
                )(sender, x, y)
            else:
                event = (
                    events.MouseMove
                    if buttons & 32
                    else (events.MouseDown if state == "M" else events.MouseUp)
                )(
                    sender,
                    x,
                    y,
                    delta_x,
                    delta_y,
                    button,
                    bool(buttons & 4),
                    bool(buttons & 8),
                    bool(buttons & 16),
                    screen_x=x,
                    screen_y=y,
                )
            return event
        return None

    def parse(self, on_token: TokenCallback) -> Generator[Awaitable, str, None]:

        ESC = "\x1b"
        read1 = self.read1
        get_ansi_sequence = ANSI_SEQUENCES.get
        more_data = self.more_data

        while not self.is_eof:
            character = yield read1()
            self.debug_log(f"character={character!r}")
            # The more_data is to allow the parse to distinguish between an escape sequence
            # and the escape key pressed
            has_more_data = more_data()
            if character == ESC:
                sequence: str = character
                while (yield self.peek_buffer() or has_more_data):
                    sequence += yield read1()
                    self.debug_log(f"sequence={sequence!r}")
                    keys = get_ansi_sequence(sequence, None)
                    self.debug_log(f"matched ansi sequences: {keys}")
                    if keys is not None:
                        for key in keys:
                            on_token(events.Key(self.sender, key=key))
                        break
                    else:
                        mouse_match = _re_mouse_event.match(sequence)
                        if mouse_match is not None:
                            mouse_code = mouse_match.group(0)
                            event = self.parse_mouse_code(mouse_code, self.sender)
                            if event:
                                on_token(event)
                            break
            else:
                keys = get_ansi_sequence(character, None)
                if keys is not None:
                    for key in keys:
                        on_token(events.Key(self.sender, key=key))
                else:
                    on_token(events.Key(self.sender, key=character))
