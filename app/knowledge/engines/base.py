"""Shared result type for format writers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class ApplyResult:
    data: bytes
    applied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    diff: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)
