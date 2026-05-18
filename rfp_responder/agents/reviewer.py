from __future__ import annotations

"""Reviewer agent — validates maker drafts using Gemini 2.5 Flash."""

import json
import logging
from pathlib import Path

from openai import OpenAI

from ..retrieval import RetrievedChunk
from .schemas import MakerOutput, ReviewerOutput

logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _build_system_prompt(prompts_dir: Path) -> str:
    tpl_path = prompts_dir / "reviewer_system.md"
    if tpl_path.exists():
        return tpl_path.read_text(encoding="utf-8")
    return _DEFAULT_REVIEWER_SYSTEM


def _format_context(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, rc in enumerate(chunks, start=1):
        meta = f"[Source: {rc.chunk.source}"
        if rc.chunk.page:
            meta += f", Page {rc.chunk.page}"
        meta += f"]"
        parts.append(f"--- Chunk {i} {meta} ---\n{rc.chunk.text}")
    return "\n\n".join(parts)


def _parse_output(raw: str) -> ReviewerOutput | None:
    raw = raw.strip()
    
    # Try to extract just the JSON object if there's conversational text
    start_idx = raw.find('{')
    end_idx = raw.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
        raw = raw[start_idx:end_idx + 1]
        
    try:
        data = json.loads(raw)
        return ReviewerOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ReviewerOutput parse failed: %s", exc)
        return None


import litellm
from .utils import call_llm_with_retries

class ReviewerAgent:
    def __init__(self, api_key: str, model: str, prompts_dir: Path, provider: str = "gemini"):
        self._api_key = api_key
        
        # Add provider prefix for litellm
        if provider == "gemini" and not model.startswith("gemini/"):
            self._model = f"gemini/{model}"
        elif provider == "openrouter" and not model.startswith("openrouter/"):
            self._model = f"openrouter/{model}"
        elif provider == "claude" and not model.startswith("anthropic/"):
            self._model = f"anthropic/{model}"
        elif provider == "openai" and not model.startswith("openai/"):
            self._model = f"openai/{model}"
        else:
            self._model = model

        self._system_prompt = _build_system_prompt(prompts_dir)

    def review(
        self,
        question: str,
        chunks: list[RetrievedChunk],
        draft: MakerOutput,
    ) -> tuple[ReviewerOutput, int]:
        """Return (ReviewerOutput, tokens_used)."""
        context = _format_context(chunks)
        user_msg = (
            f"RFP QUESTION:\n{question}\n\n"
            f"RETRIEVED CONTEXT (same as maker received):\n{context}\n\n"
            f"MAKER'S DRAFT ANSWER:\n{draft.answer_text}\n"
            f"Single Choice Value: {draft.single_choice_value}\n"
            f"Confidence: {draft.confidence}\n"
            f"Sources cited: {', '.join(draft.sources_used)}"
        )
        response = call_llm_with_retries(
            model=self._model,
            api_key=self._api_key,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user",   "content": user_msg},
            ],
        )
        raw = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if hasattr(response, "usage") and response.usage else 0
        result = _parse_output(raw)
        if result is None:
            # Retry once
            retry = call_llm_with_retries(
                model=self._model,
                api_key=self._api_key,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user",   "content": user_msg},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Your previous output was not valid JSON. Output ONLY the JSON object."},
                ],
            )
            raw = retry.choices[0].message.content or ""
            tokens += retry.usage.total_tokens if hasattr(retry, "usage") and retry.usage else 0
            result = _parse_output(raw)
            if result is None:
                # Default to PASS if we can't parse — don't block on reviewer failure
                result = ReviewerOutput(verdict="PASS", issues=[])
        return result, tokens


_DEFAULT_REVIEWER_SYSTEM = """\
You are a critical reviewer of RFP draft answers. Your job is to catch:
- Claims not supported by the source material (hallucination)
- Factual inconsistencies between the answer and the context
- Overstated confidence given the source coverage
- Inappropriate marketing tone or hype words
- Missing critical caveats
- Invented specifics (customer names, version numbers, certification dates, regulatory clause numbers)

Be terse. No chatty preamble, no praise. Output strict JSON only.

If the draft is acceptable as-is, return verdict="PASS" and empty issues.
If there are problems, return verdict="FAIL" and list up to 3 SPECIFIC, ACTIONABLE issues. Each issue must be one sentence and must be objectively fixable.

OUTPUT (strict JSON, no markdown fences):
{
  "verdict": "PASS" | "FAIL",
  "issues": ["issue 1", "issue 2", "issue 3"]
}
"""
