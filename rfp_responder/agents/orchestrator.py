from __future__ import annotations

"""Orchestrator: skip-review logic, revision cap, token budget, per-question pipeline."""

import logging
import time
from dataclasses import dataclass

from ..config import AppConfig
from ..retrieval import RetrievedChunk
from .maker import MakerAgent
from .reviewer import ReviewerAgent
from .schemas import MakerOutput, QuestionResult

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    final_output: MakerOutput
    tokens_used: int
    reviewer_called: bool
    revision_count: int
    skip_reason: str
    review_notes: str


class Orchestrator:
    """Implements the Maker → (optional Reviewer) → (optional Revision) pipeline."""

    def __init__(self, maker: MakerAgent, reviewer: ReviewerAgent, cfg: AppConfig):
        self._maker    = maker
        self._reviewer = reviewer
        self._cfg      = cfg

    def process_question(
        self,
        question: str,
        question_type: str,
        chunks: list[RetrievedChunk],
        retrieved_source_names: set[str],
    ) -> OrchestratorResult:
        """
        Run the full maker-reviewer pipeline for one question.
        Returns an OrchestratorResult with the final answer and metadata.
        """
        orch_cfg = self._cfg.orchestration
        ret_cfg  = self._cfg.retrieval
        top_score = chunks[0].score if chunks else 0.0
        tokens_used = 0

        # --- Step 1: Maker draft ---
        draft, tok = self._maker.draft(question, question_type, chunks)
        tokens_used += tok

        # Cross-validate sources_used against actually-retrieved docs
        draft = self._validate_sources(draft, retrieved_source_names, chunks)

        # --- Step 2: Skip-review decision ---
        skip_reason, should_skip = self._should_skip_reviewer(
            draft, top_score, ret_cfg.high_threshold, ret_cfg.low_threshold
        )

        if should_skip or not orch_cfg.skip_reviewer_on_high_confidence:
            # Even if skip_reviewer_on_high_confidence is False, we still honour
            # the low-score skip (no source to validate against)
            if should_skip:
                return OrchestratorResult(
                    final_output=draft,
                    tokens_used=tokens_used,
                    reviewer_called=False,
                    revision_count=0,
                    skip_reason=skip_reason,
                    review_notes="",
                )

        # --- Step 3: Check token budget before calling reviewer ---
        if tokens_used >= orch_cfg.per_question_token_budget:
            logger.warning("Token budget exceeded before reviewer — accepting draft")
            draft.needs_review = True
            return OrchestratorResult(
                final_output=draft,
                tokens_used=tokens_used,
                reviewer_called=False,
                revision_count=0,
                skip_reason="token_budget_exceeded",
                review_notes="Token budget exceeded; draft accepted as-is",
            )

        # --- Step 4: Call reviewer ---
        review, tok = self._reviewer.review(question, chunks, draft)
        tokens_used += tok
        logger.debug("Reviewer verdict: %s | issues: %s", review.verdict, review.issues)

        if review.verdict == "PASS":
            return OrchestratorResult(
                final_output=draft,
                tokens_used=tokens_used,
                reviewer_called=True,
                revision_count=0,
                skip_reason="",
                review_notes="",
            )

        # --- Step 5: Reviewer said FAIL → revise ONCE (hard cap) ---
        review_notes = "; ".join(review.issues)
        logger.info("Reviewer FAIL — requesting one revision. Issues: %s", review_notes)

        if tokens_used >= orch_cfg.per_question_token_budget:
            logger.warning("Token budget hit before revision — accepting original draft")
            draft.needs_review = True
            return OrchestratorResult(
                final_output=draft,
                tokens_used=tokens_used,
                reviewer_called=True,
                revision_count=0,
                skip_reason="token_budget_exceeded_before_revision",
                review_notes=review_notes,
            )

        revised, tok = self._maker.revise(
            question, question_type, chunks, draft, review.issues
        )
        tokens_used += tok
        revised = self._validate_sources(revised, retrieved_source_names, chunks)

        # HARD CAP: accept revised regardless — do NOT call reviewer again
        revised.needs_review = True  # SME should double-check revised answers

        return OrchestratorResult(
            final_output=revised,
            tokens_used=tokens_used,
            reviewer_called=True,
            revision_count=1,
            skip_reason="",
            review_notes=review_notes,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _should_skip_reviewer(
        draft: MakerOutput,
        top_score: float,
        high_threshold: float,
        low_threshold: float,
    ) -> tuple[str, bool]:
        """Return (reason_string, should_skip)."""
        # High confidence path — reviewer cannot add value
        if (
            top_score > high_threshold
            and draft.confidence == "high"
            and not draft.needs_review
        ):
            return "high_confidence_high_score", True

        # Low score path — no source to verify against
        if (
            top_score < low_threshold
            and draft.confidence == "low"
            and draft.needs_review
        ):
            return "low_confidence_low_score", True

        return "", False

    @staticmethod
    def _validate_sources(
        draft: MakerOutput,
        retrieved_source_names: set[str],
        chunks: list[RetrievedChunk],
    ) -> MakerOutput:
        """
        Cross-validate sources_used against actually retrieved docs.
        Replace any hallucinated source names with the real top-3.
        """
        if not retrieved_source_names:
            return draft

        valid = [s for s in draft.sources_used if s in retrieved_source_names]
        if len(valid) < len(draft.sources_used):
            invented = set(draft.sources_used) - retrieved_source_names
            logger.warning("Maker invented source names: %s — replacing with top-3 retrieved", invented)
            top3 = list({c.chunk.source for c in chunks[:3]})
            draft.sources_used = top3

        return draft
