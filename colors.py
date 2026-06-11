#!/usr/bin/env python3
"""ANSI color helpers for pipeline stdout/stderr messages.

Color is emitted only when the target stream is a TTY (or FORCE_COLOR is set)
and NO_COLOR is unset, so redirected output (e.g. `python orchestrator.py >
out.txt`) stays free of escape codes. Honors the de-facto NO_COLOR /
FORCE_COLOR conventions; to keep color through a pipe or in a file, run with
FORCE_COLOR=1 and view with a pager that renders ANSI (e.g. `less -R`).
"""

import os
import sys

# ANSI SGR codes keyed by name. 'orange' has no basic code, so it uses the
# 256-color palette (208 = orange), which all modern terminals support.
_CODES = {
    'red': '31',
    'green': '32',
    'yellow': '33',
    'blue': '34',
    'magenta': '35',
    'cyan': '36',
    'white': '37',
    'orange': '38;5;208',
}

_RESET = '\x1b[0m'


def _supports_color(stream) -> bool:
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty()


def colorize(text: str, color: str | None, stream=sys.stdout) -> str:
    """Wrap *text* in the ANSI code for *color* if *stream* supports color."""
    code = _CODES.get(color or '')
    if code and _supports_color(stream):
        return f'\x1b[{code}m{text}{_RESET}'
    return text


def cprint(text: str, color: str | None = None, *, stream=sys.stdout) -> None:
    """print() *text* to *stream*, colored with *color* when supported."""
    print(colorize(text, color, stream), file=stream)
