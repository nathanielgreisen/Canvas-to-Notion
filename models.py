from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CanvasAssignment:
    assignment_id: str | None
    name: str
    course_id: str
    course_code: str
    course_name: str
    due_at: str | None
    html_url: str | None

    @property
    def duplicate_key(self) -> str:
        if self.assignment_id:
            return f"canvas:{self.course_id}:{self.assignment_id}"
        if self.html_url:
            return f"canvas-url:{self.course_id}:{self.html_url}"
        raise ValueError("Canvas assignment has neither an id nor a URL")


@dataclass(frozen=True)
class PreparedAssignment:
    source: CanvasAssignment
    course_value: str
    due_date: datetime | None
