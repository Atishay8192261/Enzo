"""The local embedding model: which bytes, from where, and how they are checked (ADR 0030).

The model is not in the package (90 MB): Enzo downloads it once, the first time it starts, into
its private data folder, and refuses any download whose SHA-256 differs from the pinned one, like
the terminal app (ADR 0016). Only the two files Enzo runs are kept, each checked again by hash.

The archive is the ONNX export of sentence-transformers/all-MiniLM-L6-v2 (revision 7dbbc90,
Apache-2.0) that Chroma publishes for its own default embedding function; the archive's hash is
the one pinned in chromadb 1.5.9's source (``onnx_mini_lm_l6_v2.py``, read 2026-11-26).
"""

import hashlib
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx2

from enzo.paths import private_dir

#: Points at a mirror of the archive (or a local copy). Safe: the pinned hash must still match.
URL_OVERRIDE = "ENZO_EMBEDDING_MODEL_URL"


class ModelError(Exception):
    """Why the model cannot be fetched or used, in plain words (logged; shown by `enzo doctor`)."""


@dataclass(frozen=True, slots=True)
class Tuning:
    """Similarity thresholds for one model: cosine similarity between a question and a chunk.

    Measured, not guessed: each model has its own spread (``tests/semantic/meaning.json`` holds
    the questions and notes they were read from)."""

    #: A memory below this is not about the message: it is not recalled with it.
    memory_floor: float
    #: A past chat's turn below this is not recalled with a message (whole turns score lower
    #: than short notes, and an unrelated excerpt costs more tokens than a memory).
    chat_floor: float
    #: The brain's own searches (it asked, and it checks what it gets): a lower bar.
    search_floor: float
    #: Recall keeps only hits this close to the best one: a clearly weaker neighbour is a
    #: similar-but-different topic (spec §6.3 "does not confuse them").
    margin: float


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    #: Its path inside the archive.
    member: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    revision: str
    url: str
    sha256: str
    size: int
    files: tuple[ModelFile, ...]
    dims: int
    #: The longest input the model reads, in its own tokens (the rest of a chunk is cut).
    max_tokens: int
    licence: str
    tuning: Tuning

    @property
    def id(self) -> str:
        """Stored with every vector: a different model (or revision) means embedding again."""
        return f"{self.name}@{self.revision}"


MINILM = ModelSpec(
    name="all-MiniLM-L6-v2",
    revision="7dbbc90",
    url="https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz",
    sha256="913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3",
    size=83_178_821,
    files=(
        ModelFile(
            "model.onnx",
            "onnx/model.onnx",
            "4f148ba8ae9c2c7fbee4af2b132db8d06c6a6545b47fc83bbb98c3d22b8393e6",
            90_387_606,
        ),
        ModelFile(
            "tokenizer.json",
            "onnx/tokenizer.json",
            "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
            711_661,
        ),
    ),
    dims=384,
    max_tokens=256,
    licence="Apache-2.0",
    # Read 2026-11-26 on tests/semantic/meaning.json: rephrased questions scored 0.30-0.71 with
    # their note (median 0.42), unrelated ones at most 0.34 (median 0.17); a question to the
    # whole turn it is about 0.38-0.48, unrelated ones at most 0.30. Confusable neighbours sat
    # 0.19-0.59 below the right note.
    tuning=Tuning(memory_floor=0.30, chat_floor=0.35, search_floor=0.20, margin=0.15),
)


def folder_for(models: Path, spec: ModelSpec) -> Path:
    return models / spec.name


def installed(models: Path, spec: ModelSpec) -> bool:
    """All of the model's files are there, at their pinned sizes (hashes are checked on load)."""
    folder = folder_for(models, spec)
    return all(
        (folder / f.name).is_file() and (folder / f.name).stat().st_size == f.size
        for f in spec.files
    )


def verify(models: Path, spec: ModelSpec) -> None:
    """Every file is there and has its pinned hash; else ``ModelError``."""
    folder = folder_for(models, spec)
    for f in spec.files:
        path = folder / f.name
        if not path.is_file() or sha256(path) != f.sha256:
            msg = f"{f.name} is missing or changed: Enzo downloads the model again"
            raise ModelError(msg)


def fetch(models: Path, spec: ModelSpec, http: httpx2.Client) -> None:
    """Download the archive, check it, and keep the model's files (each checked again).

    Blocking: run it in a thread. Nothing is kept from a download that does not match.
    """
    folder = private_dir(folder_for(private_dir(models), spec))
    url = os.environ.get(URL_OVERRIDE) or spec.url
    fd, archive = tempfile.mkstemp(dir=folder, prefix=".download-", suffix=".tar.gz")
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, http.stream("GET", url, follow_redirects=True) as reply:
            if reply.status_code != httpx2.codes.OK:
                msg = f"the download failed ({reply.status_code} from {_host(url)})"
                raise ModelError(msg)
            for block in reply.iter_bytes():
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != spec.sha256:
            msg = "the downloaded model does not match the pinned one, so it was not used"
            raise ModelError(msg)
        with tarfile.open(archive, mode="r:gz") as tar:
            for f in spec.files:
                _extract(tar, f, folder)
    finally:
        Path(archive).unlink(missing_ok=True)


def _extract(tar: tarfile.TarFile, f: ModelFile, folder: Path) -> None:
    """One named file from the archive (never a path the archive chose), written whole or not."""
    try:
        source = tar.extractfile(f.member)
    except KeyError:
        source = None
    if source is None:
        msg = f"the model archive has no {f.member}"
        raise ModelError(msg)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=f".{f.name}-")
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != f.sha256:
            msg = f"{f.name} in the model archive does not match the pinned one"
            raise ModelError(msg)
        os.replace(temp, folder / f.name)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _host(url: str) -> str:
    return url.split("/")[2] if "//" in url else url
