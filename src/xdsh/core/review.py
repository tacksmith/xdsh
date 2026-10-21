"""Runner-neutral workspace review seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class WorkspaceReviewError(RuntimeError):
    """A workspace review failed without exposing source or credentials."""


@dataclass(frozen=True, slots=True)
class WorkspaceReview:
    """One no-tools review result and its bounded evidence coverage."""

    markdown: str
    files_reviewed: int
    evidence_characters: int
    truncated: bool = False


class WorkspaceReviewer(Protocol):
    """Review a bounded workspace snapshot without granting tools."""

    async def review(self, *, workspace: Path, scope: str | None = None) -> WorkspaceReview: ...
