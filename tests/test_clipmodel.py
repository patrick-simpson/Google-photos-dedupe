"""Tests for gpclean.clipmodel.

Only :class:`~gpclean.clipmodel.StubEmbedder` and :func:`~gpclean.clipmodel.fetch_model` are
exercised by default (no network, no torch). The one real-model test is marked ``clip`` and
skips itself unless ``GPCLEAN_TEST_CLIP=1`` is set, per docs/INTERFACES.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from gpclean import clipmodel
from gpclean.clipmodel import MODELS, ModelSpec, StubEmbedder, fetch_model


def _red_square(size: int = 64) -> Image.Image:
    """A small synthetic "red square" test image."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((size // 4, size // 4, 3 * size // 4, 3 * size // 4), fill=(220, 20, 20))
    return img


def _blue_circle(size: int = 64) -> Image.Image:
    """A small synthetic "blue circle" test image."""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((size // 4, size // 4, 3 * size // 4, 3 * size // 4), fill=(20, 20, 220))
    return img


# --- StubEmbedder -----------------------------------------------------------------------


def test_stub_image_shape_and_norm():
    stub = StubEmbedder(dim=512)
    vecs = stub.embed([_red_square(), _blue_circle()])
    assert vecs.shape == (2, 512)
    assert vecs.dtype == np.float16
    norms = np.linalg.norm(vecs.astype(np.float32), axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-2)  # fp16 rounding, so a loose tolerance


def test_stub_image_empty_batch():
    stub = StubEmbedder(dim=512)
    vecs = stub.embed([])
    assert vecs.shape == (0, 512)
    assert vecs.dtype == np.float16


def test_stub_image_deterministic():
    stub = StubEmbedder(dim=512)
    a = stub.embed([_red_square()])
    b = stub.embed([_red_square()])
    np.testing.assert_array_equal(a, b)


def test_stub_image_distinguishes_content():
    stub = StubEmbedder(dim=512)
    red = stub.embed([_red_square()])[0]
    blue = stub.embed([_blue_circle()])[0]
    assert not np.array_equal(red, blue)


def test_stub_text_shape_norm_and_determinism():
    stub = StubEmbedder(dim=512)
    a = stub.embed("a red square")
    b = stub.embed("a red square")
    c = stub.embed("a blue circle")
    assert a.shape == (512,)
    assert a.dtype == np.float32
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)


def test_stub_dim_is_configurable():
    stub = StubEmbedder(dim=8)
    assert stub.embed("q").shape == (8,)
    assert stub.embed([_red_square()]).shape == (1, 8)


def test_get_image_embedder_none():
    assert clipmodel.get_image_embedder("none") is None


# --- module hygiene / pins -----------------------------------------------------------------


def test_import_is_torch_free():
    """Importing gpclean.clipmodel must not pull in torch/open_clip: they're only imported
    lazily inside ImageEmbedder/TextEmbedder, so every other command and test that imports
    (or transitively imports) this module stays fast and doesn't need them installed."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, gpclean.clipmodel; "
            "assert 'torch' not in sys.modules and 'open_clip' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_models_are_pinned_to_safetensors_with_valid_hex():
    """Every entry in MODELS must point at a .safetensors file (never a pickle-format .bin),
    with a full 40-hex-char commit sha and a full 64-hex-char sha256 -- guards against a pin
    silently regressing to an unverifiable or pickle-loading format."""
    for name, spec in MODELS.items():
        assert spec.filename.endswith(".safetensors"), f"{name}: not a safetensors file"
        assert re.fullmatch(r"[0-9a-f]{40}", spec.revision), f"{name}: bad revision hex"
        assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256), f"{name}: bad sha256 hex"
        assert spec.dim == 512, f"{name}: dim must match schema.py's 512-float16 embedding"


# --- HF_HOME handling ------------------------------------------------------------------


def test_hf_home_empty_falls_back_to_default(monkeypatch):
    """An HF_HOME set to the empty string (e.g. an unresolved CI env-block substitution) must
    fall back to the default cache root, not resolve to the current directory."""
    monkeypatch.setenv("HF_HOME", "")
    spec = _fake_spec("0" * 64)
    path = clipmodel._cache_path(spec)
    assert path.is_relative_to(Path.home() / ".cache" / "huggingface")


# --- fetch_model(download=False) --------------------------------------------------------


def test_fetch_model_download_false_raises_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setitem(MODELS, "b32", _fake_spec("0" * 64))

    def fail_download(url, dest, *, timeout=60.0):
        raise AssertionError("download=False must never call _download")

    monkeypatch.setattr(clipmodel, "_download", fail_download)

    with pytest.raises(FileNotFoundError, match="fetch-model"):
        fetch_model("b32", download=False)


# --- cli_fetch_model ---------------------------------------------------------------------


def test_cli_fetch_model_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"pretend-clip-weights-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(
        clipmodel, "_download", lambda url, dest, *, timeout=60.0: Path(dest).write_bytes(payload)
    )

    rc = clipmodel.cli_fetch_model("b32")

    assert rc == 0
    assert "ok" in capsys.readouterr().out


def test_cli_fetch_model_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setitem(MODELS, "b32", _fake_spec("0" * 64))  # mismatched payload below
    monkeypatch.setattr(
        clipmodel,
        "_download",
        lambda url, dest, *, timeout=60.0: Path(dest).write_bytes(b"not what we pinned"),
    )

    rc = clipmodel.cli_fetch_model("b32")

    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


# --- fetch_model -------------------------------------------------------------------------


def _fake_spec(sha256: str) -> ModelSpec:
    """A ModelSpec that never resolves to a real URL; ``_download`` is monkeypatched in every
    test that uses it, so the (fake) hf_repo/revision below are never actually requested."""
    return ModelSpec(
        open_clip_name="ViT-B-32",
        pretrained_tag="datacomp_xl_s13b_b90k",
        hf_repo="example/fake-repo",
        revision="deadbeef",
        filename="weights.safetensors",
        sha256=sha256,
        dim=512,
    )


def test_fetch_model_downloads_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"pretend-clip-weights-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))

    calls = []

    def fake_download(url, dest, *, timeout=60.0):
        calls.append(url)
        Path(dest).write_bytes(payload)

    monkeypatch.setattr(clipmodel, "_download", fake_download)

    path = fetch_model("b32")

    assert len(calls) == 1
    assert path.read_bytes() == payload
    assert path.exists()
    # No leftover partial-download file next to it.
    assert list(path.parent.glob("*.part-*")) == []


def test_fetch_model_skips_download_when_already_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"already-here"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))

    dest = clipmodel._cache_path(MODELS["b32"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)

    def fail_download(url, dest, *, timeout=60.0):
        raise AssertionError("should not re-download an already-verified file")

    monkeypatch.setattr(clipmodel, "_download", fail_download)

    path = fetch_model("b32")
    assert path == dest
    assert path.read_bytes() == payload


def test_fetch_model_hash_mismatch_raises_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    # Pin an sha256 that the (fake) download will never actually match.
    monkeypatch.setitem(MODELS, "b32", _fake_spec("0" * 64))

    def fake_download(url, dest, *, timeout=60.0):
        Path(dest).write_bytes(b"not what we pinned")

    monkeypatch.setattr(clipmodel, "_download", fake_download)

    with pytest.raises(ValueError, match="sha256 mismatch"):
        fetch_model("b32")

    dest = clipmodel._cache_path(MODELS["b32"])
    assert not dest.exists()
    assert list(dest.parent.glob("*.part-*")) == []  # partial file was cleaned up too


def test_fetch_model_redownloads_after_local_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"the-real-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))

    dest = clipmodel._cache_path(MODELS["b32"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"corrupted leftovers")  # wrong content already on disk

    def fake_download(url, dest, *, timeout=60.0):
        Path(dest).write_bytes(payload)

    monkeypatch.setattr(clipmodel, "_download", fake_download)

    path = fetch_model("b32")
    assert path.read_bytes() == payload


# --- "already verified" sidecar marker -------------------------------------------------------


def _count_hashes(monkeypatch) -> list[Path]:
    """Wrap ``_sha256_of`` so a test can see how many full hashes a call performed."""
    calls: list[Path] = []
    real = clipmodel._sha256_of

    def counting(path):
        calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(clipmodel, "_sha256_of", counting)
    return calls


def _no_download(url, dest, *, timeout=60.0):
    raise AssertionError("should not download")


def _cached_file(payload: bytes) -> Path:
    """Write ``payload`` at the (monkeypatched) b32 cache path, with no marker yet."""
    dest = clipmodel._cache_path(MODELS["b32"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return dest


def test_download_writes_marker_and_is_fully_hashed(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"fresh-download"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(
        clipmodel, "_download", lambda url, dest, *, timeout=60.0: Path(dest).write_bytes(payload)
    )
    hashes = _count_hashes(monkeypatch)

    path = fetch_model("b32")

    assert len(hashes) == 1  # the download itself is always hashed
    marker = json.loads(clipmodel._marker_path(path).read_text(encoding="utf-8"))
    st = path.stat()
    assert marker == {"sha256": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    # No temp files left behind (neither the download's nor the marker's).
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name, f"{path.name}.verified"]


def test_matching_marker_skips_rehash(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"cached-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)
    hashes = _count_hashes(monkeypatch)

    fetch_model("b32")  # no marker yet: full hash, then writes one
    assert len(hashes) == 1
    assert clipmodel._marker_path(dest).exists()

    assert fetch_model("b32") == dest  # marker matches: trusted, no hash
    assert fetch_model("b32", download=False) == dest
    assert len(hashes) == 1


def test_marker_ignored_when_mtime_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"cached-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)
    fetch_model("b32")

    st = dest.stat()
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # "touched"
    hashes = _count_hashes(monkeypatch)

    assert fetch_model("b32") == dest
    assert len(hashes) == 1  # re-hashed because mtime_ns no longer matches
    marker = json.loads(clipmodel._marker_path(dest).read_text(encoding="utf-8"))
    assert marker["mtime_ns"] == dest.stat().st_mtime_ns  # marker refreshed


def test_marker_ignored_when_content_replaced(tmp_path, monkeypatch):
    """Same-size corruption with a new mtime must be caught (re-hash -> mismatch -> refetch),
    and the stale marker must go away with the bad file."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"the-real-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)
    fetch_model("b32")

    st = dest.stat()
    dest.write_bytes(b"X" * len(payload))  # same size, different bytes
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    with pytest.raises(FileNotFoundError):
        fetch_model("b32", download=False)
    assert not dest.exists()
    assert not clipmodel._marker_path(dest).exists()


@pytest.mark.parametrize(
    "marker_text",
    [
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"sha256": "0" * 64, "size": 0, "mtime_ns": 0}),
    ],
    ids=["garbage", "not-a-dict", "wrong-values"],
)
def test_bad_marker_falls_back_to_rehash(tmp_path, monkeypatch, marker_text):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"cached-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)
    clipmodel._marker_path(dest).write_text(marker_text, encoding="utf-8")
    hashes = _count_hashes(monkeypatch)

    assert fetch_model("b32") == dest
    assert len(hashes) == 1


def test_marker_for_other_pin_is_not_trusted(tmp_path, monkeypatch):
    """A marker vouching for a different sha256 (e.g. the pin was bumped) must not be trusted,
    even if size and mtime match the file on disk."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"old-pinned-bytes"
    monkeypatch.setitem(MODELS, "b32", _fake_spec("0" * 64))  # new pin these bytes don't match
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)
    st = dest.stat()
    clipmodel._marker_path(dest).write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError):
        fetch_model("b32", download=False)
    assert not dest.exists()


def test_cli_fetch_model_always_rehashes(tmp_path, monkeypatch, capsys):
    """The explicit ``gpclean fetch-model`` command re-verifies fully, ignoring the marker."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"cached-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    _cached_file(payload)
    fetch_model("b32")  # writes the marker
    hashes = _count_hashes(monkeypatch)

    assert clipmodel.cli_fetch_model("b32") == 0
    assert len(hashes) == 1
    assert "ok" in capsys.readouterr().out


def test_marker_write_failure_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    payload = b"cached-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(MODELS, "b32", _fake_spec(digest))
    monkeypatch.setattr(clipmodel, "_download", _no_download)
    dest = _cached_file(payload)

    def fail_replace(src, dst):
        raise PermissionError("simulated")

    monkeypatch.setattr(clipmodel.os, "replace", fail_replace)

    assert fetch_model("b32") == dest
    assert not clipmodel._marker_path(dest).exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]  # no temp left


# --- real model (network) -----------------------------------------------------------------


@pytest.mark.clip
@pytest.mark.skipif(os.environ.get("GPCLEAN_TEST_CLIP") != "1", reason="set GPCLEAN_TEST_CLIP=1")
def test_clip_b32_pairs_images_with_matching_text():
    """Fetches the real b32 weights (network) and checks basic sanity: the right image/text
    pairing scores higher than the crossed one."""
    from gpclean.clipmodel import ImageEmbedder, TextEmbedder

    image_embedder = ImageEmbedder("b32")
    text_embedder = TextEmbedder("b32")

    images = image_embedder.embed([_red_square(), _blue_circle()]).astype(np.float32)
    red_text = text_embedder.embed("a red square")
    blue_text = text_embedder.embed("a blue circle")

    sim_red_red = float(images[0] @ red_text)
    sim_red_blue = float(images[0] @ blue_text)
    sim_blue_blue = float(images[1] @ blue_text)
    sim_blue_red = float(images[1] @ red_text)

    assert sim_red_red > sim_red_blue
    assert sim_blue_blue > sim_blue_red
