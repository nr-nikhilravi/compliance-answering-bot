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
import asyncio
import threading
from fastapi.responses import StreamingResponse
import json

@app.post("/process")
async def process_file(
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    input_filename = f"upload_{timestamp}_{file.filename}"
    input_path = UPLOAD_DIR / input_filename
    
    content = await file.read()
    with open(input_path, "wb") as f:
        f.write(content)

    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def background_worker():
        try:
            _process_heavy_task(queue, loop, input_path, api_key, custom_prompt, api_provider, maker_model, reviewer_model)
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put({"status": "error", "error": str(e)}), loop)

    thread = threading.Thread(target=background_worker)
    thread.start()

    async def stream_generator():
        while True:
            msg = await queue.get()
            yield json.dumps(msg) + "\n"
            if msg.get("status") in ("completed", "error"):
                break

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


def _process_heavy_task(queue, loop, input_path, api_key, custom_prompt, api_provider, form_maker_model, form_reviewer_model):
    def send_update(msg):
        asyncio.run_coroutine_threadsafe(queue.put(msg), loop)
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
            send_update({"status": "processing", "progress": int((i / total_questions) * 100)})
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

        elapsed = time.time() - start_time
        time_taken = round(elapsed, 1)
        
        is_free = "free" in maker_model.lower() and "free" in reviewer_model.lower()
        if is_free:
            cost_incurred = "0.00"
        elif api_provider == "gemini" and "2.5-pro" in maker_model:
            cost_incurred = str(round((total_tokens / 1000000) * 14.0, 4))
        else:
            cost_incurred = "Varies (See Provider)"

        send_update({
            "status": "completed",
            "progress": 100,
            "filename": out_name,
            "download_url": f"/download/{out_name}",
            "total_tokens": total_tokens,
            "results": results,
            "time_taken": time_taken,
            "maker_model": maker_model,
            "reviewer_model": reviewer_model,
            "cost_incurred": cost_incurred,
            "questions_processed": total_questions
        })

    except Exception as e:
        send_update({"status": "error", "error": str(e)})



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
