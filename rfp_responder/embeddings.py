from __future__ import annotations

"""Gemini embedding client with disk caching (keyed by corpus hash + chunking config).

Uses the new google-genai SDK (google.genai) for embeddings, since text-embedding-004
is NOT available on the OpenAI-compatible endpoint (/v1beta/openai/).
The LLM agents (maker/reviewer) continue to use the OpenAI-compat endpoint.

New SDK reference: https://github.com/googleapis/python-genai
Call pattern: client.models.embed_content(model=..., contents=[...])
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .chunking import TextChunk

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100   # Gemini batchEmbedContents limit is 100 items per request


# ---------------------------------------------------------------------------
# Embedding client — new google.genai SDK
# ---------------------------------------------------------------------------

class EmbeddingClient:
    """Wraps embeddings for Gemini and other providers."""

    def __init__(self, api_key: str, model: str = "text-embedding-004", provider: str = "gemini"):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        
        if provider == "gemini":
            import google.genai as genai  # type: ignore
            import google.genai.types as genai_types  # type: ignore
            self._client = genai.Client(api_key=api_key)
            self._types = genai_types
        else:
            from openai import OpenAI
            # For non-Gemini, we use OpenAI for embeddings (or compatible endpoint)
            base_url = None
            if provider == "openrouter":
                base_url = "https://openrouter.ai/api/v1"
            self._client = OpenAI(api_key=api_key, base_url=base_url)

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        all_vecs: list[list[float]] = []
        for batch_start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[batch_start : batch_start + _BATCH_SIZE]
            attempts = 0
            while True:
                attempts += 1
                try:
                    if self.provider == "gemini":
                        result = self._client.models.embed_content(
                            model=self.model,
                            contents=batch,
                            config=self._types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
                        )
                        if getattr(result, "embeddings", None) is None:
                            raise ValueError("No embeddings in response")
                        vecs = [emb.values for emb in result.embeddings]
                    else:
                        extra_kwargs = {"encoding_format": "float"} if self.provider == "openrouter" else {}
                        result = self._client.embeddings.create(input=batch, model=self.model, **extra_kwargs)
                        if getattr(result, "data", None) is None:
                            raise ValueError("No data in response")
                        vecs = [data.embedding for data in result.data]
                    all_vecs.extend(vecs)
                    break
                except OSError as exc:
                    if attempts < 6:
                        sleep = 5 * attempts
                        logger.warning("OS-level error in embed_texts (attempt %d/6): %s. Retrying in %ds...", attempts, exc, sleep)
                        time.sleep(sleep)
                    else:
                        raise
                except Exception as exc:
                    if attempts < 4:
                        time.sleep(2)
                    else:
                        raise
        return _l2_normalize(np.array(all_vecs, dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        attempts = 0
        while True:
            attempts += 1
            try:
                if self.provider == "gemini":
                    result = self._client.models.embed_content(
                        model=self.model,
                        contents=[text],
                        config=self._types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
                    )
                    if getattr(result, "embeddings", None) is None or not result.embeddings:
                        raise ValueError("No embeddings in response")
                    vec = np.array(result.embeddings[0].values, dtype=np.float32)
                else:
                    extra_kwargs = {"encoding_format": "float"} if self.provider == "openrouter" else {}
                    result = self._client.embeddings.create(input=[text], model=self.model, **extra_kwargs)
                    if getattr(result, "data", None) is None or not result.data:
                        raise ValueError("No data in response")
                    vec = np.array(result.data[0].embedding, dtype=np.float32)
                return _l2_normalize(vec.reshape(1, -1))[0]
            except OSError as exc:
                if attempts < 6:
                    sleep = 5 * attempts
                    logger.warning("OS-level error in embed_query (attempt %d/6): %s. Retrying in %ds...", attempts, exc, sleep)
                    time.sleep(sleep)
                else:
                    raise
            except Exception as exc:
                if attempts < 4:
                    time.sleep(2)
                else:
                    raise


def _l2_normalize(arr: np.ndarray) -> np.ndarray:
    """L2-normalize each row of a 2-D array (or a 1-D vector)."""
    if arr.ndim == 1:
        norm = np.linalg.norm(arr)
        return arr / (norm if norm > 0 else 1.0)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return arr / norms


# ---------------------------------------------------------------------------
# Disk cache — keyed by corpus hash + chunking config
# ---------------------------------------------------------------------------

class EmbeddingCache:
    """Manage embeddings cache keyed by corpus hash + config."""

    def __init__(self, cache_dir: Path, client: EmbeddingClient):
        self.cache_dir = cache_dir
        self.client = client
        cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cached(self, corpus_dir: Path, chunk_size: int, overlap: int) -> Optional[tuple[list[TextChunk], np.ndarray]]:
        """Check if cache exists and return it, otherwise return None."""
        cache_key = self._compute_key(corpus_dir, chunk_size, overlap)
        return self._try_load(cache_key)

    def get_or_build(
        self,
        chunks: list[TextChunk],
        corpus_dir: Path,
        chunk_size: int,
        overlap: int,
    ) -> tuple[list[TextChunk], np.ndarray]:
        """Return (chunks, embeddings) — from cache if valid, else re-embed."""
        cache_key = self._compute_key(corpus_dir, chunk_size, overlap)
        cached = self._try_load(cache_key)
        if cached is not None:
            logger.info("Embeddings loaded from cache (key=%s…)", cache_key[:8])
            return cached
        logger.info("Building embeddings for %d chunks…", len(chunks))
        embeddings = self._embed_chunks(chunks)
        self._save(cache_key, chunks, embeddings)
        return chunks, embeddings

    def invalidate(self) -> None:
        """Delete all cache files."""
        for p in self.cache_dir.glob("*.npz"):
            p.unlink()
        for p in self.cache_dir.glob("*.json"):
            p.unlink()
        logger.info("Embedding cache cleared")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_key(self, corpus_dir: Path, chunk_size: int, overlap: int) -> str:
        h = hashlib.sha256()
        for p in sorted(corpus_dir.rglob("*")):
            if p.is_file():
                h.update(p.name.encode())
                h.update(str(p.stat().st_mtime).encode())
        h.update(f"{chunk_size}-{overlap}-{self.client.model}".encode())
        return h.hexdigest()

    def _try_load(
        self, key: str
    ) -> Optional[tuple[list[TextChunk], np.ndarray]]:
        meta_path = self.cache_dir / f"{key}.json"
        npz_path  = self.cache_dir / f"{key}.npz"
        if not meta_path.exists() or not npz_path.exists():
            return None
        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                meta = json.load(fh)
            arr = np.load(str(npz_path))["embeddings"]
            chunks = [
                TextChunk(
                    text=m["text"],
                    source=m["source"],
                    page=m.get("page"),
                    section=m.get("section"),
                    chunk_index=m.get("chunk_index", 0),
                )
                for m in meta
            ]
            return chunks, arr
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache load failed (%s) — will rebuild", exc)
            return None

    def _save(self, key: str, chunks: list[TextChunk], embeddings: np.ndarray) -> None:
        meta = [
            {
                "text": c.text,
                "source": c.source,
                "page": c.page,
                "section": c.section,
                "chunk_index": c.chunk_index,
            }
            for c in chunks
        ]
        meta_path = self.cache_dir / f"{key}.json"
        npz_path  = self.cache_dir / f"{key}.npz"
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        np.savez_compressed(str(npz_path), embeddings=embeddings)
        logger.info("Embeddings cached to %s", self.cache_dir)

    def _embed_chunks(self, chunks: list[TextChunk]) -> np.ndarray:
        from tqdm import tqdm  # type: ignore

        texts = [c.text for c in chunks]
        all_vecs: list[list[float]] = []

        for batch_start in tqdm(
            range(0, len(texts), _BATCH_SIZE), desc="Embedding", unit="batch"
        ):
            batch = texts[batch_start : batch_start + _BATCH_SIZE]
            attempts = 0
            while True:
                attempts += 1
                try:
                    if self.client.provider == "gemini":
                        result = self.client._client.models.embed_content(
                            model=self.client.model,
                            contents=batch,
                            config=self.client._types.EmbedContentConfig(
                                task_type="RETRIEVAL_DOCUMENT"
                            ),
                        )
                        if getattr(result, "embeddings", None) is None:
                            raise ValueError(f"No embeddings in response: {result}")
                        vecs = [emb.values for emb in result.embeddings]
                    else:
                        extra_kwargs = {"encoding_format": "float"} if self.client.provider == "openrouter" else {}
                        result = self.client._client.embeddings.create(input=batch, model=self.client.model, **extra_kwargs)
                        if getattr(result, "data", None) is None:
                            raise ValueError(f"No data in response: {result}")
                        vecs = [data.embedding for data in result.data]
                    all_vecs.extend(vecs)
                    break
                except OSError as exc:  # noqa: BLE001
                    # Windows socket errors (Errno 22, etc.) under concurrent load
                    if attempts < 6:
                        sleep = 5 * attempts
                        logger.warning("OS-level error in _embed_chunks (attempt %d/6): %s. Retrying in %ds...", attempts, exc, sleep)
                        time.sleep(sleep)
                    else:
                        logger.error("_embed_chunks OS error failed permanently: %s", exc)
                        raise
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc)
                    if "429" in exc_str or "quota" in exc_str.lower() or "rate" in exc_str.lower():
                        logger.warning("Rate limited — sleeping 30s then retrying")
                        time.sleep(30)
                    else:
                        if attempts < 5:
                            logger.warning(f"Embedding batch failed (attempt {attempts}/5): {exc}. Retrying in 5s...")
                            time.sleep(5)
                        else:
                            logger.error("Embedding batch failed permanently: %s", exc)
                            raise

        arr = np.array(all_vecs, dtype=np.float32)
        return _l2_normalize(arr)
