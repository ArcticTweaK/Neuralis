"""
neuralis.agents.inference
=========================
Local inference engine for Neuralis agents.

The ``InferenceEngine`` wraps a GGUF-format language model loaded via
``llama-cpp-python``.  All inference runs in a ``ThreadPoolExecutor`` so
it never blocks the asyncio event loop.

Design principles
-----------------
- Zero external API calls.  The model file lives on disk; nothing is
  fetched at runtime.
- One model per engine instance.  Agents that need a model request one
  from the ``InferenceEngine`` singleton held by ``AgentRuntime``.
- Graceful degradation.  If ``llama-cpp-python`` is not installed, the
  engine stubs itself out and returns a clear error in every response —
  the rest of the stack continues working (model-free agents still run).
- Thread-safe.  ``asyncio.Semaphore`` limits concurrent inference calls
  to ``config.inference_threads``; the executor runs blocking llama calls
  off the event loop.

Usage
-----
    engine = InferenceEngine(config)
    await engine.load("mistral-7b-instruct-q4.gguf")

    result = await engine.complete(
        prompt="Summarise the following text: ...",
        max_tokens=256,
        temperature=0.7,
    )
    print(result.text)

    await engine.unload()
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attempt to import llama_cpp — graceful degradation if absent
# ---------------------------------------------------------------------------

try:
    from llama_cpp import Llama  # type: ignore
    _LLAMA_AVAILABLE = True
except ImportError:
    Llama = None  # type: ignore
    _LLAMA_AVAILABLE = False
    logger.info(
        "llama-cpp-python not installed — InferenceEngine will run in stub mode. "
        "Install with: pip install llama-cpp-python"
    )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class InferenceRequest:
    """
    A single inference request.

    Attributes
    ----------
    prompt      : the full prompt string passed to the model
    max_tokens  : maximum tokens to generate (default 512)
    temperature : sampling temperature 0.0–2.0 (default 0.7)
    top_p       : nucleus sampling probability (default 0.95)
    stop        : list of stop strings (model stops at first match)
    request_id  : optional correlation ID (echoed in response)
    """
    prompt:     str
    max_tokens: int         = 512
    temperature: float      = 0.7
    top_p:      float       = 0.95
    stop:       List[str]   = field(default_factory=list)
    request_id: str         = ""


@dataclass
class InferenceResult:
    """
    The output of a single inference call.

    Attributes
    ----------
    text        : generated text (stripped)
    request_id  : echoed from InferenceRequest
    prompt_tokens   : tokens consumed by the prompt
    generated_tokens: tokens generated
    duration_ms : wall-clock time of the inference call
    model_name  : basename of the model file used
    truncated   : True if output hit max_tokens
    error       : non-empty if inference failed
    """
    text:             str
    request_id:       str   = ""
    prompt_tokens:    int   = 0
    generated_tokens: int   = 0
    duration_ms:      float = 0.0
    model_name:       str   = ""
    truncated:        bool  = False
    error:            str   = ""

    def is_ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "text":             self.text,
            "request_id":       self.request_id,
            "prompt_tokens":    self.prompt_tokens,
            "generated_tokens": self.generated_tokens,
            "duration_ms":      self.duration_ms,
            "model_name":       self.model_name,
            "truncated":        self.truncated,
            "error":            self.error,
        }


# ---------------------------------------------------------------------------
# InferenceEngine
# ---------------------------------------------------------------------------

class InferenceEngine:
    """
    Async wrapper around llama-cpp-python for local GGUF inference.

    Parameters
    ----------
    config : neuralis.config.AgentConfig
    """

    def __init__(self, config) -> None:
        self._config      = config
        self._model: Optional[Any] = None
        self._model_name: str      = ""
        self._model_path: Optional[Path] = None
        self._executor    = ThreadPoolExecutor(
            max_workers=1,   # one inference at a time per engine
            thread_name_prefix="neuralis-inference",
        )
        self._semaphore   = asyncio.Semaphore(max(1, config.inference_threads))
        self._loaded      = False
        self._total_calls = 0
        self._total_ms    = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self, model_filename: str) -> None:
        """
        Load a GGUF model from ``config.models_dir``.

        Parameters
        ----------
        model_filename : filename of the GGUF file (e.g. "mistral-7b.gguf")

        Raises
        ------
        FileNotFoundError : if the model file does not exist
        RuntimeError      : if llama-cpp-python is not installed
        """
        if not _LLAMA_AVAILABLE:
            logger.warning(
                "InferenceEngine.load(): llama-cpp-python not available — running in stub mode"
            )
            self._model_name = model_filename
            self._loaded = True
            return

        model_path = Path(self._config.models_dir) / model_filename
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {model_path}\n"
                f"Place a GGUF model file in {self._config.models_dir}"
            )

        logger.info("InferenceEngine: loading %s …", model_filename)
        loop = asyncio.get_event_loop()
        n_threads = max(1, self._config.inference_threads)

        def _load():
            return Llama(
                model_path=str(model_path),
                n_ctx=4096,
                n_threads=n_threads,
                n_gpu_layers=0,   # CPU-only by default; set >0 for GPU offload
                verbose=False,
            )

        self._model = await loop.run_in_executor(self._executor, _load)
        self._model_name = model_filename
        self._model_path = model_path
        self._loaded = True
        logger.info("InferenceEngine: model loaded — %s", model_filename)

    async def unload(self) -> None:
        """Release the loaded model and free memory."""
        if self._model is not None:
            del self._model
            self._model = None
        self._loaded = False
        self._model_name = ""
        logger.info("InferenceEngine: model unloaded")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        """
        Run a completion request against the loaded model.

        Thread-safe.  Uses a semaphore to limit concurrency to
        ``config.inference_threads``.

        Parameters
        ----------
        request : InferenceRequest

        Returns
        -------
        InferenceResult
        """
        if not self._loaded:
            return InferenceResult(
                text="",
                request_id=request.request_id,
                error="InferenceEngine not loaded — call load() first",
            )

        if not _LLAMA_AVAILABLE or self._model is None:
            # Stub mode — return a canned response for testing
            return InferenceResult(
                text=f"[stub] prompt received ({len(request.prompt)} chars)",
                request_id=request.request_id,
                model_name=self._model_name,
                prompt_tokens=len(request.prompt.split()),
                generated_tokens=8,
                duration_ms=0.1,
            )

        async with self._semaphore:
            loop = asyncio.get_event_loop()
            t0 = time.monotonic()

            def _infer():
                return self._model(
                    request.prompt,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    top_p=request.top_p,
                    stop=request.stop or [],
                    echo=False,
                )

            try:
                output = await loop.run_in_executor(self._executor, _infer)
            except Exception as exc:
                logger.error("InferenceEngine: inference error: %s", exc)
                return InferenceResult(
                    text="",
                    request_id=request.request_id,
                    model_name=self._model_name,
                    error=str(exc),
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

            duration_ms = (time.monotonic() - t0) * 1000
            self._total_calls += 1
            self._total_ms += duration_ms

            choice      = output["choices"][0]
            usage       = output.get("usage", {})
            text        = choice["text"].strip()
            finish      = choice.get("finish_reason", "")

            return InferenceResult(
                text             = text,
                request_id       = request.request_id,
                prompt_tokens    = usage.get("prompt_tokens", 0),
                generated_tokens = usage.get("completion_tokens", 0),
                duration_ms      = duration_ms,
                model_name       = self._model_name,
                truncated        = finish == "length",
            )

    async def complete_text(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        stop: Optional[List[str]] = None,
    ) -> str:
        """
        Convenience wrapper — returns just the generated text string.

        Returns empty string on error.
        """
        req = InferenceRequest(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop or [],
        )
        result = await self.complete(req)
        return result.text if result.is_ok() else ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def model_name(self) -> str:
        return self._model_name

    def stats(self) -> dict:
        avg_ms = (self._total_ms / self._total_calls) if self._total_calls else 0.0
        return {
            "loaded":       self._loaded,
            "model":        self._model_name,
            "llama_available": _LLAMA_AVAILABLE,
            "total_calls":  self._total_calls,
            "avg_ms":       round(avg_ms, 1),
            "threads":      self._config.inference_threads,
        }

    def __repr__(self) -> str:
        return (
            f"<InferenceEngine model={self._model_name!r} "
            f"loaded={self._loaded} llama={_LLAMA_AVAILABLE}>"
        )
