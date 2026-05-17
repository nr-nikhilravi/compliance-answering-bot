from __future__ import annotations

"""Maker agent — drafts RFP answers using Gemini 2.5 Pro."""

import json
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI

from ..retrieval import RetrievedChunk
from .schemas import MakerOutput

logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _build_system_prompt(prompts_dir: Path, vendor: object, customer: object) -> str:
    tpl_path = prompts_dir / "maker_system.md"
    if tpl_path.exists():
        tpl = tpl_path.read_text(encoding="utf-8")
    else:
        tpl = _DEFAULT_MAKER_SYSTEM
    return (
        tpl
        .replace("{vendor.name}", getattr(vendor, "name", "BusinessNext"))
        .replace("{vendor.description}", getattr(vendor, "description", ""))
        .replace("{vendor.region_focus}", getattr(vendor, "region_focus", ""))
        .replace("{customer.name}", getattr(customer, "name", "Maybank"))
        .replace("{customer.context}", getattr(customer, "context", ""))
    )


def _format_context(chunks: list[RetrievedChunk]) -> str:
    parts: list[str] = []
    for i, rc in enumerate(chunks, start=1):
        meta = f"[Source: {rc.chunk.source}"
        if rc.chunk.page:
            meta += f", Page {rc.chunk.page}"
        if rc.chunk.section:
            meta += f", Section: {rc.chunk.section}"
        meta += f", Score: {rc.score:.3f}]"
        parts.append(f"--- Chunk {i} {meta} ---\n{rc.chunk.text}")
    return "\n\n".join(parts)


def _parse_output(raw: str) -> Optional[MakerOutput]:
    """Parse and validate JSON output from maker. Returns None on failure."""
    raw = raw.strip()
    # Strip markdown fences if model added them
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        data = json.loads(raw)
        return MakerOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MakerOutput parse failed: %s", exc)
        return None


import litellm
from .utils import call_llm_with_retries

class MakerAgent:
    def __init__(
        self,
        api_key: str,
        model: str,
        prompts_dir: Path,
        vendor: object,
        customer: object,
        provider: str = "gemini",
    ):
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

        self._system_prompt = _build_system_prompt(prompts_dir, vendor, customer)

    def draft(
        self,
        question: str,
        question_type: str,
        chunks: list[RetrievedChunk],
    ) -> tuple[MakerOutput, int]:
        """Return (MakerOutput, tokens_used)."""
        context = _format_context(chunks)
        user_msg = (
            f"QUESTION TYPE: {question_type}\n\n"
            f"RFP QUESTION:\n{question}\n\n"
            f"RETRIEVED CONTEXT:\n{context}"
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
            # Retry once with a corrective message
            logger.warning("Maker output invalid — retrying once")
            retry_response = call_llm_with_retries(
                model=self._model,
                api_key=self._api_key,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user",   "content": user_msg},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "Your previous output was not valid JSON matching the required schema. Please output ONLY the JSON object, no markdown fences, no extra text."},
                ],
            )
            raw = retry_response.choices[0].message.content or ""
            tokens += retry_response.usage.total_tokens if hasattr(retry_response, "usage") and retry_response.usage else 0
            result = _parse_output(raw)
            if result is None:
                # Accept best effort with review flag
                result = MakerOutput(
                    single_choice_value="",
                    answer_text=raw[:500],
                    confidence="low",
                    sources_used=[],
                    needs_review=True,
                    review_reason="Failed to parse maker output as valid JSON",
                )

        return result, tokens

    def revise(
        self,
        question: str,
        question_type: str,
        chunks: list[RetrievedChunk],
        previous_draft: MakerOutput,
        issues: list[str],
    ) -> tuple[MakerOutput, int]:
        """Revise a draft given reviewer issues (called at most ONCE)."""
        context = _format_context(chunks)
        issues_bulleted = "\n".join(f"- {iss}" for iss in issues)
        revision_suffix = (
            f"\n\nYour previous draft was reviewed and the reviewer flagged these issues:\n\n"
            f"{issues_bulleted}\n\n"
            f"Revise your answer to address each issue. Stay grounded in the provided context. "
            f"If you cannot address an issue without inventing facts, instead set needs_review=true "
            f"and explain what the SME should clarify.\n\nOutput the same JSON format as before."
        )
        user_msg = (
            f"QUESTION TYPE: {question_type}\n\n"
            f"RFP QUESTION:\n{question}\n\n"
            f"RETRIEVED CONTEXT:\n{context}"
        )
        response = litellm.completion(
            model=self._model,
            api_key=self._api_key,
            messages=[
                {"role": "system",    "content": self._system_prompt},
                {"role": "user",      "content": user_msg},
                {"role": "assistant", "content": previous_draft.model_dump_json()},
                {"role": "user",      "content": revision_suffix},
            ],
        )
        raw = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if hasattr(response, "usage") and response.usage else 0
        result = _parse_output(raw)
        if result is None:
            result = MakerOutput(
                single_choice_value=previous_draft.single_choice_value,
                answer_text=previous_draft.answer_text,
                confidence="low",
                sources_used=previous_draft.sources_used,
                needs_review=True,
                review_reason="Revision parse failed; kept original draft",
            )
        return result, tokens


# ---------------------------------------------------------------------------
# Default system prompt (used if file not found)
# ---------------------------------------------------------------------------
_DEFAULT_MAKER_SYSTEM = """\
You are a senior presales consultant at {vendor.name}, {vendor.description}, with a strong track record in {vendor.region_focus}.

You are drafting responses to a Request for Proposal (RFP) from {customer.name}, {customer.context}. The customer is sophisticated and values evidence over marketing claims.

You will receive an RFP question and excerpts from {vendor.name}'s product documentation, security whitepapers, architecture docs, and prior RFP responses. Draft an evidence-grounded answer suitable for direct submission.

CRITICAL RULES:
1. Base your answer ONLY on the provided context. Never invent capabilities or claim functionality not supported by source material. Hallucination in an RFP is far worse than admitting a gap.
2. If the context does not adequately answer the question, set needs_review=true and explain what the SME should clarify. Do NOT fabricate an answer.
3. Tone: professional, confident, factual. No hype words ("revolutionary", "best-in-class", "world-leading") unless directly quoted from source.
4. For "Single Choice" questions, set single_choice_value to "Yes" / "No" / "Partial" / "N/A", then put the explanation in answer_text.
5. For "Comment" questions, set single_choice_value="" and write the full answer in answer_text.
6. Reference standards (BNM RMiT, ISO 27001, SOC 2, PCI-DSS) only when the source explicitly supports it. Do not invent clause numbers.
7. Confidence: "high" only when source directly answers; "medium" if partial coverage; "low" if stretching.
8. Length: 2-5 sentences typical. Up to a paragraph for technical-architecture questions. Prose only — no bullets, no markdown formatting in answer_text. Ensure that the answer_text is NOT more than 2000 characters.
9. Product Naming: Do not use the words 'AINEXT' or 'LOYALTYNEXT' as they are not available. The platform is 'BUSINESSNEXT' and products include 'CRMNEXT', 'LENDINGNEXT', 'DATANEXT' and 'MARKETINGNEXT'.

OUTPUT (strict JSON, no markdown fences):
{
  "single_choice_value": "Yes" | "No" | "Partial" | "N/A" | "",
  "answer_text": "...",
  "confidence": "high" | "medium" | "low",
  "sources_used": ["doc1", "doc2"],
  "needs_review": true | false,
  "review_reason": "..."
}
"""
