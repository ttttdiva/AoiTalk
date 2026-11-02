"""Resolve the Hugging Face hub cache without importing application services."""
import os
from pathlib import Path


def resolve_embedding_cache(explicit=None):
    value = explicit
    if value is None:
        for key in ("AOITALK_EMBEDDING_CACHE", "SENTENCE_TRANSFORMERS_HOME",
                    "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
            if os.environ.get(key):
                value = os.environ[key]
                break
        else:
            if os.environ.get("HF_HOME"):
                value = Path(os.environ["HF_HOME"]).expanduser() / "hub"
            else:
                base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
                value = Path(base).expanduser() / "huggingface" / "hub"
    try:
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value).strip():
            raise ValueError("cache directory must be a non-empty path")
        if "\x00" in os.fsdecode(value):
            raise ValueError("cache directory contains a null character")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("cache directory must be an absolute path")
        # A missing directory may be created by the normal download flow.
        for part in (path, *path.parents):
            if part.exists() and not part.is_dir():
                raise ValueError("cache directory or an ancestor is not a directory")
        return str(path)
    except (TypeError, OSError, RuntimeError) as exc:
        raise ValueError("invalid cache directory") from exc
