import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import openpyxl

from rfp_responder.config import AppConfig, load_config
from rfp_responder.parsers import walk_corpus
from rfp_responder.chunking import chunk_corpus
from rfp_responder.embeddings import EmbeddingClient, EmbeddingCache
from rfp_responder.retrieval import retrieve
from rfp_responder.excel_io import read_rfp_sheet, write_output_excel, ROW_TYPE_QUESTION
from rfp_responder.agents.maker import MakerAgent
from rfp_responder.agents.reviewer import ReviewerAgent
from rfp_responder.agents.orchestrator import Orchestrator
from rfp_responder.agents.comparer import ComparerAgent

app = FastAPI(title="Compliance Answering Bot")

# Directory setup
BASE_DIR = Path(os.environ.get("APP_BASE_DIR", Path(__file__).parent))
UPLOAD_DIR = Path(os.environ.get("INPUT_EXCEL_FOLDER", BASE_DIR / "Input Excel"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_EXCEL_FOLDER", BASE_DIR / "Output Excel"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Templates
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

from starlette.concurrency import run_in_threadpool

import uuid
from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import openpyxl

# In-memory dictionary to store task status
TASKS = {}

@app.post("/process")
async def process_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    custom_prompt: Optional[str] = Form(None),
    form_api_key: Optional[str] = Form(None),
    api_provider: Optional[str] = Form("gemini"),
    maker_model: Optional[str] = Form(None),
    reviewer_model: Optional[str] = Form(None)
):
    api_key = form_api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"error": "API Key is not set. Please provide it in the form."}

    # Save uploaded file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    input_filename = f"upload_{timestamp}_{file.filename}"
    input_path = UPLOAD_DIR / input_filename
    
    content = await file.read()
    with open(input_path, "wb") as f:
        f.write(content)

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "processing", "progress": 0, "filename": "", "download_url": "", "error": ""}

    # Add the heavy workload to background tasks
    background_tasks.add_task(
        _process_heavy_task, task_id, input_path, api_key, custom_prompt, api_provider, maker_model, reviewer_model
    )
    
    return {"task_id": task_id}

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in TASKS:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return TASKS[task_id]

@app.post("/cancel/{task_id}")
async def cancel_task(task_id: str):
    if task_id in TASKS:
        TASKS[task_id]['cancelled'] = True
        return {"status": "cancelled"}
    return {"error": "Task not found"}

def _process_heavy_task(task_id, input_path, api_key, custom_prompt, api_provider, form_maker_model, form_reviewer_model):
    try:
        start_time = time.time()
        cfg = load_config(BASE_DIR / "config.yaml")

        # Output path
        out_name = f"{input_path.stem}_answered.xlsx"
        output_path = OUTPUT_DIR / out_name

        # Read questions
        wb = openpyxl.load_workbook(str(input_path))
        rows = read_rfp_sheet(wb, cfg)
        wb.close()
        question_rows = [r for r in rows if r.row_type == ROW_TYPE_QUESTION]

        # Determine models based on provider
        if api_provider == "openai":
            maker_model = form_maker_model or "gpt-4o"
            reviewer_model = form_reviewer_model or "gpt-4o"
            embed_model = "text-embedding-3-small"
        elif api_provider == "claude":
            maker_model = form_maker_model or "claude-3-5-sonnet-20241022"
            reviewer_model = form_reviewer_model or "claude-3-5-sonnet-20241022"
            embed_model = "text-embedding-3-small" 
        elif api_provider == "openrouter":
            maker_model = form_maker_model or "deepseek/deepseek-v4-flash"
            reviewer_model = form_reviewer_model or "deepseek/deepseek-v4-flash"
            embed_model = "openai/text-embedding-3-small"
        else:
            maker_model = form_maker_model or cfg.models.maker
            reviewer_model = form_reviewer_model or cfg.models.reviewer
            embed_model = cfg.models.embeddings

        if api_provider == "openrouter":
            if maker_model and not maker_model.startswith("openrouter/"):
                maker_model = f"openrouter/{maker_model}"
            if reviewer_model and not reviewer_model.startswith("openrouter/"):
                reviewer_model = f"openrouter/{reviewer_model}"

        # Build corpus
        parsed = walk_corpus(cfg.paths.source_corpus)
        chunks = chunk_corpus(parsed)
        
        embed_client = EmbeddingClient(api_key=api_key, model=embed_model, provider=api_provider)
        cache = EmbeddingCache(cfg.paths.cache_folder, embed_client)
        chunks, chunk_embeddings = cache.get_or_build(chunks, cfg.paths.source_corpus, chunk_size=3200, overlap=800)

        # Agents
        maker = MakerAgent(api_key, maker_model, cfg.prompts_dir, cfg.vendor, cfg.customer, provider=api_provider)
        reviewer = ReviewerAgent(api_key, reviewer_model, cfg.prompts_dir, provider=api_provider)
        orch = Orchestrator(maker, reviewer, cfg)
        comparer = ComparerAgent(api_key, reviewer_model, provider=api_provider)

        results = {}
        total_tokens = 0
        total_questions = len(question_rows)
        for i, rfp_row in enumerate(question_rows):
            if TASKS.get(task_id, {}).get("cancelled", False):
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = "Processing stopped by user."
                return

            # Update progress
            progress = int((i / total_questions) * 100)
            TASKS[task_id]["progress"] = progress
            try:
                q_embedding = embed_client.embed_query(rfp_row.question_text)
                retrieved = retrieve(q_embedding, chunk_embeddings, chunks, top_k=cfg.retrieval.top_k)
                retrieved_source_names = {rc.chunk.source for rc in retrieved}

                orch_result = orch.process_question(
                    question=rfp_row.question_text,
                    question_type=rfp_row.question_type,
                    chunks=retrieved,
                    retrieved_source_names=retrieved_source_names,
                )
                total_tokens += orch_result.tokens_used
                out = orch_result.final_output
                top_score = retrieved[0].score if retrieved else 0.0

                comp_result, _ = comparer.compare(rfp_row.existing_comment, out.answer_text)

                results[rfp_row.row_number] = {
                    "question": rfp_row.question_text,
                    "single_choice_value": out.single_choice_value,
                    "answer_text": out.answer_text,
                    "confidence": out.confidence,
                    "top_retrieval_score": top_score,
                    "sources": out.sources_used[:3],
                    "needs_review": out.needs_review,
                    "review_notes": orch_result.review_notes,
                    "revision_count": orch_result.revision_count,
                    "comparison": comp_result,
                }
            except Exception as exc:
                results[rfp_row.row_number] = {
                    "question": rfp_row.question_text,
                    "single_choice_value": "",
                    "answer_text": "",
                    "confidence": "low",
                    "top_retrieval_score": 0.0,
                    "sources": [],
                    "needs_review": True,
                    "review_notes": f"Error: {exc}",
                    "revision_count": 0,
                    "comparison": "",
                }

        write_output_excel(input_path, output_path, results, cfg)

        TASKS[task_id]["status"] = "completed"
        TASKS[task_id]["progress"] = 100
        TASKS[task_id]["filename"] = out_name
        TASKS[task_id]["download_url"] = f"/download/{out_name}"
        TASKS[task_id]["total_tokens"] = total_tokens
        TASKS[task_id]["results"] = results
        
        elapsed = time.time() - start_time
        TASKS[task_id]["time_taken"] = round(elapsed, 1)
        TASKS[task_id]["maker_model"] = maker_model
        TASKS[task_id]["reviewer_model"] = reviewer_model
        
        is_free = "free" in maker_model.lower() and "free" in reviewer_model.lower()
        if is_free:
            TASKS[task_id]["cost_incurred"] = "0.00"
        elif api_provider == "gemini" and "2.5-pro" in maker_model:
            TASKS[task_id]["cost_incurred"] = round((total_tokens / 1000000) * 14.0, 4)
        else:
            TASKS[task_id]["cost_incurred"] = "Varies (See Provider)"
            
        TASKS[task_id]["questions_processed"] = total_questions

    except Exception as e:
        TASKS[task_id]["status"] = "error"
        TASKS[task_id]["error"] = str(e)



@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = OUTPUT_DIR / filename
    if file_path.exists():
        return FileResponse(path=file_path, filename=filename, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    return {"error": "File not found."}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
