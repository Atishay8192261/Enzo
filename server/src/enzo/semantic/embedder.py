"""The embedding provider (spec §1.3.1 ``embed()``, §6.3 steps 2-3), swappable behind one interface.

The default runs on the user's computer: no key, no account, nothing sent anywhere (ADR 0030).
Inference runs in a worker thread, one core per call, so the gateway keeps answering while older
chats are being indexed.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx2

from enzo.semantic.model import MINILM, ModelError, ModelSpec, Tuning, fetch, folder_for, verify

log = logging.getLogger("enzo.semantic")

#: query: what the user asks; document: what is stored. Some models embed the two differently.
type Use = Literal["query", "document"]
#: ready: in use · preparing: downloading or loading · unavailable: cannot run on this computer
#: · failed: could not be fetched (retried later) · off: the user turned it off.
type State = Literal["ready", "preparing", "unavailable", "failed", "off"]

#: Texts per model call: small, so a question never waits long behind a batch of old chats.
BATCH = 8


class EmbedderNotReadyError(Exception):
    """The model is not loaded (yet): callers search by words alone meanwhile."""


class Embedder(Protocol):
    """What the index needs from an embedding model, whoever runs it."""

    @property
    def id(self) -> str:
        """Stored with each vector; vectors of another id are embedded again."""
        ...

    @property
    def tuning(self) -> Tuning: ...

    @property
    def state(self) -> State: ...

    @property
    def ready(self) -> bool: ...

    async def prepare(self) -> bool:
        """Get ready (download, load). False when it cannot now; ``problem`` says why."""
        ...

    async def embed(self, texts: Sequence[str], *, use: Use) -> list[list[float]]:
        """One unit-length vector per text. ``EmbedderNotReadyError`` before ``prepare``."""
        ...


def _runtime() -> Any:  # noqa: ANN401 - onnxruntime ships no type information
    import onnxruntime  # noqa: PLC0415 - heavy, and only for search by meaning

    onnxruntime.set_default_logger_severity(3)
    return onnxruntime


class LocalEmbedder:
    """all-MiniLM-L6-v2 run with ONNX Runtime: mean-pooled, normalized, 384 numbers per text."""

    def __init__(
        self,
        models: Path,
        spec: ModelSpec = MINILM,
        *,
        http: httpx2.Client | None = None,
        runtime: Callable[[], Any] = _runtime,
    ) -> None:
        self.spec = spec
        self.models = models
        self._http = http
        self._runtime = runtime
        self._session: Any = None
        self._tokenizer: Any = None
        self._state: State = "preparing"
        self.problem: str | None = None
        self._lock = asyncio.Lock()

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def tuning(self) -> Tuning:
        return self.spec.tuning

    @property
    def state(self) -> State:
        return self._state

    @property
    def ready(self) -> bool:
        return self._session is not None

    @property
    def folder(self) -> Path:
        return folder_for(self.models, self.spec)

    async def prepare(self) -> bool:
        async with self._lock:
            if self._session is not None:
                return True
            if self._state == "unavailable":
                return False
            try:
                runtime = self._runtime()
            except ImportError as exc:  # no ONNX Runtime build for this computer
                self._state, self.problem = "unavailable", f"cannot run here ({exc})"
                log.warning("semantic.model_unavailable", extra={"reason": str(exc)})
                return False
            self._state = "preparing"
            try:
                await asyncio.to_thread(self._fetch_and_load, runtime)
            except (ModelError, httpx2.HTTPError, OSError) as exc:
                self._state, self.problem = "failed", str(exc)
                log.warning("semantic.model_failed", extra={"reason": str(exc)})
                return False
            self._state, self.problem = "ready", None
            log.info("semantic.model_ready", extra={"model": self.id})
            return True

    async def embed(self, texts: Sequence[str], *, use: Use) -> list[list[float]]:
        del use  # this model embeds questions and passages the same way
        if self._session is None:
            raise EmbedderNotReadyError(self.id)
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            out += await asyncio.to_thread(self._run, list(texts[i : i + BATCH]))
        return out

    def _fetch_and_load(self, runtime: Any) -> None:  # noqa: ANN401 - the onnxruntime module
        try:
            verify(self.models, self.spec)
        except ModelError:
            log.info("semantic.model_download", extra={"model": self.id, "bytes": self.spec.size})
            if self._http is not None:
                fetch(self.models, self.spec, self._http)
            else:
                with httpx2.Client(timeout=httpx2.Timeout(60.0, connect=15.0)) as http:
                    fetch(self.models, self.spec, http)
            verify(self.models, self.spec)
        from tokenizers import Tokenizer  # noqa: PLC0415 - only once the model is used

        tokenizer = Tokenizer.from_file(str(self.folder / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=self.spec.max_tokens)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")  # noqa: S106 - a token name
        options = runtime.SessionOptions()
        options.intra_op_num_threads = 1  # one core per call: the laptop stays responsive
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False  # ~70 MB less memory for long chunks
        options.log_severity_level = 3
        self._session = runtime.InferenceSession(
            str(self.folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = tokenizer

    def _run(self, texts: list[str]) -> list[list[float]]:
        import numpy as np  # noqa: PLC0415 - comes with onnxruntime

        encoded = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        (hidden, *_rest) = self._session.run(
            None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
        )
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors = pooled / np.clip(norms, 1e-12, None)
        return [[float(x) for x in row] for row in vectors]


class Off:
    """Search by meaning turned off (``ENZO_EMBEDDINGS=off``): Enzo finds things by their words."""

    id = "off"
    tuning = MINILM.tuning
    ready = False
    problem: str | None = None

    @property
    def state(self) -> State:
        return "off"

    async def prepare(self) -> bool:
        return False

    async def embed(self, texts: Sequence[str], *, use: Use) -> list[list[float]]:
        raise EmbedderNotReadyError(self.id)
