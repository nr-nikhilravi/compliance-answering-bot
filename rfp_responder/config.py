from __future__ import annotations

"""Configuration loading with YAML override support."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Default paths (configurable via YAML or CLI)
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).parent.parent
_DEFAULT_SOURCE = os.environ.get("SOURCE_CORPUS", str(_BASE_DIR / "Existing knowledge" / "Compliances"))
_DEFAULT_INPUT  = os.environ.get("INPUT_EXCEL_FOLDER", str(_BASE_DIR / "Input Excel"))
_DEFAULT_OUTPUT = os.environ.get("OUTPUT_EXCEL_FOLDER", str(_BASE_DIR / "Output Excel"))
_DEFAULT_CACHE  = os.environ.get("CACHE_FOLDER", str(_BASE_DIR / ".rag_cache"))


@dataclass
class PathsConfig:
    source_corpus: Path = field(default_factory=lambda: Path(_DEFAULT_SOURCE))
    input_excel_folder: Path = field(default_factory=lambda: Path(_DEFAULT_INPUT))
    output_excel_folder: Path = field(default_factory=lambda: Path(_DEFAULT_OUTPUT))
    cache_folder: Path = field(default_factory=lambda: Path(_DEFAULT_CACHE))


@dataclass
class ExcelConfig:
    question_sheet_name: str = "Technical (App)"
    preserve_sheets: list[str] = field(default_factory=lambda: ["Tracker"])
    header_row: int = 2
    data_start_row: int = 5
    no_column: str = "A"
    question_column: str = "B"
    type_column: str = "C"
    response_column: str = "D"
    existing_comments_column: str = "E"
    generated_comments_column: str = "F"
    comparison_column: str = "G"


@dataclass
class VendorConfig:
    name: str = "BusinessNext"
    description: str = "banking-focused CRM SaaS vendor headquartered in India"
    region_focus: str = "BFSI across India, Middle East, and Asia-Pacific"


@dataclass
class CustomerConfig:
    name: str = "Maybank"
    context: str = "tier-1 Malaysian bank regulated by Bank Negara Malaysia under RMiT 2020"


@dataclass
class ModelsConfig:
    maker: str = "gemini-2.5-pro"
    reviewer: str = "gemini-2.5-flash"
    embeddings: str = "text-embedding-004"


@dataclass
class RetrievalConfig:
    top_k: int = 10
    low_threshold: float = 0.30
    high_threshold: float = 0.75


@dataclass
class OrchestrationConfig:
    max_revisions: int = 1
    per_question_token_budget: int = 20000
    skip_reviewer_on_high_confidence: bool = True


@dataclass
class AppConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    excel: ExcelConfig = field(default_factory=ExcelConfig)
    vendor: VendorConfig = field(default_factory=VendorConfig)
    customer: CustomerConfig = field(default_factory=CustomerConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)
    prompts_dir: Optional[Path] = None  # resolved to rfp_responder/prompts by default


def load_config(yaml_path: Optional[Path] = None) -> AppConfig:
    """Load config from optional YAML file, merging with defaults."""
    cfg = AppConfig()

    # Resolve prompts dir relative to this file's package
    cfg.prompts_dir = Path(__file__).parent / "prompts"

    if yaml_path is None or not yaml_path.exists():
        return cfg

    with yaml_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    # --- paths ---
    if paths := raw.get("paths"):
        if v := paths.get("source_corpus"):
            cfg.paths.source_corpus = Path(v)
        if v := paths.get("input_excel_folder"):
            cfg.paths.input_excel_folder = Path(v)
        if v := paths.get("output_excel_folder"):
            cfg.paths.output_excel_folder = Path(v)
        if v := paths.get("cache_folder"):
            cfg.paths.cache_folder = Path(v)

    # --- excel ---
    if excel := raw.get("excel"):
        for attr in ("question_sheet_name", "header_row", "data_start_row",
                     "no_column", "question_column", "type_column",
                     "response_column", "existing_comments_column", 
                     "generated_comments_column", "comparison_column"):
            if v := excel.get(attr):
                setattr(cfg.excel, attr, v)
        if v := excel.get("preserve_sheets"):
            cfg.excel.preserve_sheets = list(v)

    # --- vendor ---
    if vendor := raw.get("vendor"):
        for attr in ("name", "description", "region_focus"):
            if v := vendor.get(attr):
                setattr(cfg.vendor, attr, v)

    # --- customer ---
    if customer := raw.get("customer"):
        for attr in ("name", "context"):
            if v := customer.get(attr):
                setattr(cfg.customer, attr, v)

    # --- models ---
    if models := raw.get("models"):
        for attr in ("maker", "reviewer", "embeddings"):
            if v := models.get(attr):
                setattr(cfg.models, attr, v)

    # --- retrieval ---
    if retrieval := raw.get("retrieval"):
        if v := retrieval.get("top_k"):
            cfg.retrieval.top_k = int(v)
        if v := retrieval.get("low_threshold"):
            cfg.retrieval.low_threshold = float(v)
        if v := retrieval.get("high_threshold"):
            cfg.retrieval.high_threshold = float(v)

    # --- orchestration ---
    if orch := raw.get("orchestration"):
        if v := orch.get("max_revisions"):
            cfg.orchestration.max_revisions = int(v)
        if v := orch.get("per_question_token_budget"):
            cfg.orchestration.per_question_token_budget = int(v)
        if "skip_reviewer_on_high_confidence" in orch:
            cfg.orchestration.skip_reviewer_on_high_confidence = bool(
                orch["skip_reviewer_on_high_confidence"]
            )

    return cfg
