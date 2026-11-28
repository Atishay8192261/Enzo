"""The local model's download (ADR 0030): pinned bytes only, nothing kept from a bad download,
and a computer without ONNX Runtime still works, by words."""

import hashlib
import io
import tarfile
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from enzo import cli
from enzo.semantic import MINILM, LocalEmbedder, ModelError
from enzo.semantic.model import ModelFile, ModelSpec, fetch, installed, verify

FILES = {"onnx/model.onnx": b"weights " * 1000, "onnx/tokenizer.json": b'{"version": "1.0"}'}


def archive(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def spec_for(body: bytes, files: dict[str, bytes] = FILES) -> ModelSpec:
    return replace(
        MINILM,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        files=tuple(
            ModelFile(name.split("/")[-1], name, hashlib.sha256(data).hexdigest(), len(data))
            for name, data in files.items()
        ),
    )


def serving(body: bytes, status: int = 200) -> tuple[httpx2.Client, list[str]]:
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        return httpx2.Response(status, content=body)

    return httpx2.Client(transport=httpx2.MockTransport(handler)), asked


def test_the_model_is_downloaded_checked_and_kept_whole(tmp_path: Path) -> None:
    body = archive(FILES)
    spec = spec_for(body)
    http, asked = serving(body)
    fetch(tmp_path, spec, http)
    assert asked == [spec.url]
    folder = tmp_path / spec.name
    assert sorted(p.name for p in folder.iterdir()) == ["model.onnx", "tokenizer.json"]
    assert (folder / "model.onnx").read_bytes() == FILES["onnx/model.onnx"]
    assert folder.stat().st_mode & 0o777 == 0o700  # the user's alone
    assert installed(tmp_path, spec)
    verify(tmp_path, spec)


def test_a_download_that_does_not_match_the_pin_is_refused_and_nothing_is_kept(
    tmp_path: Path,
) -> None:
    spec = spec_for(archive(FILES))
    tampered = archive({**FILES, "onnx/model.onnx": b"other weights"})
    http, _ = serving(tampered)
    with pytest.raises(ModelError, match="does not match the pinned one"):
        fetch(tmp_path, spec, http)
    assert list((tmp_path / spec.name).iterdir()) == []


def test_a_file_inside_the_archive_is_checked_too(tmp_path: Path) -> None:
    body = archive({**FILES, "onnx/model.onnx": b"other weights"})
    spec = replace(spec_for(body), files=spec_for(archive(FILES)).files)  # archive pin matches
    http, _ = serving(body)
    with pytest.raises(ModelError, match=r"model.onnx in the model archive does not match"):
        fetch(tmp_path, spec, http)
    assert list((tmp_path / spec.name).iterdir()) == []


def test_only_the_named_files_are_taken_from_the_archive(tmp_path: Path) -> None:
    """An archive can name any path ("../../.bashrc"): Enzo never extracts by its names."""
    body = archive({**FILES, "../../escape.txt": b"x", "onnx/extra.bin": b"y"})
    spec = spec_for(body, FILES)
    http, _ = serving(body)
    fetch(tmp_path, spec, http)
    assert not (tmp_path.parent / "escape.txt").exists()
    assert sorted(p.name for p in (tmp_path / spec.name).iterdir()) == [
        "model.onnx",
        "tokenizer.json",
    ]


def test_a_missing_file_in_the_archive_is_a_clear_error(tmp_path: Path) -> None:
    body = archive({"onnx/model.onnx": FILES["onnx/model.onnx"]})
    spec = replace(spec_for(body), files=spec_for(archive(FILES)).files)
    http, _ = serving(body)
    with pytest.raises(ModelError, match=r"has no onnx/tokenizer\.json"):
        fetch(tmp_path, spec, http)


def test_a_failed_download_says_so(tmp_path: Path) -> None:
    http, _ = serving(b"no", status=403)
    with pytest.raises(ModelError, match=r"the download failed \(403 from chroma-onnx-models"):
        fetch(tmp_path, spec_for(archive(FILES)), http)


def test_a_mirror_can_serve_it_but_the_pin_still_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = archive(FILES)
    monkeypatch.setenv("ENZO_EMBEDDING_MODEL_URL", "https://mirror.example/minilm.tar.gz")
    http, asked = serving(body)
    fetch(tmp_path, spec_for(body), http)
    assert asked == ["https://mirror.example/minilm.tar.gz"]


def test_a_changed_model_file_is_noticed(tmp_path: Path) -> None:
    body = archive(FILES)
    spec = spec_for(body)
    fetch(tmp_path, spec, serving(body)[0])
    (tmp_path / spec.name / "model.onnx").write_bytes(b"weights " * 999 + b"changed!")
    with pytest.raises(ModelError, match=r"model\.onnx is missing or changed"):
        verify(tmp_path, spec)


async def test_without_onnx_runtime_the_model_is_unavailable_not_broken(tmp_path: Path) -> None:
    def missing() -> object:
        msg = "No module named 'onnxruntime'"
        raise ImportError(msg)

    embedder = LocalEmbedder(tmp_path, runtime=missing)
    assert await embedder.prepare() is False
    assert embedder.state == "unavailable"
    assert embedder.problem is not None
    assert "onnxruntime" in embedder.problem
    assert not embedder.ready
    assert await embedder.prepare() is False  # not tried again and again


async def test_a_download_that_fails_leaves_it_failed_with_the_reason(tmp_path: Path) -> None:
    http, _ = serving(b"no", status=503)
    embedder = LocalEmbedder(tmp_path, http=http)
    assert await embedder.prepare() is False
    assert embedder.state == "failed"
    assert embedder.problem == "the download failed (503 from chroma-onnx-models.s3.amazonaws.com)"


def test_the_pinned_model_is_the_one_measured() -> None:
    """The pins and the thresholds were read together (ADR 0030): changing one needs the other."""
    assert MINILM.id == "all-MiniLM-L6-v2@7dbbc90"
    assert MINILM.sha256 == "913d7300ceae3b2dbc2c50d1de4baacab4be7b9380491c27fab7418616a16ec3"
    assert MINILM.dims == 384
    assert (MINILM.tuning.memory_floor, MINILM.tuning.chat_floor) == (0.30, 0.35)


def test_doctor_says_whether_search_by_meaning_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENZO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENZO_EMBEDDINGS", "local")
    mark, line = cli._check_search()
    assert (mark, line) == (
        "·",
        "Search by meaning: the model (83 MB) downloads once when Enzo starts; until then Enzo "
        "finds things by their words",
    )
    folder = tmp_path / "models" / MINILM.name
    folder.mkdir(parents=True)
    for f in MINILM.files:
        with (folder / f.name).open("wb") as out:
            out.truncate(f.size)
    mark, line = cli._check_search()
    assert mark == "✓"
    assert line.startswith("Search by meaning: all-MiniLM-L6-v2, on this computer")
    monkeypatch.setenv("ENZO_EMBEDDINGS", "off")
    assert cli._check_search()[1].startswith("Search by meaning: off (ENZO_EMBEDDINGS=off)")
