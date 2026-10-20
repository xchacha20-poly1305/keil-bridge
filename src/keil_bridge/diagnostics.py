"""Warnings and errors collected during a run, reported together at the end."""

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import NoReturn


class Level(Enum):
    NOTE = "note"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Diagnostic:
    level: Level
    message: str
    path: str | None = None
    line: int | None = None
    # Option name this diagnostic is about. An override of that option makes
    # the diagnostic obsolete, so it is dropped (see Diagnostics.drop_fields).
    option: str | None = None

    def location(self) -> str:
        if self.path is None:
            return ""
        if self.line is None:
            return f"{self.path}: "
        return f"{self.path}:{self.line}: "

    def __str__(self) -> str:
        return f"{self.level.value}: {self.location()}{self.message}"


ABORTED = "aborted because of the errors above"


class KeilBridgeError(Exception):
    """Raised when a run cannot continue; diagnostics carry the details."""


@dataclass
class Diagnostics:
    items: list[Diagnostic] = field(default_factory=list)

    def note(self, message, path=None, line=None, option=None):
        self.items.append(Diagnostic(Level.NOTE, message, path, line, option))

    def warn(self, message, path=None, line=None, option=None):
        self.items.append(Diagnostic(Level.WARNING, message, path, line, option))

    def error(self, message, path=None, line=None, option=None):
        self.items.append(Diagnostic(Level.ERROR, message, path, line, option))

    def fatal(self, message, path=None, line=None) -> NoReturn:
        """Record an error and stop the run."""
        self.error(message, path, line)
        raise KeilBridgeError(ABORTED)

    def error_count(self) -> int:
        return sum(1 for d in self.items if d.level is Level.ERROR)

    @property
    def has_errors(self) -> bool:
        return self.error_count() > 0

    def drop_fields(self, overridden: set[str]):
        self.items = [d for d in self.items if d.option is None or d.option not in overridden]

    def check(self) -> None:
        """Stop the run if any error has been recorded."""
        if self.has_errors:
            raise KeilBridgeError(ABORTED)

    def print(self, stream=None):
        for d in self.items:
            print(d, file=stream or sys.stderr)
