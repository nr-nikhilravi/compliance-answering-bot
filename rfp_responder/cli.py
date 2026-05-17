from __future__ import annotations

"""CLI entry point: wires everything together."""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from .config import AppConfig, load_config
from .parsers import walk_corpus
from .chunking import chunk_corpus
from .embeddings import EmbeddingClient, EmbeddingCache
from .retrieval import retrieve
from .excel_io import read_rfp_sheet, write_output_excel, ROW_TYPE_QUESTION
from .agents.maker import MakerAgent
from .agents.reviewer import ReviewerAgent
from .agents.orchestrator import Orchestrator

import openpyxl

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_path), encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Input file resolution
# ---------------------------------------------------------------------------

def _resolve_input_file(input_arg: Optional[str], cfg: AppConfig) -> Path:
    if input_arg:
        p = Path(input_arg)
        if not p.exists():
            sys.exit(f"ERROR: Input file not found: {p}")
        return p

    folder = cfg.paths.input_excel_folder
    if not folder.exists():
        sys.exit(f"ERROR: Input folder not found: {folder}")

    xlsx_files = sorted(folder.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not xlsx_files:
        sys.exit(f"ERROR: No .xlsx files found in {folder}")

    chosen = xlsx_files[0]
    print(f"Selected input file: {chosen.name} (most recently modified)")
    return chosen


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace, cfg: AppConfig) -> None:
    start_time = time.time()

    _setup_logging(cfg.paths.output_excel_folder)
    logger.info("=== RFP Auto-Responder starting ===")

    # --- API key ---
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        sys.exit("ERROR: GEMINI_API_KEY environment variable is not set.")

    # --- Input file ---
    input_path = _resolve_input_file(getattr(args, "input", None), cfg)

    # --- Output path ---
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_name = f"{input_path.stem}_answered_{ts}.xlsx"
    output_path = cfg.paths.output_excel_folder / out_name

    # --- Read RFP questions ---
    wb = openpyxl.load_workbook(str(input_path))
    rows = read_rfp_sheet(wb, cfg)
    wb.close()

    question_rows = [r for r in rows if r.row_type == ROW_TYPE_QUESTION]
    limit = getattr(args, "limit", None)
    if limit:
        question_rows = question_rows[:limit]

    logger.info("Processing %d questions from %s", len(question_rows), input_path.name)

    # --- Build/load corpus embeddings ---
    parsed = walk_corpus(cfg.paths.source_corpus)
    if not parsed:
        logger.warning("No documents parsed from corpus — answers will have low confidence")

    chunks = chunk_corpus(parsed)

    embed_client = EmbeddingClient(api_key=api_key, model=cfg.models.embeddings)
    cache = EmbeddingCache(cfg.paths.cache_folder, embed_client)

    if getattr(args, "rebuild", False):
        cache.invalidate()

    chunks, chunk_embeddings = cache.get_or_build(
        chunks,
        cfg.paths.source_corpus,
        chunk_size=3200,
        overlap=800,
    )

    all_source_names = {c.source for c in chunks}

    # --- Agents ---
    maker    = MakerAgent(api_key, cfg.models.maker,    cfg.prompts_dir, cfg.vendor, cfg.customer)
    reviewer = ReviewerAgent(api_key, cfg.models.reviewer, cfg.prompts_dir)
    orch     = Orchestrator(maker, reviewer, cfg)
    from .agents.comparer import ComparerAgent
    comparer = ComparerAgent(api_key, cfg.models.reviewer)

    # --- Per-question loop ---
    results: dict[int, dict] = {}
    run_log: list[dict] = []

    counters = {"high": 0, "medium": 0, "low": 0, "revised": 0, "flagged": 0, "total_tokens": 0}

    for rfp_row in tqdm(question_rows, desc="Answering questions"):
        q_start = time.time()
        logger.info("Q%s: %s", rfp_row.question_number, rfp_row.question_text[:80])

        try:
            # Embed question (uses retrieval_query task type for better retrieval quality)
            q_embedding = embed_client.embed_query(rfp_row.question_text)

            # Retrieve
            retrieved = retrieve(
                q_embedding,
                chunk_embeddings,
                chunks,
                top_k=cfg.retrieval.top_k,
            )

            retrieved_source_names = {rc.chunk.source for rc in retrieved}

            # Orchestrate
            orch_result = orch.process_question(
                question=rfp_row.question_text,
                question_type=rfp_row.question_type,
                chunks=retrieved,
                retrieved_source_names=retrieved_source_names,
            )

            out = orch_result.final_output
            top_score = retrieved[0].score if retrieved else 0.0

            # Comparison
            comparison_result, comp_tokens = comparer.compare(
                rfp_row.existing_comment, 
                out.answer_text
            )
            orch_result.tokens_used += comp_tokens

            result = {
                "single_choice_value": out.single_choice_value,
                "answer_text":         out.answer_text,
                "confidence":          out.confidence,
                "top_retrieval_score": top_score,
                "sources":             out.sources_used[:3],
                "needs_review":        out.needs_review,
                "review_notes":        orch_result.review_notes,
                "revision_count":      orch_result.revision_count,
                "comparison":          comparison_result,
            }

            counters[out.confidence] = counters.get(out.confidence, 0) + 1
            if orch_result.revision_count > 0:
                counters["revised"] += 1
            if out.needs_review:
                counters["flagged"] += 1
            counters["total_tokens"] += orch_result.tokens_used

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error processing Q%s: %s", rfp_row.question_number, exc)
            result = {
                "single_choice_value": "",
                "answer_text": "",
                "confidence": "low",
                "top_retrieval_score": 0.0,
                "sources": [],
                "needs_review": True,
                "review_notes": f"Processing error: {exc}",
                "revision_count": 0,
                "comparison": "",
            }
            counters["low"] += 1
            counters["flagged"] += 1

        results[rfp_row.row_number] = result

        run_log.append({
            "row":          rfp_row.row_number,
            "q_number":     rfp_row.question_number,
            "question":     rfp_row.question_text[:100],
            "confidence":   result["confidence"],
            "top_score":    result["top_retrieval_score"],
            "tokens":       orch_result.tokens_used if "orch_result" in dir() else 0,
            "reviewer_called": orch_result.reviewer_called if "orch_result" in dir() else False,
            "revision_count":  orch_result.revision_count if "orch_result" in dir() else 0,
            "skip_reason":     orch_result.skip_reason if "orch_result" in dir() else "",
            "elapsed_s":    round(time.time() - q_start, 2),
        })

    # --- Write output Excel ---
    write_output_excel(input_path, output_path, results, cfg)

    # --- Write run log ---
    log_path = cfg.paths.output_excel_folder / "run_log.json"
    with log_path.open("w", encoding="utf-8") as fh:
        json.dump(run_log, fh, indent=2, ensure_ascii=False)
    logger.info("Run log written to %s", log_path)

    # --- Final summary ---
    elapsed = time.time() - start_time
    total_tokens = counters["total_tokens"]
    # Rough cost estimate: Gemini 2.5 Pro ~$7/1M input + $21/1M output → ~$14/1M blended
    est_cost = (total_tokens / 1_000_000) * 14.0

    print("\n" + "=" * 60)
    print(f"Input file          : {input_path.name}")
    print(f"Output file         : {output_path}")
    print(f"Total questions     : {len(question_rows)}")
    print(f"  high confidence   : {counters.get('high', 0)}")
    print(f"  medium confidence : {counters.get('medium', 0)}")
    print(f"  low confidence    : {counters.get('low', 0)}")
    print(f"  needed revision   : {counters.get('revised', 0)}")
    print(f"  flagged for review: {counters.get('flagged', 0)}")
    print(f"Total tokens used   : {total_tokens:,}")
    print(f"Estimated cost      : ${est_cost:.2f}")
    print(f"Total runtime       : {elapsed/60:.1f} min")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rfp_responder",
        description="RFP Auto-Responder — Multi-Agent RAG pipeline",
    )
    p.add_argument("--input",   help="Path to input .xlsx file (defaults to latest in configured folder)")
    p.add_argument("--config",  help="Path to YAML config file")
    p.add_argument("--limit",   type=int, help="Process only first N questions (dry-run)")
    p.add_argument("--rebuild", action="store_true", help="Force re-embed the corpus")
    p.add_argument("--list",    action="store_true", help="List available input files and exit")
    return p


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    cfg_path = Path(args.config) if args.config else None
    cfg = load_config(cfg_path)

    if args.list:
        folder = cfg.paths.input_excel_folder
        if not folder.exists():
            print(f"Input folder not found: {folder}")
            return
        files = sorted(folder.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            print(f"No .xlsx files found in {folder}")
        else:
            print(f"Files in {folder}:")
            for f in files:
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                print(f"  {f.name}  [{mtime}]")
        return

    run(args, cfg)
