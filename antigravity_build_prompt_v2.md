

# Build Spec: RFP Auto-Responder (Multi-Agent RAG)

## 1\. Project Context

I am a presales consultant at **BusinessNext** (formerly CRMNEXT), a banking-focused CRM SaaS vendor headquartered in India with strong customer references in Indian and ASEAN BFSI. We respond to RFPs from large banks, including a current high-stakes RFP from **Maybank** (a tier-1 Malaysian bank regulated by Bank Negara Malaysia under the RMiT 2020 policy).

A typical RFP has **120–500 compliance and technical questions** that we must answer in Excel format. Each answer must be:

* Grounded in our internal source documentation (product specs, security whitepapers, architecture documents, prior RFP responses)
* Professional, factual, and free of marketing hype
* Honest about gaps — hallucinated capabilities in an RFP get caught in technical evaluation and damage the bid

## 2\. File Locations (Windows)

These are concrete paths on the user's Windows machine:

* **Source knowledge base** (read-only, \~11 MB of PDFs, DOCX, etc.):  
`D:\\AI Knowledge\\Complaince Answering Bot\\Existing knowledge\\Compliances`
* **Input RFP Excel folder** (read-only — the application picks up the active RFP file from here):  
`D:\\AI Knowledge\\Complaince Answering Bot\\Input Excel`
* **Output folder** (the app creates a new answered file here — do NOT overwrite the input):  
`D:\\AI Knowledge\\Complaince Answering Bot\\Output Excel`  
(Create this folder if it doesn't exist on first run.)
* **Cache folder** for embeddings (created automatically):  
`D:\\AI Knowledge\\Complaince Answering Bot\\.rag\_cache`

**Note for code**: Use Python's `pathlib.Path` and raw strings (`r"D:\\..."`) or forward slashes to avoid backslash escape issues on Windows. Make all paths configurable via CLI flags or YAML config — these defaults are sensible but should be overridable.

## 3\. Goal

Build a **reusable Python application** that reads an RFP Excel file, retrieves relevant content from the knowledge base, drafts answers using a maker-reviewer agent architecture, and produces a NEW answered Excel file in the output folder. The same application must be reusable across future RFPs by changing inputs — no code changes required.

## 4\. Input Excel Structure (CRITICAL — must be handled exactly as specified)

The input file is an `.xlsx` workbook with **two sheets** that must both be preserved in the output:

### Sheet 1: `Tracker` (DO NOT modify cell contents)

A summary dashboard with formulas like `=COUNTA('Technical (App)'!F5:F7)` that auto-count filled answers per section. This sheet must be **copied through to the output unchanged**. The COUNTA formulas will recalculate automatically based on what we write to the `Technical (App)` sheet.

### Sheet 2: `Technical (App)` (this is where answers go)

**Header row: row 2.** Columns are:

|Column|Letter|Header|Purpose|
|-|-|-|-|
|B|B|`No`|Question number (e.g., `7.1`, `7.2`). Empty for section headers.|
|C|C|`Label`|The question text. **THIS IS WHERE WE READ FROM, starting at row 5.**|
|D|D|`Type`|`Single Choice` or `Comment`.|
|E|E|`Response`|Yes / No / Partial / N/A — only for Single Choice questions.|
|F|F|`Comments`|**THIS IS WHERE WE WRITE THE ANSWER PROSE.** Tracker COUNTA depends on this column.|

### Row Classification Rules (READ CAREFULLY)

Walk row 5 onward. For each row:

1. **Section header row** — SKIP (do not answer):

   * Column B (`No`) is empty AND Column D (`Type`) equals `Comment`.
   * Column C contains a section name like `Business Architecture`, `Data Architecture`, `Cloud Architecture`, etc.
   * These rows must not be touched in the output.
2. **Real question row** — ANSWER:

   * Column B has a value (like `7.1`, `7.2`, etc.).
   * Column C has the question text.
   * Column D is either `Single Choice` or `Comment`.
   * Write the prose answer into Column F.
   * For `Single Choice` rows, ALSO write `Yes` / `No` / `Partial` / `N/A` into Column E.
3. **Empty row** — SKIP. Some sheets have blank separator rows.
4. **Stop when**: Column C and Column B are both empty for 3 consecutive rows (end of data), OR `ws.max\_row` is reached.

### Sections Present in This Specific File

For reference, the input file `Technical\_\_App\_.xlsx` has at least these sections (more may exist beyond row 124):
Business Architecture, Data Architecture, Artificial Intelligence Architecture, Application Architecture, Technology Architecture, Cloud Architecture, Security Architecture, Identity \& Access, Compliance \& Privacy, Data Model \& Customization, Integration \& APIs, Data Migration, Automation \& Workflow.

The application must NOT hardcode these. It should walk the sheet and detect sections dynamically using the row classification rules above.

## 5\. Output File

**Create a new Excel file** — do not overwrite the input. Naming convention:

```
{InputFileName}\_answered\_{YYYY-MM-DD\_HHMM}.xlsx
```

Example: `Technical\_\_App\_\_answered\_2026-05-09\_1430.xlsx`

Save it to: `D:\\AI Knowledge\\Complaince Answering Bot\\Output Excel\\`

The output file must:

* Preserve the `Tracker` sheet exactly as in the input (formulas will auto-recalculate to reflect the count of filled F cells in `Technical (App)`).
* Have the `Technical (App)` sheet with answers populated as described, plus appended audit columns to the right of column F.

### Audit Columns (appended to `Technical (App)` sheet)

After column F (Comments), add these new columns:

|Column|Header|Content|
|-|-|-|
|G|`Confidence`|`high` / `medium` / `low`|
|H|`Top Retrieval Score`|Numeric, e.g. `0.78`|
|I|`Sources`|Comma-separated source document names|
|J|`Needs Review`|`YES` / `no`|
|K|`Review Notes`|Reviewer feedback if any; empty otherwise|
|L|`Revision Count`|`0` or `1`|

Color coding on the `Confidence` cell: green (`D9F2D9`) for high, yellow (`FFF2CC`) for medium, red (`FFCCCC`) for low. Also apply red fill to the `Comments` cell when `Needs Review = YES`.

Wrap text on `Comments` and `Review Notes`. Set their column widths to 60 and 50 respectively.

### Run Log

Also write a `run\_log.json` to the output folder capturing per-question token usage, timing, retrieval scores, and decisions. Useful for audit and cost tracking.

## 6\. Architecture Overview

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Source Docs    │────▶│  Parser \&    │────▶│  Embedded Chunks │
│  (D:\\...\\       │     │  Chunker     │     │  (cached)        │
│   Compliances)  │     └──────────────┘     └──────────────────┘
└─────────────────┘                                    │
                                                       ▼
┌─────────────────┐                          ┌──────────────────┐
│ RFP Excel       │─────────question─────────▶  Retriever       │
│ (Input Excel\\)  │                          │  (top-K chunks)  │
└─────────────────┘                          └──────────────────┘
                                                       │
                                                       ▼
                                             ┌──────────────────┐
                                             │  Maker Agent     │
                                             │  (Gemini 2.5 Pro)│
                                             └──────────────────┘
                                                       │
                                          ┌────────────┴───────────┐
                                          ▼                        ▼
                              \[Skip review path]         \[Review path]
                              high confidence +           medium confidence
                              high retrieval score        OR mixed signals
                                          │                        │
                                          │                        ▼
                                          │            ┌──────────────────┐
                                          │            │  Reviewer Agent  │
                                          │            │  (Gemini Flash)  │
                                          │            └──────────────────┘
                                          │                        │
                                          │                ┌───────┴────────┐
                                          │                ▼                ▼
                                          │             \[PASS]         \[FAIL]
                                          │                │                │
                                          │                │                ▼
                                          │                │       ┌─────────────────┐
                                          │                │       │ Maker Revises   │
                                          │                │       │ (ONCE only)     │
                                          │                │       └─────────────────┘
                                          ▼                ▼                ▼
                                          └────────────────┴────────────────┘
                                                          │
                                                          ▼
                                          ┌──────────────────────────────┐
                                          │  Excel Writer                │
                                          │  (NEW file in Output Excel\\) │
                                          └──────────────────────────────┘
```

## 7\. Maker-Reviewer Pattern — CRITICAL DESIGN

This is the most important part of the spec. Get this right.

### 7.1 The Maker Agent

* **Model**: Gemini 2.5 Pro (high quality, strong instruction following)
* **Input**: The RFP question + top-K retrieved chunks with source metadata
* **Output**: Strict JSON with: `single\_choice\_value`, `answer\_text`, `confidence` (`high`/`medium`/`low`), `sources\_used`, `needs\_review`, `review\_reason`
* **Behavior**: Drafts answer from context only. If context is insufficient, sets `confidence="low"` and `needs\_review=true` with a specific reason.

### 7.2 The Reviewer Agent

* **Model**: Gemini 2.5 Flash (fast, cheap — keeps token budget down)
* **Input**: The question + the SAME retrieved chunks + maker's draft answer
* **Output**: Strict JSON with `verdict` (`PASS` or `FAIL`) and a list of up to 3 specific, actionable issues. Reviewer is instructed to be terse — no chatty preamble.
* **Job**: Check that the answer is (a) grounded in the provided context, (b) factually consistent, (c) appropriately hedged, (d) not making claims beyond the source. Also check tone is professional / no hype.

### 7.3 Anti-Loop Logic — MANDATORY

To prevent runaway token consumption:

**Skip the reviewer entirely when:**

* Top retrieval score > 0.75 (strong context match) AND
* Maker self-reported `confidence == "high"` AND
* Maker said `needs\_review == false`
* → Accept the maker's answer directly. The retrieval signal already validates grounding.

**Also skip the reviewer when:**

* Top retrieval score < 0.30 (no good context to verify against) AND
* Maker said `confidence == "low"` AND `needs\_review == true`
* → Trust the maker's self-flag. Reviewer can't add value where there's no source material.

**Run the reviewer when:**

* Maker confidence is `medium`, OR
* Retrieval score is in the `0.30–0.75` range, OR
* Maker said `needs\_review=true` despite high confidence (suspicious mismatch worth checking)

**Revision rule (HARD CAP):**

* If reviewer returns `FAIL`: maker is given the reviewer's specific issues and re-drafts **ONCE**.
* The revised answer is **accepted as final** regardless of whether the reviewer would still object — no second review pass, no third revision, no debate loop.
* If revised: `Revision Count = 1`, `Review Notes` field captures the reviewer's original concerns. The flag `needs\_review=true` is set so SME knows to double-check.
* Do NOT call the reviewer again after the maker revises. This is non-negotiable to control cost and prevent infinite loops.

### 7.4 Token Budget Per Question

Set a soft per-question budget of **6,000 total tokens** (across maker + reviewer + optional revision). If a question would exceed this, log a warning, accept whatever was last produced, and flag `needs\_review=true`. This is a circuit breaker, not a normal path — most questions should consume 2K–4K tokens.

## 8\. Anti-Hallucination Rules

These must be encoded in the maker's system prompt and enforced through output validation:

1. **Source-only grounding**: Maker may only state facts present in or directly inferable from the retrieved context.
2. **Explicit gap acknowledgement**: If the context doesn't answer the question, the answer must say so plainly (e.g., *"The provided source material does not specifically address \[topic]; recommend SME confirmation."*) and set `needs\_review=true`.
3. **No invented specifics**: Never invent customer names, certification dates, version numbers, contract values, regulatory clause numbers, feature names, or BNM RMiT subsection references.
4. **No hype words**: Forbidden in answer text unless directly quoted from source: "revolutionary", "best-in-class", "world-leading", "industry-first", "cutting-edge", "state-of-the-art".
5. **Source attribution validation**: `sources\_used` must contain only document names that appeared in the retrieved context for this specific question. Cross-validate after model output: if `sources\_used` contains a doc name not in the retrieved set, overwrite it with the actual top-3 retrieved doc names and log a warning.
6. **Standards mentions**: References to BNM RMiT, ISO 27001, SOC 2, PCI-DSS are allowed only when the source material supports them.

## 9\. Functional Requirements

### Document Parsing (source knowledge base)

* PDF: use `pypdf`, page-level extraction, capture page numbers as metadata
* DOCX: use `python-docx`, extract paragraphs with heading hierarchy, also extract tables (RFP source docs frequently have RACI matrices and feature tables that matter)
* XLSX (in source folder): read all sheets, flatten rows into "col1 | col2 | col3" text
* TXT, MD: read as-is
* Skip files that fail to parse with a warning, don't crash the run
* Recursively walk the source folder (subdirectories supported)

### Chunking

* Approximately 800-token chunks (use \~3200 chars as proxy)
* 200-token (\~800 char) overlap between adjacent chunks
* Prefer to break on paragraph boundaries (`\\n\\n`), then sentence boundaries (`. `)
* Skip chunks under 100 chars
* Preserve metadata: source document name, page number (PDFs), section heading (DOCX where available)

### Embeddings

* **Model**: Gemini `text-embedding-004` (use Google's OpenAI-compatible endpoint at `https://generativelanguage.googleapis.com/v1beta/openai/`)
* Batch in groups of 100
* Cache to disk in the cache folder, keyed by file hash + chunking config so re-runs don't re-embed
* L2-normalize for cosine similarity via dot product

### Retrieval

* Cosine similarity, top-K = 10 default (configurable)
* For each question, embed the question text, dot-product with all chunk embeddings, take top K
* Pass retrieval scores through to the agents (they need this for skip-review logic)

### Excel I/O — IMPORTANT IMPLEMENTATION NOTES

* Use `openpyxl` (preserves formatting, formulas, and the Tracker sheet)
* Open the input file with `load\_workbook(path)` (NOT `data\_only=True` — we need to preserve the Tracker sheet's formulas)
* After populating the `Technical (App)` sheet, save to a new path in the output folder. The Tracker formulas will recalculate automatically when the file is opened in Excel.
* Detect headers in row 2 of `Technical (App)`. Use case-insensitive header matching with the expected names (`No`, `Label`, `Type`, `Response`, `Comments`) but tolerate slight variations.
* Iterate from row 5 to `ws.max\_row`. Apply the row classification rules from Section 4.
* Append the 6 audit columns AFTER column F (so they become G, H, I, J, K, L).
* Apply the color fills described in Section 5.

## 10\. Technical Stack

```
Python 3.10+

Required:
  google-generativeai      # for Gemini API (alternative: use openai SDK against Gemini's compat endpoint)
  openai                   # used for the OpenAI-compatible Gemini endpoint
  pypdf                    # PDF parsing
  python-docx              # DOCX parsing
  openpyxl                 # Excel I/O (preserves Tracker formulas)
  numpy                    # vector ops
  tqdm                     # progress bars
  pydantic                 # output schema validation
  pyyaml                   # config files
```

Use Pydantic models to validate all LLM JSON output. If validation fails, retry once with a "your previous output was invalid JSON" addendum, then accept whatever comes back and flag `needs\_review=true`.

## 11\. Project Structure

```
rfp\_responder/
  \_\_init\_\_.py
  config.py             # config loading, defaults, dataclasses
  parsers.py            # PDF, DOCX, XLSX, TXT parsers
  chunking.py           # text splitting with overlap
  embeddings.py         # Gemini embedding client + caching
  retrieval.py          # vector retrieval
  agents/
    \_\_init\_\_.py
    schemas.py          # Pydantic models for agent I/O
    maker.py            # maker agent
    reviewer.py         # reviewer agent
    orchestrator.py     # skip-review logic, revision cap, token budget
  excel\_io.py           # read/write Excel with Tracker sheet preservation
  cli.py                # entry point with argparse
  prompts/
    maker\_system.md     # editable system prompt
    reviewer\_system.md  # editable system prompt
  \_\_main\_\_.py           # python -m rfp\_responder

tests/
  test\_chunking.py
  test\_parsers.py
  test\_excel\_io.py      # MUST test that the Tracker sheet is preserved with formulas intact
  test\_orchestrator.py  # MUST test the anti-loop logic specifically
  fixtures/
    sample.pdf
    sample.docx
    sample\_rfp.xlsx     # a small fixture with the same column structure

config.example.yaml
requirements.txt
README.md
.gitignore
```

## 12\. Configuration

Default settings live in `config.py`. A user-supplied YAML overrides these:

```yaml
paths:
  source\_corpus: "D:/AI Knowledge/Complaince Answering Bot/Existing knowledge/Compliances"
  input\_excel\_folder: "D:/AI Knowledge/Complaince Answering Bot/Input Excel"
  output\_excel\_folder: "D:/AI Knowledge/Complaince Answering Bot/Output Excel"
  cache\_folder: "D:/AI Knowledge/Complaince Answering Bot/.rag\_cache"

excel:
  question\_sheet\_name: "Technical (App)"   # the sheet to write answers into
  preserve\_sheets: \["Tracker"]              # sheets to copy through unchanged
  header\_row: 2
  data\_start\_row: 5
  no\_column: "B"
  question\_column: "C"
  type\_column: "D"
  response\_column: "E"
  comments\_column: "F"

vendor:
  name: "BusinessNext"
  description: "banking-focused CRM SaaS vendor headquartered in India"
  region\_focus: "BFSI across India, Middle East, and Asia-Pacific"

customer:
  name: "Maybank"
  context: "tier-1 Malaysian bank regulated by Bank Negara Malaysia under RMiT 2020"

models:
  maker: "gemini-2.5-pro"
  reviewer: "gemini-2.5-flash"
  embeddings: "text-embedding-004"

retrieval:
  top\_k: 10
  low\_threshold: 0.30
  high\_threshold: 0.75

orchestration:
  max\_revisions: 1                # HARD CAP, do not exceed
  per\_question\_token\_budget: 6000
  skip\_reviewer\_on\_high\_confidence: true
```

System prompts are read from the `prompts/` directory so non-developers can edit them without touching code.

## 13\. CLI Interface

The default behavior should "just work" against the configured Windows paths — no flags required when run with the supplied config:

```bash
# Simplest usage — picks up the latest .xlsx from the Input Excel folder
python -m rfp\_responder

# Specify a particular input file
python -m rfp\_responder --input "D:\\AI Knowledge\\Complaince Answering Bot\\Input Excel\\Technical\_\_App\_.xlsx"

# Dry run on first 10 questions
python -m rfp\_responder --limit 10

# Force re-embedding the corpus
python -m rfp\_responder --rebuild

# Override the config file
python -m rfp\_responder --config "C:\\path\\to\\my\_config.yaml"
```

Environment variable: `GEMINI\_API\_KEY` (required). The CLI should fail fast with a clear error message if it's missing.

If multiple `.xlsx` files exist in the input folder, default to the most recently modified one and print which file was selected. Add a `--list` flag to show all available input files.

Print a clear summary at end of run:

```
Input file          : Technical\_\_App\_.xlsx
Output file         : D:\\AI Knowledge\\Complaince Answering Bot\\Output Excel\\
                      Technical\_\_App\_\_answered\_2026-05-09\_1430.xlsx
Total questions     : 124
  high confidence   : 78
  medium confidence : 32
  low confidence    : 14
  needed revision   : 7
  flagged for review: 21
Total tokens used   : 480,000
Estimated cost      : $0.58
Total runtime       : 18 min
```

## 14\. System Prompts (Starting Templates)

### Maker (`prompts/maker\_system.md`)

```
You are a senior presales consultant at {vendor.name}, {vendor.description}, with a strong track record in {vendor.region\_focus}.

You are drafting responses to a Request for Proposal (RFP) from {customer.name}, {customer.context}. The customer is sophisticated and values evidence over marketing claims.

You will receive an RFP question and excerpts from {vendor.name}'s product documentation, security whitepapers, architecture docs, and prior RFP responses. Draft an evidence-grounded answer suitable for direct submission.

CRITICAL RULES:
1. Base your answer ONLY on the provided context. Never invent capabilities or claim functionality not supported by source material. Hallucination in an RFP is far worse than admitting a gap.
2. If the context does not adequately answer the question, set needs\_review=true and explain what the SME should clarify. Do NOT fabricate an answer.
3. Tone: professional, confident, factual. No hype words ("revolutionary", "best-in-class", "world-leading") unless directly quoted from source.
4. For "Single Choice" questions, set single\_choice\_value to "Yes" / "No" / "Partial" / "N/A", then put the explanation in answer\_text.
5. For "Comment" questions, set single\_choice\_value="" and write the full answer in answer\_text.
6. Reference standards (BNM RMiT, ISO 27001, SOC 2, PCI-DSS) only when the source explicitly supports it. Do not invent clause numbers.
7. Confidence: "high" only when source directly answers; "medium" if partial coverage; "low" if stretching.
8. Length: 2-5 sentences typical. Up to a paragraph for technical-architecture questions. Prose only — no bullets, no markdown formatting in answer\_text.

OUTPUT (strict JSON, no markdown fences):
{
  "single\_choice\_value": "Yes" | "No" | "Partial" | "N/A" | "",
  "answer\_text": "...",
  "confidence": "high" | "medium" | "low",
  "sources\_used": \["doc1", "doc2"],
  "needs\_review": true | false,
  "review\_reason": "..."
}
```

### Reviewer (`prompts/reviewer\_system.md`)

```
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
  "issues": \["issue 1", "issue 2", "issue 3"]
}
```

### Maker Revision Prompt (used only when reviewer returns FAIL)

```
Your previous draft was reviewed and the reviewer flagged these issues:

{issues\_bulleted}

Revise your answer to address each issue. Stay grounded in the provided context. If you cannot address an issue without inventing facts, instead set needs\_review=true and explain what the SME should clarify.

Output the same JSON format as before.
```

## 15\. Quality Standards

* **Type hints** throughout (use `from \_\_future\_\_ import annotations`)
* **Pydantic models** for all agent I/O — never trust raw LLM JSON
* **Logging** to both console and `run.log` file in the output folder. Use Python's `logging` module, not `print` (except for the final summary).
* **Error handling**: parse failures, API errors, rate limits should be caught, logged, and the question marked `needs\_review=true` with the error in `Review Notes`. Never let one bad question crash the whole run.
* **Idempotent caching**: re-running with the same corpus should not re-embed
* **No hardcoded paths or credentials in code** — everything via CLI or config (paths in Section 2 are *defaults* in the example YAML, not hardcoded constants)
* **Black + ruff formatted**

## 16\. Testing

Write unit tests for:

* Document parsers (use small fixture files)
* Chunking edge cases (very short text, no paragraph breaks, very long single paragraph)
* The Excel reader: must correctly identify section headers vs questions in a fixture xlsx that mimics the real structure
* The Excel writer: must preserve the Tracker sheet with formulas intact, must write to columns E and F correctly, must append audit columns
* The orchestrator's skip-review logic — specifically test all four branches:

  * High retrieval + high confidence → reviewer skipped
  * Low retrieval + low confidence → reviewer skipped
  * Medium retrieval → reviewer called
  * Reviewer FAIL → exactly one revision, then accept
* Pydantic schema validation rejecting bad LLM output

## 17\. README

The README must include:

* One-line description and the goal
* Setup (env var, install, Windows path notes)
* Quickstart: how to drop a new RFP into the Input Excel folder and run with no arguments
* Configuration: link to `config.example.yaml` with comments explaining each field
* How the maker-reviewer logic works (with the skip-review and revision-cap rules in plain English)
* Cost and runtime estimates (\~$1–5 and 20–60 minutes for a typical RFP)
* Troubleshooting:

  * Missing API key
  * Source folder empty / files don't parse
  * Tracker sheet formulas not recalculating (open the file in Excel — formulas evaluate on open)
* "Adapting to a new RFP / vendor" section explaining how to swap config

## 18\. Acceptance Criteria

The build is done when:

1. With the GEMINI\_API\_KEY env var set and a config pointing at the Windows folders, `python -m rfp\_responder --limit 5` runs end-to-end and produces a valid output Excel.
2. The output Excel opens in Microsoft Excel without errors. The `Tracker` sheet's COUNTA formulas display updated counts reflecting the answers written. The `Technical (App)` sheet has answers in columns E (Single Choice only) and F (all answered rows), with audit columns G–L populated.
3. Section header rows (e.g., the row containing "Business Architecture" in column C with empty column B and Type=Comment) are NOT touched in the output.
4. Unit tests pass (`pytest tests/`), including:

   * The orchestrator anti-loop test (no question ever causes more than one revision call)
   * The Excel I/O test (Tracker sheet formulas are preserved)
5. A run on the full RFP completes in under 60 minutes and costs under $5 (Gemini pricing).
6. Spot-checking 10 randomly chosen `high` confidence answers shows no fabricated facts.
7. Spot-checking 10 randomly chosen `low` confidence / flagged answers shows the reasons are accurate (i.e., the source material genuinely doesn't cover those topics).

## 19\. Step-by-Step Implementation Plan (For You)

Build in this order, testing as you go:

1. **Scaffold** the project structure, `requirements.txt`, `.gitignore`, basic `README.md`, `config.example.yaml`.
2. **Excel reader** (`excel\_io.py` first half) — implement reading from `Technical (App)` sheet starting row 5, classifying rows as section-header / question / empty. Test against a fixture that mirrors the real file structure (header row at 2, section header rows interleaved).
3. **Excel writer** (`excel\_io.py` second half) — implement writing to a new file, preserving the Tracker sheet and its formulas. Verify the output opens cleanly in Excel and the formulas display correct counts.
4. **Document parsers** (`parsers.py`) — implement PDF, DOCX, XLSX, TXT parsing. Unit-test against fixture files.
5. **Chunking** (`chunking.py`) — implement and unit-test edge cases.
6. **Embeddings client** (`embeddings.py`) — Gemini via OpenAI-compatible endpoint, with batching and disk cache.
7. **Retrieval** (`retrieval.py`) — top-K cosine similarity, return chunks + scores.
8. **Pydantic schemas** (`agents/schemas.py`) — define `MakerOutput` and `ReviewerOutput`.
9. **Maker agent** (`agents/maker.py`) — system-prompt-based, Pydantic-validated.
10. **Reviewer agent** (`agents/reviewer.py`) — same pattern, smaller model.
11. **Orchestrator** (`agents/orchestrator.py`) — implement skip-review logic, revision cap, token budget. Test this thoroughly — it is the heart of the system.
12. **CLI** (`cli.py`) — wire everything together with argparse, with sensible defaults that pick up the most recent file from the configured input folder.
13. **Config loading** (`config.py`) — YAML with defaults from Section 12.
14. **End-to-end smoke test** with the small fixture RFP and a tiny corpus.
15. **README** with full setup, usage, and architecture explanation.

When you finish each step, run the relevant tests before moving on. If a test fails, fix it before continuing.

## 20\. Important Things to Get Right

These are the points where similar projects most often go wrong — please pay specific attention:

* **Tracker sheet preservation is mandatory.** The input file has a `Tracker` sheet with COUNTA formulas referencing the `Technical (App)` sheet. Open the input with `openpyxl.load\_workbook()` (without `data\_only=True`) so formulas are preserved as formula strings. Do NOT regenerate the workbook from scratch — copy through. After saving, the formulas re-evaluate when Excel opens the file.
* **Section header detection must be dynamic.** Don't hardcode section names. The rule is: empty `No` (column B) AND `Type=Comment` (column D) → section header → skip. This handles the existing 13 sections and any future sections added without code changes.
* **The anti-loop logic in the orchestrator is the heart of the design.** A bug here causes runaway token bills. Write the test first, then the code.
* **Pydantic validation of LLM output is non-negotiable.** Never insert raw model output into Excel without parsing through Pydantic. Reject and retry once on validation failure.
* **The `sources\_used` field must be cross-validated** against the actually-retrieved chunks. The model will sometimes invent source names. Reject any source name not present in the retrieved set and overwrite with the actual top-3 retrieved doc names.
* **Cache must be invalidated** when any source document changes. Hash file mtimes + chunking config to form the cache key.
* **Logging should make it possible to reconstruct what happened on any single question** — including which chunks were retrieved, what the maker said, whether the reviewer was called, and why.
* **Windows path handling**: use `pathlib.Path` everywhere, and accept either backslash or forward-slash paths in CLI/config. Do not concatenate paths with string addition.

## 21\. What NOT to Build

To keep scope tight:

* No Streamlit/web UI in the first version — CLI only.
* No multi-RFP queue management — one RFP per run.
* No fine-tuning — prompt engineering only.
* No agentic re-retrieval — the maker uses what was retrieved upfront.
* No support for image-based PDFs (OCR) — out of scope, document the limitation.
* No multi-language — English only for v1.
* Do NOT modify the Tracker sheet's formulas or layout — copy through unchanged.

These are explicit out-of-scope items. Don't add them unless asked.

\---

End of build spec. When you're ready, scaffold the project and start with Step 1 of the implementation plan. Confirm any architectural decisions you're uncertain about before writing significant code, but do NOT ask clarifying questions about anything explicitly specified above.

