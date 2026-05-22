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

app = FastAPI(title="Compliance Answering Bot")

# Directory setup
BASE_DIR = Path(os.environ.get("APP_BASE_DIR", Path(__file__).parent))
UPLOAD_DIR = Path(os.environ.get("INPUT_EXCEL_FOLDER", BASE_DIR / "Input Excel"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_EXCEL_FOLDER", BASE_DIR / "Output Excel"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Templates
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------------------
# RAG cache auto-build on startup
# ---------------------------------------------------------------------------
import logging
import threading

logger = logging.getLogger("rag_cache_builder")

_cache_status = {
    "state": "idle",       # idle | building | ready | error
    "message": "",
    "chunks": 0,
    "documents": 0,
}
_cache_status_lock = threading.Lock()

def _set_cache_status(**kwargs):
    with _cache_status_lock:
        _cache_status.update(kwargs)

def _build_cache_on_startup():
    """Check if RAG cache exists; if not, parse + chunk + embed the corpus."""
    try:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            _set_cache_status(state="error", message="GEMINI_API_KEY not set — cache cannot be built automatically. Provide it via the web UI per-request instead.")
            logger.warning("GEMINI_API_KEY not set — skipping auto cache build")
            return

        cfg = load_config(BASE_DIR / "config.yaml")
        embed_client = EmbeddingClient(api_key=api_key, model=cfg.models.embeddings)
        cache = EmbeddingCache(cfg.paths.cache_folder, embed_client)

        # Check if cache already exists
        cached = cache.get_cached(cfg.paths.source_corpus, chunk_size=2000, overlap=400)
        if cached is not None:
            chunks, _ = cached
            _set_cache_status(state="ready", message="Cache loaded from disk", chunks=len(chunks))
            logger.info("RAG cache already exists with %d chunks — no rebuild needed", len(chunks))
            return

        # Cache miss — need to build
        _set_cache_status(state="building", message="Parsing documents...")
        logger.info("RAG cache not found — starting auto-build...")

        parsed = walk_corpus(cfg.paths.source_corpus)
        _set_cache_status(state="building", message=f"Parsed {len(parsed)} documents. Chunking...", documents=len(parsed))
        logger.info("Parsed %d documents from corpus", len(parsed))

        chunks = chunk_corpus(parsed)
        _set_cache_status(state="building", message=f"Embedding {len(chunks)} chunks (this may take a few minutes)...", chunks=len(chunks))
        logger.info("Created %d chunks — now embedding...", len(chunks))

        chunks, embeddings = cache.get_or_build(chunks, cfg.paths.source_corpus, chunk_size=2000, overlap=400)

        _set_cache_status(state="ready", message=f"Cache built successfully: {len(chunks)} chunks, embeddings shape {embeddings.shape}", chunks=len(chunks))
        logger.info("RAG cache built: %d chunks, shape %s", len(chunks), embeddings.shape)

    except Exception as e:
        _set_cache_status(state="error", message=f"Cache build failed: {e}")
        logger.exception("RAG cache auto-build failed: %s", e)

@app.on_event("startup")
async def startup_event():
    """Launch cache build in a background thread so the server starts immediately."""
    thread = threading.Thread(target=_build_cache_on_startup, daemon=True)
    thread.start()
    logger.info("RAG cache builder started in background thread")

@app.get("/cache-status")
async def cache_status():
    with _cache_status_lock:
        return dict(_cache_status)

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
    cancel_event = threading.Event()

    def background_worker():
        try:
            _process_heavy_task(queue, loop, input_path, api_key, custom_prompt, api_provider, maker_model, reviewer_model, cancel_event)
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put({"status": "error", "error": str(e)}), loop)

    thread = threading.Thread(target=background_worker)
    thread.start()

    async def stream_generator():
        try:
            while True:
                msg = await queue.get()
                yield json.dumps(msg) + "\n"
                if msg.get("status") in ("completed", "error"):
                    break
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    return StreamingResponse(stream_generator(), media_type="application/x-ndjson")


def _process_heavy_task(queue, loop, input_path, api_key, custom_prompt, api_provider, form_maker_model, form_reviewer_model, cancel_event):
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

        embed_client = EmbeddingClient(api_key=api_key, model=embed_model, provider=api_provider)
        cache = EmbeddingCache(cfg.paths.cache_folder, embed_client)
        
        cached_data = cache.get_cached(cfg.paths.source_corpus, chunk_size=2000, overlap=400)
        if cached_data is not None:
            chunks, chunk_embeddings = cached_data
        else:
            # Build corpus only if not cached
            parsed = walk_corpus(cfg.paths.source_corpus)
            chunks = chunk_corpus(parsed)
            chunks, chunk_embeddings = cache.get_or_build(chunks, cfg.paths.source_corpus, chunk_size=2000, overlap=400)

        # Agents
        maker = MakerAgent(api_key, maker_model, cfg.prompts_dir, cfg.vendor, cfg.customer, provider=api_provider, custom_instructions=custom_prompt)
        reviewer = ReviewerAgent(api_key, reviewer_model, cfg.prompts_dir, provider=api_provider)
        orch = Orchestrator(maker, reviewer, cfg)

        results = {}
        total_tokens = 0
        total_questions = len(question_rows)
        error_count = 0
        
        # Send initial status
        send_update({
            "status": "processing", 
            "progress": 0,
            "current": 0,
            "total": total_questions,
            "errors": 0
        })

        import concurrent.futures

        def _process_single_question(rfp_row):
            try:
                q_embedding = embed_client.embed_query(rfp_row.question_text)
                retrieved = retrieve(q_embedding, chunk_embeddings, chunks, top_k=cfg.retrieval.top_k)
                retrieved_source_names = {rc.chunk.source for rc in retrieved}

                orch_result = orch.process_question(
                    question=rfp_row.question_text,
                    question_type=rfp_row.question_type,
                    chunks=retrieved,
                    retrieved_source_names=retrieved_source_names,
                    existing_answer=rfp_row.existing_comment,
                )
                out = orch_result.final_output
                top_score = retrieved[0].score if retrieved else 0.0

                return {
                    "row_number": rfp_row.row_number,
                    "tokens": orch_result.tokens_used,
                    "error": False,
                    "result_dict": {
                        "question": rfp_row.question_text,
                        "single_choice_value": out.single_choice_value,
                        "answer_text": out.answer_text,
                        "confidence": out.confidence,
                        "top_retrieval_score": top_score,
                        "sources": out.sources_used[:3],
                        "needs_review": out.needs_review,
                        "review_notes": orch_result.review_notes,
                        "revision_count": orch_result.revision_count,
                        "comparison": "",
                    }
                }
            except Exception as exc:
                return {
                    "row_number": rfp_row.row_number,
                    "tokens": 0,
                    "error": True,
                    "result_dict": {
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
                }

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_to_row = {executor.submit(_process_single_question, r): r for r in question_rows}
                completed = 0
                for future in concurrent.futures.as_completed(future_to_row):
                    if cancel_event.is_set():
                        break
                    completed += 1
                    res = future.result()
                    results[res["row_number"]] = res["result_dict"]
                    total_tokens += res["tokens"]
                    if res["error"]:
                        error_count += 1
                    
                    send_update({
                        "status": "processing", 
                        "progress": int((completed / total_questions) * 100),
                        "current": completed,
                        "total": total_questions,
                        "errors": error_count,
                        "filename": out_name,
                        "download_url": f"/download/{out_name}"
                    })
        finally:
            if results:
                try:
                    write_output_excel(input_path, output_path, results, cfg)
                except Exception as e:
                    print(f"Failed to write partial output: {e}")

        elapsed = time.time() - start_time
        time_taken = round(elapsed, 1)
        
        is_free = "free" in maker_model.lower() and "free" in reviewer_model.lower()
        if is_free:
            cost_incurred = "0.00"
        elif api_provider == "gemini" and "2.5-pro" in maker_model:
            cost_incurred = str(round((total_tokens / 1000000) * 14.0, 4))
        else:
            cost_incurred = "Varies (See Provider)"

        # Save job metadata alongside the output file
        job_meta = {
            "filename": out_name,
            "download_url": f"/download/{out_name}",
            "questions_processed": total_questions,
            "errors": error_count,
            "time_taken": time_taken,
            "total_tokens": total_tokens,
            "maker_model": maker_model,
            "reviewer_model": reviewer_model,
            "cost_incurred": cost_incurred,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            meta_path = output_path.with_suffix(".meta.json")
            with open(meta_path, "w", encoding="utf-8") as mf:
                json.dump(job_meta, mf, ensure_ascii=False, indent=2)
        except Exception:
            pass  # Don't fail the job if metadata save fails

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
            "questions_processed": total_questions,
            "errors": error_count
        })

    except Exception as e:
        send_update({"status": "error", "error": str(e)})



@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = OUTPUT_DIR / filename
    if file_path.exists():
        return FileResponse(path=file_path, filename=filename, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    return {"error": "File not found."}

@app.get("/last-job")
async def last_job():
    """Return metadata for the most recently completed job."""
    latest_meta = None
    latest_mtime = 0
    for meta_path in OUTPUT_DIR.glob("*.meta.json"):
        if meta_path.is_file():
            mtime = meta_path.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_meta = meta_path
    if latest_meta:
        try:
            with open(latest_meta, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

@app.get("/history")
async def get_history():
    import time as _time
    now = _time.time()
    ten_days_ago = now - (10 * 24 * 60 * 60)
    
    history_list = []
    for file_path in OUTPUT_DIR.glob("*_answered.xlsx"):
        if file_path.is_file():
            mtime = file_path.stat().st_mtime
            if mtime >= ten_days_ago:
                entry = {
                    "filename": file_path.name,
                    "timestamp": mtime,
                    "download_url": f"/download/{file_path.name}",
                    "questions_processed": None,
                    "time_taken": None,
                }
                # Try to load metadata
                meta_path = file_path.with_suffix(".meta.json")
                if meta_path.exists():
                    try:
                        with open(meta_path, "r", encoding="utf-8") as mf:
                            meta = json.load(mf)
                        entry["questions_processed"] = meta.get("questions_processed")
                        entry["time_taken"] = meta.get("time_taken")
                        entry["errors"] = meta.get("errors", 0)
                        entry["maker_model"] = meta.get("maker_model", "")
                    except Exception:
                        pass
                history_list.append(entry)
    
    # Sort by newest first
    history_list.sort(key=lambda x: x["timestamp"], reverse=True)
    
    for item in history_list:
        item["date"] = datetime.fromtimestamp(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        
    return {"history": history_list}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
