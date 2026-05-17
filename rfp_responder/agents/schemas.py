from __future__ import annotations

"""Pydantic schemas for all agent I/O."""

from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator


class MakerOutput(BaseModel):
    single_choice_value: str = ""
    answer_text: str
    confidence: Literal["high", "medium", "low"]
    sources_used: list[str]
    needs_review: bool
    review_reason: str = ""

    @field_validator("single_choice_value")
    @classmethod
    def validate_choice(cls, v: str) -> str:
        allowed = {"Yes", "No", "Partial", "N/A", ""}
        if v not in allowed:
            raise ValueError(f"single_choice_value must be one of {allowed}, got {v!r}")
        return v


class ReviewerOutput(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    issues: list[str] = []

    @model_validator(mode="after")
    def cap_issues(self) -> "ReviewerOutput":
        self.issues = self.issues[:3]
        return self


class QuestionResult(BaseModel):
    """Complete result for one RFP question row."""
    row_number: int
    question_number: str
    question_text: str
    question_type: str

    # Written to Excel
    single_choice_value: str = ""
    answer_text: str = ""
    confidence: str = "low"
    top_retrieval_score: float = 0.0
    sources: list[str] = []
    needs_review: bool = True
    review_notes: str = ""
    revision_count: int = 0

    # Audit
    tokens_used: int = 0
    reviewer_called: bool = False
    skip_reason: str = ""
    error: Optional[str] = None
