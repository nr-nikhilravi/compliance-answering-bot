# RFP Auto-Responder

A **reusable, multi-agent RAG pipeline** that reads an RFP Excel file, retrieves relevant content from a knowledge base, drafts evidence-grounded answers using a maker-reviewer agent architecture, and produces a new answered Excel file.

Built for **BusinessNext** presales — specifically the Maybank RFP, but fully reusable across future RFPs by changing config, with no code changes required.

---

## How It Works

```
Source Docs (PDFs, DOCX, etc.)
    → Parser + Chunker
    → Gemini text-embedding-004 (cached)
    → Top-K retrieval per question
    → Maker Agent (Gemini 2.5 Pro) → draft answer
    → [Skip reviewer? see rules below]
    → Reviewer Agent (Gemini 2.5 Flash) → PASS / FAIL
    → [If FAIL: Maker revises ONCE, then accept]
    → Excel Writer (new file, Tracker formulas preserved)
```

### Skip-Review Rules

The reviewer is **skipped** in two cases (to control cost):

1. **High confidence, high retrieval score** (`top_score > 0.75` AND maker `confidence=high` AND `needs_review=false`) — source already validates grounding.
2. **Low confidence, low retrieval score** (`top_score < 0.30` AND `confidence=low`) — no source material to verify against anyway.

The reviewer is called when: confidence is `medium`, retrieval score is `0.30–0.75`, or maker flagged `needs_review=true` despite claiming high confidence.

**Hard revision cap**: If reviewer returns FAIL, maker revises **exactly once**. The revised answer is accepted as final. The reviewer is **never called again after a revision** — no infinite loops, no runaway token bills.

**Per-question token budget**: 6,000 tokens (soft circuit breaker). Exceeded questions are accepted as-is and flagged for review.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A [Gemini API key](https://aistudio.google.com/app/apikey)

### 2. Install Dependencies

```powershell
cd "D:\AI Knowledge\Complaince Answering Bot"
pip install -r requirements.txt
```

### 3. Set API Key

```powershell
$env:GEMINI_API_KEY = "your_key_here"
```

Or add it permanently to your Windows environment variables.

### 4. Configure (optional)

Copy `config.example.yaml` to `config.yaml` and adjust paths if needed. The defaults already point to the correct Windows folders.

### 5. Create test fixtures (first time only)

```powershell
python create_fixtures.py
```

---

## Quickstart

Drop your RFP Excel file into:
```
D:\AI Knowledge\Complaince Answering Bot\Input Excel\
```

Then run:

```powershell
# Auto-picks the most recently modified .xlsx
python -m rfp_responder

# Or specify the file explicitly
python -m rfp_responder --input "D:\AI Knowledge\Complaince Answering Bot\Input Excel\Technical__App_.xlsx"

# Dry run — only first 10 questions
python -m rfp_responder --limit 10

# Force re-embed the corpus (use after adding/changing source docs)
python -m rfp_responder --rebuild

# List available input files
python -m rfp_responder --list
```

The answered file is written to:
```
D:\AI Knowledge\Complaince Answering Bot\Output Excel\
Technical__App__answered_2026-05-09_1430.xlsx
```

---

## Input File Structure

The input `.xlsx` must have two sheets:

- **`Tracker`** — summary dashboard with COUNTA formulas (DO NOT touch — copied as-is)
- **`Technical (App)`** — questions starting at row 5, with:
  - Column B: Question number (`7.1`, `7.2`, …) — empty for section headers
  - Column C: Question text (label)
  - Column D: `Single Choice` or `Comment`
  - Column E: Response (Yes/No/Partial/N/A — written by this tool for Single Choice)
  - Column F: Comments (answer prose — written by this tool)

**Section headers** are detected dynamically: empty Column B + `Type=Comment` → skip (not answered).

---

## Output File

The output adds 6 audit columns after column F:

| Column | Header | Values |
|--------|--------|--------|
| G | Confidence | `high` / `medium` / `low` (color-coded) |
| H | Top Retrieval Score | `0.00–1.00` |
| I | Sources | Comma-separated doc names |
| J | Needs Review | `YES` / `no` |
| K | Review Notes | Reviewer concerns (if any) |
| L | Revision Count | `0` or `1` |

Color coding: 🟢 green = high, 🟡 yellow = medium, 🔴 red = low confidence. Red fill also on Comments when Needs Review = YES.

A `run_log.json` is also written to the output folder with per-question token usage, timing, retrieval scores, and decisions.

---

## Configuration Reference

See [`config.example.yaml`](config.example.yaml) for all fields with comments. Key settings:

```yaml
models:
  maker: "gemini-2.5-pro"      # change to test cheaper models
  reviewer: "gemini-2.5-flash"
  embeddings: "text-embedding-004"

retrieval:
  top_k: 10
  low_threshold: 0.30   # skip reviewer below this
  high_threshold: 0.75  # skip reviewer above this (+ high confidence)

orchestration:
  max_revisions: 1        # HARD CAP — do not increase
  per_question_token_budget: 6000
```

System prompts can be edited in `rfp_responder/prompts/` without touching any Python code.

---

## Cost & Runtime Estimates

For a typical RFP with 120–500 questions:

| Scenario | Time | Cost |
|----------|------|------|
| 120 questions, mostly high-confidence | ~20 min | ~$1–2 |
| 500 questions, many medium-confidence | ~60 min | ~$4–5 |

These are rough estimates based on Gemini 2.5 Pro pricing (~$14/1M tokens blended). Actual costs depend on question complexity and retrieval quality.

---

## Adapting to a New RFP / Vendor

1. Drop the new source docs into `Existing knowledge/Compliances/` (or change `paths.source_corpus` in config)
2. Drop the new RFP Excel into `Input Excel/`
3. Update `vendor` and `customer` fields in `config.yaml`
4. Run `python -m rfp_responder --rebuild` to re-embed the new corpus
5. That's it — no code changes required

---

## Running Tests

```powershell
# First, create the test fixtures
python create_fixtures.py

# Then run all tests
pytest tests/ -v
```

---

## Troubleshooting

### `ERROR: GEMINI_API_KEY environment variable is not set`
Set it: `$env:GEMINI_API_KEY = "your_key"`

### `No documents parsed from corpus`
Check that `paths.source_corpus` in your config points to a folder containing PDF/DOCX/XLSX/TXT files. Make sure the files aren't corrupted.

### `Sheet 'Technical (App)' not found`
Your input Excel has a different sheet name. Update `excel.question_sheet_name` in `config.yaml`.

### Tracker sheet formulas showing 0 / not updating
The formulas evaluate when you **open the file in Microsoft Excel**. This is normal behaviour — Excel recalculates on open. The tool intentionally preserves formulas as strings (not pre-calculated values) so they remain live.

### OCR / scanned PDFs not parsed
Image-based PDFs are out of scope for v1. Use a separate OCR tool (e.g., Adobe Acrobat) to convert them to text-based PDFs first.

---

## Project Structure

```
rfp_responder/
  config.py           # config loading, defaults
  parsers.py          # PDF, DOCX, XLSX, TXT parsers
  chunking.py         # text splitting with overlap
  embeddings.py       # Gemini embedding client + caching
  retrieval.py        # top-K cosine similarity retrieval
  agents/
    schemas.py        # Pydantic models for agent I/O
    maker.py          # Maker agent (Gemini 2.5 Pro)
    reviewer.py       # Reviewer agent (Gemini Flash)
    orchestrator.py   # skip-review logic, revision cap, token budget
  excel_io.py         # Excel read/write (Tracker sheet preservation)
  cli.py              # argparse entry point
  prompts/
    maker_system.md   # editable system prompt
    reviewer_system.md
  __main__.py         # python -m rfp_responder

tests/
  test_chunking.py
  test_parsers.py
  test_excel_io.py
  test_orchestrator.py
  fixtures/

config.example.yaml
requirements.txt
create_fixtures.py    # generate test fixtures
```

---

## Anti-Hallucination Guarantees

1. Maker may only state facts present in retrieved context
2. Gaps acknowledged explicitly — never invented
3. No invented customer names, version numbers, certification dates, or regulatory clause numbers
4. `sources_used` cross-validated against actually-retrieved docs — invented source names are replaced automatically
5. Standards (BNM RMiT, ISO 27001, SOC 2, PCI-DSS) referenced only when source material supports them
6. Reviewer checks for all of the above independently
