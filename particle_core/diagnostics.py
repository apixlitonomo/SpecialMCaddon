"""Structured diagnostics for the renderer-independent particle pipeline.

The core deliberately reports recoverable format problems instead of printing or
silently guessing.  Blender integration can display these records in its UI,
while tests and batch callers can treat selected severities as fatal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Iterator, Optional, Tuple


class Severity(IntEnum):
    """Diagnostic severity ordered from least to most serious."""

    INFO = 10
    WARNING = 20
    ERROR = 30
    FATAL = 40


@dataclass(frozen=True)
class SourceRef:
    """A stable source URI plus an optional RFC 6901-style JSON pointer."""

    uri: str = "<memory>"
    pointer: str = ""
    line: Optional[int] = None
    column: Optional[int] = None

    def child(self, *parts: object) -> "SourceRef":
        pointer = self.pointer
        for part in parts:
            escaped = str(part).replace("~", "~0").replace("/", "~1")
            pointer += "/" + escaped
        return SourceRef(self.uri, pointer, self.line, self.column)

    def display(self) -> str:
        location = self.uri + self.pointer
        if self.line is not None:
            location += ":%d" % self.line
            if self.column is not None:
                location += ":%d" % self.column
        return location


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity = Severity.WARNING
    source: SourceRef = SourceRef()
    hint: str = ""


class DiagnosticBag:
    """Mutable collector with an immutable public snapshot."""

    def __init__(self, initial: Iterable[Diagnostic] = ()) -> None:
        self._items = list(initial)

    def add(
        self,
        code: str,
        message: str,
        severity: Severity = Severity.WARNING,
        source: Optional[SourceRef] = None,
        hint: str = "",
    ) -> Diagnostic:
        diagnostic = Diagnostic(
            code=code,
            message=message,
            severity=severity,
            source=source or SourceRef(),
            hint=hint,
        )
        self._items.append(diagnostic)
        return diagnostic

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        self._items.extend(diagnostics)

    def snapshot(self) -> Tuple[Diagnostic, ...]:
        return tuple(self._items)

    def has_at_least(self, severity: Severity) -> bool:
        return any(item.severity >= severity for item in self._items)

    @property
    def has_errors(self) -> bool:
        return self.has_at_least(Severity.ERROR)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

