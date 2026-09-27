"""Pinned CLIP models: fetch verified weights and embed images/text.

Two named models are supported, ``b32`` (default, faster) and ``b16`` (slower, slightly
better recall). Both are `open_clip <https://github.com/mlfoundations/open_clip>`_ checkpoints
pretrained on DataComp-XL, published on the Hugging Face Hub. We never let ``open_clip``/
``huggingface_hub`` reach out to the network on our behalf: instead we resolve the exact file
we want ourselves, pin it by *commit sha + sha256 of the file's bytes* in ``MODELS`` below, and
hand ``open_clip`` a plain local path to load from. That means:

- a supply-chain change on the Hub (a repo re-tagged, a file replaced) can never silently swap
  the weights we embed with -- ``fetch_model`` raises instead of trusting an unpinned download;
- once the file is verified, ``HF_HUB_OFFLINE=1`` is set so nothing else in the process can
  touch the network trying to "helpfully" re-resolve the model.

``torch`` and ``open_clip`` are only imported lazily, inside :class:`ImageEmbedder` and
:class:`TextEmbedder`, so importing this module (e.g. to read ``MODELS``, or to use
:class:`StubEmbedder` in tests / ``--no-clip`` paths) stays cheap and torch-free.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    """Everything needed to fetch, verify, and load one pinned CLIP checkpoint."""

    open_clip_name: str   # open_clip architecture name, e.g. "ViT-B-32"
    pretrained_tag: str   # open_clip pretrained tag (what resolved to the HF repo below)
    hf_repo: str           # Hugging Face Hub repo id
    revision: str          # pinned commit sha of that repo
    filename: str          # weights file name inside the repo, at that revision
    sha256: str            # pinned sha256 of the file's bytes
    dim: int               # embedding dimension


# Resolved once (2026-09) with:
#   open_clip.pretrained.get_pretrained_cfg("ViT-B-32", "datacomp_xl_s13b_b90k")["hf_hub"]
#   -> laion/CLIP-ViT-B-32-DataComp.XL-s13B-b90K  (and ViT-B-16 -> the B-16 repo)
# then https://huggingface.co/api/models/<repo> for the current commit `sha`, and
# https://huggingface.co/api/models/<repo>/tree/<sha> for each file's `lfs.oid` (== sha256
# for files stored via Git LFS, which these weight files are). The safetensors file was
# downloaded once and hashed locally to confirm the API's reported sha256 before pinning it
# here.
MODELS: dict[str, ModelSpec] = {
    "b32": ModelSpec(
        open_clip_name="ViT-B-32",
        pretrained_tag="datacomp_xl_s13b_b90k",
        hf_repo="laion/CLIP-ViT-B-32-DataComp.XL-s13B-b90K",
        revision="f0e2ffa09cbadab3db6a261ec1ec56407ce42912",
        filename="open_clip_model.safetensors",
        sha256="3c00043509d2e3f35ec62bd85a643a393883dcda624d8208d034cc390c708297",
        dim=512,
    ),
    "b16": ModelSpec(
        open_clip_name="ViT-B-16",
        pretrained_tag="datacomp_xl_s13b_b90k",
        hf_repo="laion/CLIP-ViT-B-16-DataComp.XL-s13B-b90K",
        # laion's main branch for this repo ships only a .bin (a torch pickle), with no
        # open_clip_model.safetensors. But refs/pr/2 on that repo (opened by the automated
        # SFconvertbot, which converts .bin checkpoints to .safetensors across the Hub) adds
        # open_clip_pytorch_model.safetensors at this commit. The tensors in that file were
        # checked bit-identical (torch.equal, all 302 keys) to main's .bin (revision
        # d110532e8d4ff91c574ee60a342323f28468b287) on 2026-09-27, so pinning this commit
        # keeps the same weights while satisfying "no pickle anywhere" in docs/PLAN.md.
        revision="05fa70d888b3dcddcaeb5906c71e35e736bc8d42",
        filename="open_clip_pytorch_model.safetensors",
        sha256="783dcd825e1a3532faa77a2537bbc041353779a7579d904e547a61d5aa18ac24",
        dim=512,
    ),
}


def _hf_home() -> Path:
    """The Hugging Face cache root, honouring ``HF_HOME`` like the real hub client does.

    Treats an unset *or empty* ``HF_HOME`` the same way (a common CI pattern is
    ``HF_HOME: ${{ env.SOMETHING_UNSET }}``, which sets the variable to ``""`` rather than
    leaving it unset) -- otherwise ``Path("")`` resolves relative to the current directory,
    which could be a repo checkout, and ~600 MB of weights would land there. Also expands
    ``~`` like ``huggingface_hub`` does, since a plain ``Path()`` doesn't.
    """
    raw = os.environ.get("HF_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "huggingface"


def _cache_path(spec: ModelSpec) -> Path:
    """Where a verified weights file for ``spec`` lives on disk.

    This is a small gpclean-owned subdirectory of the HF cache root, not an attempt to
    replicate huggingface_hub's own snapshot layout (which we don't need, since we never let
    that library touch the network itself).
    """
    return _hf_home() / "gpclean" / spec.hf_repo.replace("/", "--") / spec.revision / spec.filename


def _sha256_of(path: Path) -> str:
    """Streaming sha256 of a file's bytes, without loading it all into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _weights_url(spec: ModelSpec) -> str:
    """The exact, revision-pinned download URL for ``spec``'s weights file."""
    return f"https://huggingface.co/{spec.hf_repo}/resolve/{spec.revision}/{spec.filename}"


def _download(url: str, dest: Path, *, timeout: float = 60.0) -> None:
    """Stream ``url`` into ``dest`` using stdlib urllib only (no new dependency).

    Plain ``urllib.request.urlopen`` already does the right thing here without any extra
    wiring: it builds its default opener with a ``ProxyHandler`` that reads ``HTTPS_PROXY``/
    ``HTTP_PROXY`` from the environment, and its default SSL context is
    ``ssl.create_default_context()``, which loads the system CA bundle (and honours
    ``SSL_CERT_FILE`` when set) -- exactly "the system CA bundle, honour HTTPS_PROXY" asked
    for, with no custom transport code to get wrong.

    This is a separate function (not inlined into ``fetch_model``) so tests can monkeypatch
    it to point at a local fixture server, or to simulate a corrupted download, without any
    real network access.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "gpclean/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        for chunk in iter(lambda: resp.read(1024 * 1024), b""):
            out.write(chunk)


def fetch_model(name: str, *, download: bool = True) -> Path:
    """Return a local, sha256-verified path to model ``name``'s weights.

    Skips the download when a verified copy is already cached. On a hash mismatch (a corrupt
    or unexpectedly-changed download), the bad file is deleted and ``ValueError`` is raised --
    callers must not fall back to using unverified weights.

    ``download=False`` (used by :class:`TextEmbedder`, which runs in long-lived, latency-
    sensitive processes like the review site and the MCP server) never touches the network:
    if no verified copy is cached, it raises ``FileNotFoundError`` with a pointer to the
    explicit ``gpclean fetch-model`` command, instead of silently starting a ~600 MB download
    inside what's supposed to be a quick request/tool call.
    """
    spec = MODELS[name]
    dest = _cache_path(spec)

    if dest.exists():
        if _sha256_of(dest) == spec.sha256:
            logger.info("clip weights already cached: %s", name)
            return dest
        logger.warning("cached clip weights failed verification, refetching: %s", name)
        dest.unlink()

    if not download:
        raise FileNotFoundError(
            f"CLIP weights for {name!r} not found; run `gpclean fetch-model --model {name}`"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Download to a per-process temp name in the same directory, then atomically rename into
    # place, so a crash mid-download (or two processes fetching at once) never leaves a
    # partial file at the path other code checks for.
    tmp = dest.with_name(f"{dest.name}.part-{os.getpid()}")
    try:
        _download(_weights_url(spec), tmp)
        digest = _sha256_of(tmp)
        if digest != spec.sha256:
            raise ValueError(
                f"sha256 mismatch fetching clip model {name!r}: "
                f"expected {spec.sha256}, got {digest}"
            )
        try:
            os.replace(tmp, dest)
        except PermissionError:
            # Windows only: another process (e.g. a sibling scan-pool worker, also racing to
            # fetch the same model) may already have this file open/mmapped, which blocks a
            # rename onto it. That's fine as long as *their* copy verifies -- use it instead
            # of failing the whole worker. If it doesn't verify, this really is some other
            # problem (e.g. permissions), so re-raise.
            if not (dest.exists() and _sha256_of(dest) == spec.sha256):
                raise
            logger.info("clip weights already installed by another process: %s", name)
    finally:
        # No-op once os.replace succeeded; cleans up a bad/partial download otherwise.
        tmp.unlink(missing_ok=True)

    logger.info("fetched and verified clip weights: %s", name)
    return dest


def cli_fetch_model(model: str) -> int:
    """``gpclean fetch-model --model {b32,b16}``: fetch + verify, printing a short local status.

    This is a local, interactive command (not library code), so it prints normally per the
    logging rules in docs/INTERFACES.md.
    """
    try:
        path = fetch_model(model)
    except Exception as exc:  # noqa: BLE001 - any failure is reported the same way
        print(f"fetch-model {model}: FAILED ({exc})")
        return 1
    print(f"fetch-model {model}: ok -> {path}")
    return 0


class ImageEmbedder:
    """Embeds PIL images to L2-normalised CLIP vectors, on CPU.

    Loading is offline-only: the weights path handed to ``open_clip`` is the file
    :func:`fetch_model` already verified, and ``HF_HUB_OFFLINE`` is set first so nothing else
    open_clip does while building the model can reach the network.
    """

    def __init__(self, name: str, *, threads: int | None = None) -> None:
        """Load model ``name`` ("b32" or "b16"). ``threads`` sets torch's CPU thread count,
        only when the caller asks for a specific value (leaving other threads free, e.g. for
        the shard worker pool that's also running).
        """
        # Set this *before* importing open_clip/huggingface_hub: huggingface_hub reads
        # HF_HUB_OFFLINE into a module-level constant once, at import time, so setting the
        # env var afterwards would have no effect on this process (only on subprocesses that
        # inherit the environment).
        os.environ["HF_HUB_OFFLINE"] = "1"
        import open_clip  # lazy: keep importing this module cheap when CLIP isn't used
        import torch

        if threads is not None:
            torch.set_num_threads(threads)

        spec = MODELS[name]
        weights_path = fetch_model(name)
        model, _, preprocess = open_clip.create_model_and_transforms(
            spec.open_clip_name, pretrained=str(weights_path)
        )
        model.eval()

        self._torch = torch
        self._model = model
        self._preprocess = preprocess
        self._dim = spec.dim

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        """Embed a batch of PIL images -> ``(n, dim)`` float16, L2-normalised."""
        if not images:
            return np.zeros((0, self._dim), dtype=np.float16)
        torch = self._torch
        batch = torch.stack([self._preprocess(img.convert("RGB")) for img in images])
        with torch.inference_mode():
            feats = self._model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.to(torch.float16).cpu().numpy()


class TextEmbedder:
    """Embeds a text query to an L2-normalised CLIP vector, on CPU.

    Used by the review site and MCP server for CLIP text search; both are long-lived
    processes, so the (comparatively slow) model load happens once at construction and the
    weights come from the same offline, verified path as :class:`ImageEmbedder`.
    """

    def __init__(self, name: str) -> None:
        """Load model ``name`` ("b32" or "b16")."""
        # See the comment in ImageEmbedder.__init__: this must happen before open_clip (and
        # therefore huggingface_hub) is imported to have any effect in this process.
        os.environ["HF_HUB_OFFLINE"] = "1"
        import open_clip
        import torch

        spec = MODELS[name]
        # download=False: TextEmbedder runs in long-lived, latency-sensitive processes (the
        # review site, the MCP server). Per docs/PLAN.md section 6, text search is offline;
        # a missing model should fail fast with guidance, not silently start a long download
        # inside what's supposed to be a quick request/tool call.
        weights_path = fetch_model(name, download=False)
        model, _, _ = open_clip.create_model_and_transforms(
            spec.open_clip_name, pretrained=str(weights_path)
        )
        model.eval()
        tokenizer = open_clip.get_tokenizer(spec.open_clip_name)

        self._torch = torch
        self._model = model
        self._tokenizer = tokenizer
        self._dim = spec.dim

    def embed(self, query: str) -> np.ndarray:
        """Embed ``query`` -> ``(dim,)`` float32, L2-normalised.

        Per docs/PLAN.md section 6: the mean of the embeddings of ``query`` and
        ``"a photo of " + query``, renormalised, so a bare noun phrase matches images about as
        well as it would with the "a photo of" framing CLIP was trained to expect.
        """
        torch = self._torch
        texts = [query, f"a photo of {query}"]
        tokens = self._tokenizer(texts)
        with torch.inference_mode():
            feats = self._model.encode_text(tokens)
            feats = feats / feats.norm(dim=-1, keepdim=True)
            mean = feats.mean(dim=0)
            mean = mean / mean.norm()
        return mean.to(torch.float32).cpu().numpy()


class StubEmbedder:
    """A deterministic, torch-free stand-in for *both* :class:`ImageEmbedder` and
    :class:`TextEmbedder`.

    Used by tests and by ``--no-clip``-like paths that still want *some* embedding to exercise
    downstream code (search ranking, schema shape) without pulling in torch/open_clip or any
    network access. It exposes the same ``embed()`` name both classes use, dispatching on the
    argument's type so one stub can play either role. Vectors are pseudo-random but fully
    reproducible: seeded from a hash of the query text, or of a small downscaled version of the
    image's pixels (so visibly similar images naturally do *not* land on the same vector by
    fluke, but re-embedding the exact same image always gives the exact same vector).
    """

    def __init__(self, dim: int = 512) -> None:
        """``dim`` should match the real model's dimension (512 for both b32 and b16)."""
        self.dim = dim

    def _vector(self, key: bytes) -> np.ndarray:
        """A deterministic unit vector derived from ``key``."""
        # A fresh, independently-seeded Generator per call: reproducible from `key` alone,
        # and never touches (or is affected by) numpy's global random state.
        seed = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self.dim)
        v = v / np.linalg.norm(v)
        return v

    def embed(self, x: str | list[Image.Image]) -> np.ndarray:
        """Embed either a text query (``str`` -> ``(dim,)`` float32) or a batch of PIL images
        (``list[Image]`` -> ``(n, dim)`` float16), matching whichever of
        :class:`TextEmbedder`/:class:`ImageEmbedder` the caller is standing in for.
        """
        if isinstance(x, str):
            return self._vector(x.encode("utf-8")).astype(np.float32)

        images = x
        if not images:
            return np.zeros((0, self.dim), dtype=np.float16)
        vecs = []
        for img in images:
            # Downscale to a tiny fixed size first: cheap, and makes the key depend on the
            # image's actual (visible) content rather than incidental encoding bytes.
            small = img.convert("RGB").resize((8, 8))
            vecs.append(self._vector(small.tobytes()))
        return np.stack(vecs).astype(np.float16)


def get_image_embedder(name: str, *, threads: int | None = None) -> ImageEmbedder | None:
    """Factory for the scan pipeline: ``"none"`` disables CLIP entirely (returns ``None``),
    otherwise loads the named pinned model.
    """
    if name == "none":
        return None
    return ImageEmbedder(name, threads=threads)
