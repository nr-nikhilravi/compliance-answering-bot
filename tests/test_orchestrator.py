"""Tests for Orchestrator anti-loop and skip-review logic."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rfp_responder.config import AppConfig
from rfp_responder.agents.orchestrator import Orchestrator
from rfp_responder.agents.schemas import MakerOutput, ReviewerOutput
from rfp_responder.retrieval import RetrievedChunk
from rfp_responder.chunking import TextChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(score: float, source: str = "doc.pdf") -> RetrievedChunk:
    return RetrievedChunk(
        chunk=TextChunk(text="Some context text.", source=source),
        score=score,
    )


def _make_maker_output(**kwargs) -> MakerOutput:
    defaults = dict(
        single_choice_value="Yes",
        answer_text="Test answer.",
        confidence="high",
        sources_used=["doc.pdf"],
        needs_review=False,
        review_reason="",
    )
    defaults.update(kwargs)
    return MakerOutput(**defaults)


def _make_reviewer_output(verdict: str, issues: list[str] | None = None) -> ReviewerOutput:
    return ReviewerOutput(verdict=verdict, issues=issues or [])


def _build_orch(cfg: AppConfig | None = None) -> tuple[Orchestrator, MagicMock, MagicMock]:
    if cfg is None:
        cfg = AppConfig()
    maker    = MagicMock()
    reviewer = MagicMock()
    orch     = Orchestrator(maker, reviewer, cfg)
    return orch, maker, reviewer


# ---------------------------------------------------------------------------
# Branch 1: High retrieval + high confidence → reviewer SKIPPED
# ---------------------------------------------------------------------------

class TestHighConfidenceSkipReviewer:
    def test_reviewer_not_called(self):
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.90)]  # > 0.75 threshold

        draft = _make_maker_output(confidence="high", needs_review=False)
        maker.draft.return_value = (draft, 100)

        result = orch.process_question(
            question="Q?",
            question_type="Comment",
            chunks=chunks,
            retrieved_source_names={"doc.pdf"},
        )

        reviewer.review.assert_not_called()
        assert result.reviewer_called is False
        assert result.skip_reason == "high_confidence_high_score"
        assert result.revision_count == 0
        assert result.final_output.answer_text == "Test answer."


# ---------------------------------------------------------------------------
# Branch 2: Low retrieval + low confidence → reviewer SKIPPED
# ---------------------------------------------------------------------------

class TestLowConfidenceSkipReviewer:
    def test_reviewer_not_called_on_low_signal(self):
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.10)]  # < 0.30 threshold

        draft = _make_maker_output(confidence="low", needs_review=True)
        maker.draft.return_value = (draft, 80)

        result = orch.process_question(
            question="Q?",
            question_type="Comment",
            chunks=chunks,
            retrieved_source_names={"doc.pdf"},
        )

        reviewer.review.assert_not_called()
        assert result.reviewer_called is False
        assert result.skip_reason == "low_confidence_low_score"


# ---------------------------------------------------------------------------
# Branch 3: Medium retrieval → reviewer CALLED
# ---------------------------------------------------------------------------

class TestMediumRetrievalReviewerCalled:
    def test_reviewer_called_on_medium_score(self):
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.55)]  # in 0.30–0.75 range

        draft = _make_maker_output(confidence="medium")
        maker.draft.return_value = (draft, 200)

        review = _make_reviewer_output("PASS")
        reviewer.review.return_value = (review, 50)

        result = orch.process_question(
            question="Q?",
            question_type="Comment",
            chunks=chunks,
            retrieved_source_names={"doc.pdf"},
        )

        reviewer.review.assert_called_once()
        assert result.reviewer_called is True
        assert result.revision_count == 0


# ---------------------------------------------------------------------------
# Branch 4: Reviewer FAIL → exactly ONE revision, then accept (no second review)
# ---------------------------------------------------------------------------

class TestReviewerFailExactlyOneRevision:
    def test_one_revision_then_accept(self):
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.55)]

        draft = _make_maker_output(confidence="medium")
        revised = _make_maker_output(
            confidence="medium",
            answer_text="Revised answer.",
            needs_review=True,
        )
        maker.draft.return_value  = (draft, 200)
        maker.revise.return_value = (revised, 150)

        review_fail = _make_reviewer_output("FAIL", ["Issue A", "Issue B"])
        reviewer.review.return_value = (review_fail, 50)

        result = orch.process_question(
            question="Q?",
            question_type="Comment",
            chunks=chunks,
            retrieved_source_names={"doc.pdf"},
        )

        # Reviewer called once, maker revised once, reviewer NOT called again
        reviewer.review.assert_called_once()
        maker.revise.assert_called_once()
        assert result.revision_count == 1
        assert result.reviewer_called is True
        assert result.final_output.answer_text == "Revised answer."
        # Review notes must capture the original issues
        assert "Issue A" in result.review_notes

    def test_no_second_review_after_revision(self):
        """Under no circumstances should the reviewer be called after a revision."""
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.55)]

        draft = _make_maker_output(confidence="medium")
        revised = _make_maker_output(confidence="medium", answer_text="Rev.")
        maker.draft.return_value  = (draft, 200)
        maker.revise.return_value = (revised, 100)

        reviewer.review.return_value = (_make_reviewer_output("FAIL", ["X"]), 40)

        orch.process_question("Q?", "Comment", chunks, {"doc.pdf"})

        # reviewer.review should have been called exactly once (not twice)
        assert reviewer.review.call_count == 1


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------

class TestSourceValidation:
    def test_invented_sources_replaced(self):
        orch, maker, reviewer = _build_orch()
        chunks = [_make_chunk(0.90, source="real_doc.pdf")]

        # Maker claims a source not in the retrieved set
        draft = _make_maker_output(
            confidence="high",
            needs_review=False,
            sources_used=["invented_source.pdf"],  # NOT in retrieved set
        )
        maker.draft.return_value = (draft, 100)

        result = orch.process_question(
            question="Q?",
            question_type="Comment",
            chunks=chunks,
            retrieved_source_names={"real_doc.pdf"},
        )

        # sources_used should be replaced with the actual top retrieved doc
        assert "invented_source.pdf" not in result.final_output.sources_used
        assert "real_doc.pdf" in result.final_output.sources_used


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_invalid_single_choice_value_rejected(self):
        with pytest.raises(Exception):
            MakerOutput(
                single_choice_value="Maybe",  # invalid
                answer_text="...",
                confidence="high",
                sources_used=[],
                needs_review=False,
            )

    def test_invalid_confidence_rejected(self):
        with pytest.raises(Exception):
            MakerOutput(
                single_choice_value="Yes",
                answer_text="...",
                confidence="very_high",  # invalid
                sources_used=[],
                needs_review=False,
            )

    def test_reviewer_issues_capped_at_3(self):
        out = ReviewerOutput(verdict="FAIL", issues=["a", "b", "c", "d", "e"])
        assert len(out.issues) == 3
