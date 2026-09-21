"""Safe preparation manager for trusted managed local LLM profiles."""

from __future__ import annotations

import copy
import json
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from huggingface_hub import hf_hub_download, snapshot_download

from src.llm.openai_compatible_local_profiles import (
    FREETOKEN_MINIMUM_VERSION,
    LLAMA_CPP_RUNTIME_DISTRIBUTION_PRISMML,
    LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK,
    freetoken_model_profile,
    llama_cpp_auxiliary_artifacts,
    llama_cpp_model_profile,
    llama_cpp_runtime_distribution,
    managed_local_runtime_for_model,
)
from .local_llm_paths import (
    canonicalize_llama_cpp_model_root_override,
    default_managed_models_root,
    llama_cpp_model_discovery_roots,
    resolve_llama_cpp_model_root,
    resolve_managed_runtime_root,
    validate_managed_child,
)

_MANAGED_RUNTIMES = frozenset({"llama_cpp", "freetoken"})
_LLAMA_CPP_BUILD_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])b(\d{3,})(?!\d)", re.IGNORECASE),
    re.compile(r"\bbuild(?:\s+number)?\s*[:=]?\s*(\d{3,})\b", re.IGNORECASE),
)
_LLAMA_CPP_COMMIT_PATTERN = re.compile(
    r"\bcommit\s*[:=]?\s*([0-9a-f]{7,40})\b", re.IGNORECASE
)
_LLAMA_CPP_RELEASE_TAG_PATTERN = re.compile(r"^b(?P<build>\d+)$", re.IGNORECASE)
_LLAMA_CPP_ASSET_BUILD_PATTERN = re.compile(
    r"^llama-b(?P<build>\d+)-",
    re.IGNORECASE,
)
_LLAMA_CPP_RELEASE_API_URL = (
    "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
)
_LLAMA_CPP_RELEASES_API_URL = (
    "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
)
_LLAMA_CPP_INSTALL_MARKER = ".aoitalk-runtime.json"

# Security-sensitive runtime metadata is resolved only from this in-process
# registry.  A profile can select a logical distribution id, but callers may
# not override repository, release pin, or asset naming through API/UI input.
_LLAMA_CPP_RUNTIME_DISTRIBUTIONS: dict[str, dict[str, Any]] = {
    LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK: {
        "id": LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK,
        "repository": "ggml-org/llama.cpp",
        "release_api": _LLAMA_CPP_RELEASE_API_URL,
        "recent_releases_api": _LLAMA_CPP_RELEASES_API_URL,
        "release_policy": "latest_compatible",
        "release_pin": "",
    },
    LLAMA_CPP_RUNTIME_DISTRIBUTION_PRISMML: {
        "id": LLAMA_CPP_RUNTIME_DISTRIBUTION_PRISMML,
        "repository": "PrismML-Eng/llama.cpp",
        "release_api": (
            "https://api.github.com/repos/PrismML-Eng/llama.cpp/releases/"
            "tags/prism-b10683-d8f26ee"
        ),
        "recent_releases_api": "",
        "release_policy": "pinned",
        "release_pin": "prism-b10683-d8f26ee",
        "required_commit": "d8f26eec76da6d09bb708bcba51ef64b8cd868a3",
        "asset_templates": {
            "windows_cuda": (
                "llama-{tag}-bin-win-cuda-13.3-x64.zip",
                "llama-{tag}-bin-win-cuda-12.4-x64.zip",
            ),
            "windows_vulkan": ("llama-{tag}-bin-win-vulkan-x64.zip",),
            "windows_cpu": ("llama-{tag}-bin-win-cpu-x64.zip",),
            "linux_vulkan": ("llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz",),
            "linux_cpu": ("llama-{tag}-bin-ubuntu-x64.tar.gz",),
        },
        "runtime_asset_templates": {
            "13.3": "cudart-llama-bin-win-cuda-13.3-x64.zip",
            "12.4": "cudart-llama-bin-win-cuda-12.4-x64.zip",
        },
    },
}


def _fetch_llama_cpp_release_json(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "AoiTalk"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid llama.cpp release JSON")
    return payload


def _default_llama_cpp_release_provider() -> dict[str, Any]:
    return _fetch_llama_cpp_release_json(_LLAMA_CPP_RELEASE_API_URL)


def _default_llama_cpp_recent_releases_provider() -> list[dict[str, Any]]:
    request = Request(
        _LLAMA_CPP_RELEASES_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "AoiTalk"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if (
        not isinstance(payload, list)
        or not payload
        or not isinstance(payload[0], dict)
    ):
        raise RuntimeError("invalid llama.cpp releases JSON")
    return payload


def parse_llama_cpp_release_build(tag_name: str | None) -> int | None:
    """Parse an exact llama.cpp release tag (for example ``b10660``).

    GitHub's release API can contain PR or nightly tags that happen to include
    a build-looking substring.  Managed installs only accept the canonical
    ``bNNNNN`` tag shape and never infer a build from arbitrary text.
    """

    value = str(tag_name or "").strip()
    match = _LLAMA_CPP_RELEASE_TAG_PATTERN.fullmatch(value)
    return int(match.group("build")) if match else None


def _default_llama_cpp_asset_downloader(url: str, destination: Path) -> None:
    request = Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "AoiTalk"},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_llama_cpp_build(version_output: str) -> int | None:
    """Return the llama.cpp build number reported by --version."""

    text = str(version_output or "")
    for pattern in _LLAMA_CPP_BUILD_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def validate_llama_cpp_executable(
    executable: str | Path,
    *,
    minimum_build: int | None = None,
    required_commit: str | None = None,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    """Probe llama-server without a shell and enforce build/commit requirements."""

    value = str(executable or "").strip()
    if not value:
        raise RuntimeError("llama-server executable is required")
    try:
        probe_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("llama-server validation timeout must be positive") from exc
    if probe_timeout <= 0:
        raise ValueError("llama-server validation timeout must be positive")
    validate_llama_cpp_cuda_dependencies(value)
    try:
        result = subprocess.run(
            [value, "--version"],
            capture_output=True,
            text=True,
            timeout=probe_timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to execute llama-server --version: {exc}") from exc
    output = "\n".join(
        part.strip()
        for part in (result.stdout or "", result.stderr or "")
        if part.strip()
    )
    if result.returncode != 0 or not output:
        raise RuntimeError("llama-server --version failed")
    build = parse_llama_cpp_build(output)
    required = int(minimum_build or 0)
    if required and build is None:
        raise RuntimeError(
            f"llama-server did not report a build number; b{required} or newer is required"
        )
    if required and build is not None and build < required:
        raise RuntimeError(
            f"llama-server b{build} is too old; b{required} or newer is required"
        )

    commit_match = _LLAMA_CPP_COMMIT_PATTERN.search(output)
    commit = commit_match.group(1).casefold() if commit_match else None
    required_sha = str(required_commit or "").strip().casefold()
    if required_sha:
        if not re.fullmatch(r"[0-9a-f]{7,40}", required_sha):
            raise RuntimeError("invalid required llama.cpp commit")
        if commit is None:
            raise RuntimeError(
                f"llama-server did not report a commit; {required_sha} is required"
            )
        if not (
            required_sha.startswith(commit)
            or commit.startswith(required_sha)
        ):
            raise RuntimeError(
                f"llama-server commit {commit} does not match required commit {required_sha}"
            )
    return {"version": output.splitlines()[0], "build": build, "commit": commit}


def validate_llama_cpp_cuda_dependencies(executable: str | Path) -> None:
    """Fail closed when a Windows CUDA build lacks its runtime DLL package.

    llama.cpp publishes CUDA binaries and CUDA runtime DLLs as separate
    archives.  A missing ``cublas64_*`` beside a CUDA backend otherwise causes
    Windows to show a native error dialog before AoiTalk can report a launch
    failure.  Treat a complete local directory (or a DLL on PATH) as valid and
    reject only an identifiable CUDA backend with an incomplete runtime set.
    """

    if os.name != "nt":
        return
    candidate = Path(str(executable)).expanduser()
    if not candidate.is_file():
        return
    directory = candidate.parent
    backend = directory / "ggml-cuda.dll"
    if not backend.is_file() or backend.is_symlink():
        return

    def available(name: str) -> bool:
        local = directory / name
        return (local.is_file() and not local.is_symlink()) or bool(
            shutil.which(name)
        )

    versions: set[str] = set()
    for path in directory.glob("cublas64_*.dll"):
        if path.is_file() and not path.is_symlink():
            suffix = path.stem.removeprefix("cublas64_")
            if suffix:
                versions.add(suffix)
    # When the primary import is absent, infer the current major from the
    # well-known release families so the error names the missing package.
    if not versions:
        versions = {"13", "12"}

    missing: list[str] = []
    for suffix in sorted(versions):
        required = (
            f"cublas64_{suffix}.dll",
            f"cublasLt64_{suffix}.dll",
            f"cudart64_{suffix}.dll",
        )
        absent = [name for name in required if not available(name)]
        if not absent:
            return
        missing.extend(absent)

    if missing:
        unique = ", ".join(dict.fromkeys(missing))
        raise RuntimeError(
            "llama.cpp CUDA runtime DLLs are missing; install the official "
            f"cudart runtime package beside llama-server ({unique})"
        )


def safe_archive_member_path(name: str) -> PurePosixPath:
    """Validate one archive member name before extraction."""

    value = str(name or "").replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    return path


def validate_zip_archive_member(member: Any) -> PurePosixPath:
    path = safe_archive_member_path(str(member.filename))
    mode = (int(member.external_attr) >> 16) & 0o170000
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise RuntimeError(f"unsafe zip archive member type: {member.filename!r}")
    return path


def validate_tar_archive_member(member: Any) -> PurePosixPath:
    path = safe_archive_member_path(str(member.name))
    if not (member.isfile() or member.isdir()):
        raise RuntimeError(f"unsafe tar archive member type: {member.name!r}")
    return path


def trusted_llama_cpp_release_assets(
    release: dict[str, Any],
    *,
    repository: str,
) -> list[dict[str, Any]]:
    """Return release assets backed by one trusted GitHub repository.

    Asset names are never constructed from a build/version convention. The
    release JSON supplies the literal name and URL; this function verifies
    their relationship and origin only.
    """

    if not isinstance(release, dict):
        return []
    tag = str(release.get("tag_name") or "").strip()
    assets = release.get("assets")
    if (
        not tag
        or "/" in tag
        or "\\" in tag
        or not isinstance(assets, list)
    ):
        return []

    repository_value = str(repository or "").strip().strip("/")
    if (
        not repository_value
        or repository_value.count("/") != 1
        or any(part in {"", ".", ".."} for part in repository_value.split("/"))
        or any(char in repository_value for char in "\\:?*[]")
    ):
        return []

    prefix = f"/{repository_value}/releases/download/{tag}/"
    result: list[dict[str, Any]] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if not name or "/" in name or "\\" in name:
            continue
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            continue
        decoded_path = unquote(parsed.path)
        if not decoded_path.startswith(prefix):
            continue
        if decoded_path[len(prefix) :] != name:
            continue
        result.append(
            {
                "name": name,
                "browser_download_url": url,
                "size": item.get("size"),
                "digest": item.get("digest"),
            }
        )
    return result


def official_llama_cpp_release_assets(
    release: dict[str, Any],
) -> list[dict[str, Any]]:
    """Backward-compatible stock llama.cpp release-asset wrapper."""

    return trusted_llama_cpp_release_assets(
        release,
        repository="ggml-org/llama.cpp",
    )


def select_llama_cpp_release_asset(
    release: dict[str, Any],
    *,
    allowed_names: Iterable[str],
    repository: str = "ggml-org/llama.cpp",
) -> dict[str, Any]:
    """Select one exact caller-allowlisted name from official release JSON."""

    allowlist = {
        str(name).strip()
        for name in allowed_names
        if str(name).strip()
    }
    if not allowlist:
        raise RuntimeError("llama.cpp release asset allowlist is empty")
    matches = [
        asset
        for asset in trusted_llama_cpp_release_assets(
            release,
            repository=repository,
        )
        if asset["name"] in allowlist
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "llama.cpp release JSON did not contain exactly one allowed asset"
        )
    return matches[0]


def verify_release_asset_digest(path: Path, asset: dict[str, Any]) -> None:
    """Verify a GitHub release asset digest when the API provides one."""

    digest = str(asset.get("digest") or "").strip().lower()
    if not digest:
        return
    if not digest.startswith("sha256:"):
        raise RuntimeError("unsupported llama.cpp release asset digest")
    expected = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("invalid llama.cpp release asset digest")

    actual = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual.update(chunk)
    if actual.hexdigest() != expected:
        raise RuntimeError("llama.cpp release asset checksum mismatch")


class ManagedLocalRuntimeManager:
    """Prepare trusted model artifacts without accepting arbitrary URLs/paths."""

    def __init__(
        self,
        config: Any = None,
        *,
        root: str | Path | None = None,
        platform_name: str | None = None,
        machine: str | None = None,
        release_provider: Callable[[], dict[str, Any]] | None = None,
        asset_downloader: Callable[[str, Path], Any] | None = None,
    ) -> None:
        self.config = config
        self._runtime_root_explicit = root is not None
        self.root = self._resolve_runtime_root(root)
        self.platform_name = str(platform_name or sys.platform).strip().lower()
        self.machine = str(machine or platform.machine()).strip().lower()
        self._release_provider = release_provider or _default_llama_cpp_release_provider
        self._release_provider_is_default = release_provider is None
        self._asset_downloader = (
            asset_downloader or _default_llama_cpp_asset_downloader
        )
        self._lock = threading.RLock()
        self._runtime_install_lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._active: dict[tuple[str, str], str] = {}

    def _config_get(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.config, "get", None)
        missing = object()
        if callable(getter):
            try:
                direct = getter(key, missing)
                if direct is not missing:
                    return direct
            except TypeError:
                pass
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def _resolve_runtime_root(
        self,
        explicit_root: str | Path | None,
    ) -> Path:
        return resolve_managed_runtime_root(
            self.config,
            explicit=explicit_root,
            create=False,
        ).path

    @staticmethod
    def _llama_cpp_distribution(
        profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        distribution_id = llama_cpp_runtime_distribution(profile=profile)
        contract = _LLAMA_CPP_RUNTIME_DISTRIBUTIONS.get(distribution_id)
        if contract is None:
            raise RuntimeError(
                f"untrusted llama.cpp runtime distribution: {distribution_id!r}"
            )
        return copy.deepcopy(contract)

    def _llama_cpp_required_commit(
        self,
        profile: dict[str, Any] | None,
    ) -> str | None:
        contract = self._llama_cpp_distribution(profile)
        distribution_commit = str(
            contract.get("required_commit") or ""
        ).strip().casefold()
        profile_commit = str(
            (profile or {}).get("required_llama_cpp_commit") or ""
        ).strip().casefold()
        for value in (distribution_commit, profile_commit):
            if value and not re.fullmatch(r"[0-9a-f]{7,40}", value):
                raise RuntimeError(
                    "trusted llama.cpp runtime has invalid required commit"
                )
        if distribution_commit and profile_commit and not (
            distribution_commit.startswith(profile_commit)
            or profile_commit.startswith(distribution_commit)
        ):
            raise RuntimeError(
                "llama.cpp profile required commit conflicts with runtime distribution"
            )
        return distribution_commit or profile_commit or None

    def _llama_cpp_distribution_root(
        self,
        profile: dict[str, Any] | None,
    ) -> Path:
        contract = self._llama_cpp_distribution(profile)
        candidate = self.root / "runtimes" / "llama_cpp" / str(contract["id"])
        try:
            return validate_managed_child(
                self.root,
                candidate,
                kind="runtime",
                create_parent=False,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def _llama_cpp_current_dir(self, profile: dict[str, Any] | None) -> Path:
        return self._llama_cpp_distribution_root(profile) / "current"

    def _legacy_stock_llama_cpp_current_dir(self) -> Path:
        candidate = self.root / "runtimes" / "llama_cpp" / "current"
        try:
            return validate_managed_child(
                self.root,
                candidate,
                kind="runtime",
                create_parent=False,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def _llama_cpp_current_candidates(
        self,
        profile: dict[str, Any] | None,
    ) -> list[Path]:
        current = self._llama_cpp_current_dir(profile)
        contract = self._llama_cpp_distribution(profile)
        if contract["id"] != LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK:
            return [current]
        legacy = self._legacy_stock_llama_cpp_current_dir()
        return [current, legacy] if current != legacy else [current]

    @staticmethod
    def _validate_model_identifier(model: str) -> str:
        value = str(model or "").strip()
        if not value:
            raise ValueError("model is required")
        lowered = value.casefold()
        if (
            "://" in value
            or lowered.startswith(("file:", "http:", "https:"))
            or value.startswith(("/", "\\", "~", "."))
            or (
                len(value) >= 3
                and value[0].isalpha()
                and value[1] == ":"
                and value[2] in {"\\", "/"}
            )
        ):
            raise ValueError(
                "managed local runtime accepts a trusted model id, not a URL/path"
            )
        return value

    @staticmethod
    def _validate_runtime(runtime: str | None) -> str | None:
        if runtime is None:
            return None
        value = str(runtime).strip().lower()
        if value not in _MANAGED_RUNTIMES:
            raise ValueError(
                "runtime must be one of: llama_cpp, freetoken"
            )
        return value

    def _resolve_profile(
        self,
        model: str,
        runtime: str | None = None,
        *,
        require_trusted: bool,
    ) -> tuple[str | None, dict[str, Any] | None]:
        model_id = self._validate_model_identifier(model)
        requested_runtime = self._validate_runtime(runtime)
        resolved_runtime = managed_local_runtime_for_model(
            self.config,
            model_id,
        )

        if requested_runtime is not None and requested_runtime != resolved_runtime:
            raise ValueError(
                "requested runtime does not match the selected managed profile: "
                f"requested={requested_runtime!r}, resolved={resolved_runtime!r}"
            )

        if resolved_runtime == "freetoken":
            profile = freetoken_model_profile(model_id)
        elif resolved_runtime == "llama_cpp":
            profile = llama_cpp_model_profile(model_id)
        else:
            profile = None

        if require_trusted and not profile:
            raise ValueError(
                f"untrusted managed local model profile: {model_id}"
            )
        return resolved_runtime, profile

    def _model_dir(
        self,
        runtime: str,
        model: str,
        profile: dict[str, Any] | None = None,
    ) -> Path:
        digest = hashlib.sha256(
            f"{runtime}\0{model}".encode("utf-8")
        ).hexdigest()[:24]
        # The canonical llama.cpp model root is independent from the runtime
        # executable tree.  Keep the profile-specific subdirectory layout so
        # sharded artifacts and MTP companions remain isolated.
        if runtime == "llama_cpp":
            configured_root = self._configured_llama_cpp_model_root()
            profile = profile or llama_cpp_model_profile(model)
            if profile is not None:
                profile_id = str(profile.get("id") or model).strip()
                if profile_id and Path(profile_id).name == profile_id:
                    destination = configured_root / profile_id
                    # ``gguf_repository_subdir`` is the upstream repository
                    # layout.  A profile may optionally provide a distinct
                    # local storage subdirectory (Flash-Next uses ``main``
                    # so its embedded MTP variant can live beside it under
                    # ``mtp`` without mixing the two artifact sets).
                    storage_subdir = str(
                        profile.get("model_storage_subdir")
                        or profile.get("gguf_repository_subdir")
                        or ""
                    ).strip()
                    if storage_subdir:
                        try:
                            relative = safe_archive_member_path(storage_subdir)
                        except RuntimeError:
                            relative = None
                        if relative is not None:
                            destination = destination.joinpath(*relative.parts)
                    return validate_managed_child(
                        configured_root,
                        destination,
                        kind="model",
                    )
        # FreeToken's default model tree is a sibling of the runtime tree.
        # Explicit manager roots remain isolated under that root for backwards
        # compatibility with callers/tests that inject a temporary root.
        model_base = (
            self.root / "models"
            if self._runtime_root_explicit
            else default_managed_models_root()
        )
        return validate_managed_child(
            model_base,
            model_base / runtime / digest,
            kind="model",
        )

    def _configured_llama_cpp_model_root(self) -> Path:
        """Return the one canonical writable GGUF root."""
        if self._runtime_root_explicit:
            raw = self._config_get(
                "openai_compatible_local.llama_cpp.model_root",
                "",
            )
            env_override = os.getenv("LLAMA_CPP_MODEL_ROOT") or os.getenv(
                "AOITALK_LLAMA_CPP_MODEL_ROOT"
            )
            if not str(raw or "").strip() and not str(env_override or "").strip():
                # A caller-supplied manager root is itself an explicit test or
                # embedding override. Keep that instance self-contained while
                # the normal application default remains repository-local.
                return (
                    canonicalize_llama_cpp_model_root_override(
                        self.root / "models" / "llama_cpp",
                        create=False,
                    )
                    or self.root / "models" / "llama_cpp"
                )
        try:
            return resolve_llama_cpp_model_root(self.config, create=False).path
        except ValueError:
            # A stale legacy PATH-like setting must not make status unusable;
            # managed downloads remain on the isolated instance fallback.  API
            # writes use the stricter canonicalizer and reject the value.
            return (
                canonicalize_llama_cpp_model_root_override(
                    self.root / "models" / "llama_cpp",
                    create=False,
                )
                or self.root / "models" / "llama_cpp"
            )

    @staticmethod
    def _regular_non_symlink_file(path: Path) -> bool:
        try:
            return path.is_file() and not path.is_symlink()
        except OSError:
            return False

    def _llama_cpp_discovery_roots(self) -> list[Path]:
        return llama_cpp_model_discovery_roots(self.config)

    @staticmethod
    def _llama_cpp_mtp_companion_contract(
        profile: dict[str, Any],
    ) -> tuple[list[str], bool]:
        metadata = profile.get("mtp")
        if not isinstance(metadata, dict):
            return [], False
        declared = metadata.get("companion_filenames")
        values = list(declared) if isinstance(declared, (list, tuple)) else []
        singular = metadata.get("artifact_filename") or metadata.get(
            "companion_filename"
        )
        if singular:
            values.append(singular)
        filenames: list[str] = []
        for item in values:
            filename = str(item or "").strip()
            if (
                not filename
                or filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
                or Path(filename).is_absolute()
                or Path(filename).name != filename
            ):
                raise RuntimeError(
                    "trusted llama.cpp profile has unsafe MTP companion filename"
                )
            if filename not in filenames:
                filenames.append(filename)
        return filenames, bool(
            metadata.get("artifact_required") or metadata.get("required")
        )

    @staticmethod
    def _llama_cpp_mtp_download_source(
        profile: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Return the trusted source override for an MTP companion artifact."""

        metadata = profile.get("mtp")
        if not isinstance(metadata, dict):
            return "", "", ""
        repository = str(metadata.get("artifact_repository") or "").strip()
        if repository and not re.fullmatch(
            r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository
        ):
            raise RuntimeError(
                "trusted llama.cpp profile has unsafe MTP artifact repository"
            )
        revision = str(metadata.get("artifact_revision") or "").strip()
        if revision and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", revision):
            raise RuntimeError(
                "trusted llama.cpp profile has unsafe MTP artifact revision"
            )
        repository_subdir = str(
            metadata.get("artifact_repository_subdir") or ""
        ).strip()
        if repository_subdir:
            parts = repository_subdir.split("/")
            if (
                "\\" in repository_subdir
                or repository_subdir.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or (len(parts[0]) >= 2 and parts[0][1] == ":")
            ):
                raise RuntimeError(
                    "trusted llama.cpp profile has unsafe MTP artifact subdirectory"
                )
        return repository, revision, repository_subdir

    @staticmethod
    def _llama_cpp_auxiliary_contract(
        profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            return llama_cpp_auxiliary_artifacts(profile=profile)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _verify_hf_artifact(
        path: Path,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("managed Hugging Face artifact is not a regular file")
        if expected_size is not None and path.stat().st_size != expected_size:
            raise RuntimeError(
                f"managed Hugging Face artifact size mismatch: {path.name}"
            )
        if expected_sha256:
            digest = str(expected_sha256).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RuntimeError("managed Hugging Face artifact SHA-256 is invalid")
            actual = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
            if actual.hexdigest() != digest:
                raise RuntimeError(
                    f"managed Hugging Face artifact checksum mismatch: {path.name}"
                )

    def _discover_existing_llama_cpp_artifact(
        self,
        profile: dict[str, Any],
    ) -> Path | None:
        primary, filenames, repository_subdir = self._llama_cpp_artifact_contract(
            profile
        )
        companions, companions_required = self._llama_cpp_mtp_companion_contract(
            profile
        )
        auxiliaries = self._llama_cpp_auxiliary_contract(profile)
        required_auxiliaries = [
            artifact for artifact in auxiliaries if artifact.get("required")
        ]
        seen: set[str] = set()
        for root in self._llama_cpp_discovery_roots():
            try:
                if not root.is_dir():
                    continue
                candidates = (
                    [root.joinpath(*repository_subdir.split("/")) / primary]
                    if repository_subdir
                    else []
                )
                candidates.extend([root / primary, *root.rglob(primary)])
            except OSError:
                continue
            for candidate in candidates:
                key = os.path.normcase(os.path.normpath(str(candidate)))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    safe_candidate = validate_managed_child(
                        root,
                        candidate,
                        kind="model",
                    )
                except ValueError:
                    continue
                if not self._regular_non_symlink_file(safe_candidate):
                    continue
                parent = safe_candidate.parent
                try:
                    safe_shard_paths = [
                        validate_managed_child(
                            root,
                            parent / filename,
                            kind="model",
                        )
                        for filename in filenames
                    ]
                except ValueError:
                    continue
                if not all(
                    self._regular_non_symlink_file(path)
                    for path in safe_shard_paths
                ):
                    continue
                companion_paths = [parent / filename for filename in companions]
                try:
                    safe_companion_paths = [
                        validate_managed_child(root, path, kind="model")
                        for path in companion_paths
                    ]
                except ValueError:
                    continue
                if companions_required and not all(
                    self._regular_non_symlink_file(path)
                    for path in safe_companion_paths
                ):
                    continue
                if any(
                    path.is_symlink()
                    or (
                        path.exists()
                        and not self._regular_non_symlink_file(path)
                    )
                    for path in safe_companion_paths
                ):
                    continue
                auxiliary_paths: list[Path] = []
                try:
                    for artifact in required_auxiliaries:
                        auxiliary_path = validate_managed_child(
                            root,
                            parent / str(artifact["filename"]),
                            kind="model",
                        )
                        if not self._regular_non_symlink_file(auxiliary_path):
                            raise ValueError("required auxiliary artifact is missing")
                        self._verify_hf_artifact(
                            auxiliary_path,
                            expected_size=artifact.get("size_bytes"),
                            expected_sha256=artifact.get("sha256"),
                        )
                        auxiliary_paths.append(auxiliary_path)
                except (OSError, RuntimeError, ValueError):
                    continue
                try:
                    for artifact in required_auxiliaries:
                        self._verify_hf_artifact(
                            parent / str(artifact["filename"]),
                            expected_size=artifact.get("size_bytes"),
                            expected_sha256=artifact.get("sha256"),
                        )
                    return safe_candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
        return None

    @staticmethod
    def _llama_cpp_artifact_contract(
        profile: dict[str, Any],
    ) -> tuple[str, list[str], str]:
        primary_value = profile.get("gguf_filename")
        if not isinstance(primary_value, str):
            raise RuntimeError(
                "trusted llama.cpp profile has no safe GGUF filename"
            )
        primary = primary_value.strip()
        if (
            not primary
            or primary in {".", ".."}
            or "/" in primary
            or "\\" in primary
            or Path(primary).is_absolute()
            or Path(primary).name != primary
        ):
            raise RuntimeError(
                "trusted llama.cpp profile has no safe GGUF filename"
            )

        declared = profile.get("gguf_filenames")
        if declared is None:
            filenames = [primary]
        else:
            if not isinstance(declared, (list, tuple)) or not declared:
                raise RuntimeError(
                    "trusted llama.cpp profile has malformed GGUF shard metadata"
                )
            filenames = []
            seen: set[str] = set()
            for item in declared:
                if not isinstance(item, str):
                    raise RuntimeError(
                        "trusted llama.cpp profile has malformed GGUF shard metadata"
                    )
                filename = item.strip()
                if (
                    not filename
                    or filename in {".", ".."}
                    or "/" in filename
                    or "\\" in filename
                    or Path(filename).is_absolute()
                    or Path(filename).name != filename
                ):
                    raise RuntimeError(
                        "trusted llama.cpp profile has unsafe GGUF shard filename"
                    )
                duplicate_key = filename.casefold()
                if duplicate_key in seen:
                    raise RuntimeError(
                        "trusted llama.cpp profile has duplicate GGUF shard filename"
                    )
                seen.add(duplicate_key)
                filenames.append(filename)
            if filenames[0] != primary:
                raise RuntimeError(
                    "trusted llama.cpp profile primary GGUF does not match shard metadata"
                )

        subdir_value = profile.get("gguf_repository_subdir")
        if subdir_value is None:
            subdir = ""
        elif not isinstance(subdir_value, str):
            raise RuntimeError(
                "trusted llama.cpp profile has unsafe repository subdir"
            )
        else:
            subdir = subdir_value.strip()
        if subdir:
            parts = subdir.split("/")
            if (
                "\\" in subdir
                or subdir.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or (len(parts[0]) >= 2 and parts[0][1] == ":")
            ):
                raise RuntimeError(
                    "trusted llama.cpp profile has unsafe repository subdir"
                )
        return primary, filenames, subdir

    @staticmethod
    def _model_installed(
        runtime: str,
        profile: dict[str, Any],
        destination: Path,
    ) -> bool:
        try:
            if runtime == "freetoken":
                if not destination.is_dir() or destination.is_symlink():
                    return False
                return any(
                    path.is_file()
                    for path in destination.rglob("*.safetensors")
                )
            primary, filenames, _ = (
                ManagedLocalRuntimeManager._llama_cpp_artifact_contract(
                    profile
                )
            )
            if destination.is_file():
                if destination.is_symlink() or destination.name != primary:
                    return False
                directory = destination.parent
            elif destination.is_dir() and not destination.is_symlink():
                directory = destination
            else:
                return False
            companions, companions_required = (
                ManagedLocalRuntimeManager._llama_cpp_mtp_companion_contract(
                    profile
                )
            )
            auxiliaries = ManagedLocalRuntimeManager._llama_cpp_auxiliary_contract(
                profile
            )
            required_auxiliaries = [
                artifact for artifact in auxiliaries if artifact.get("required")
            ]
            required_files = [
                *filenames,
                *(companions if companions_required else []),
                *(str(artifact["filename"]) for artifact in required_auxiliaries),
            ]
            if not all(
                ManagedLocalRuntimeManager._regular_non_symlink_file(
                    directory / filename
                )
                for filename in required_files
            ):
                return False
            primary_size = profile.get("gguf_size_bytes")
            primary_sha256 = profile.get("gguf_sha256")
            ManagedLocalRuntimeManager._verify_hf_artifact(
                directory / primary,
                expected_size=primary_size,
                expected_sha256=primary_sha256,
            )
            for artifact in required_auxiliaries:
                ManagedLocalRuntimeManager._verify_hf_artifact(
                    directory / str(artifact["filename"]),
                    expected_size=artifact.get("size_bytes"),
                    expected_sha256=artifact.get("sha256"),
                )
            return True
        except (OSError, RuntimeError):
            return False

    def _freetoken_executable(self) -> str:
        # Never call on Windows. No public headless Windows installation
        # contract is assumed and Desktop/private paths are not inspected.
        configured = str(
            self._config_get(
                "openai_compatible_local.freetoken.executable",
                "ft",
            )
            or "ft"
        ).strip()
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        return str(shutil.which(configured) or "")

    def _managed_llama_executable_info(
        self,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Return the executable from this manager's trusted current install.

        Managed runtimes are deliberately kept separate from PATH and legacy
        environment discovery.  The current tree is an install-owned path,
        and marker-provided relative paths are accepted only when they remain
        below that tree after resolution.
        """

        executable_name = (
            "llama-server.exe"
            if self.platform_name.startswith("win")
            else "llama-server"
        )
        # A managed ``current`` symlink would make the trust boundary depend
        # on mutable external state.  Installs produced by this manager use a
        # real directory and atomically replace it.  Stock also checks the
        # legacy single-current tree for backward compatibility.
        try:
            current_candidates = self._llama_cpp_current_candidates(profile)
        except RuntimeError:
            return {"executable": "", "source": ""}
        for managed_root in current_candidates:
            if managed_root.is_symlink() or not managed_root.is_dir():
                continue

            # The marker is the install-ownership boundary.  A llama-server
            # that happens to be present under ``current`` is not trusted
            # until the marker matches this distribution's contract.
            metadata = self._managed_llama_metadata(
                profile,
                current_dir=managed_root,
            )
            if metadata is None:
                continue

            managed = managed_root / executable_name
            if managed.is_file() and not managed.is_symlink():
                return {"executable": str(managed), "source": "managed"}

            relative_value = str(metadata.get("executable_relative") or "").strip()
            if relative_value:
                try:
                    relative = safe_archive_member_path(relative_value)
                    candidate = managed_root.joinpath(*relative.parts)
                    candidate.resolve(strict=False).relative_to(
                        managed_root.resolve(strict=False)
                    )
                except (OSError, RuntimeError, ValueError):
                    candidate = None
                if (
                    candidate is not None
                    and candidate.is_file()
                    and not candidate.is_symlink()
                ):
                    return {"executable": str(candidate), "source": "managed"}
        return {"executable": "", "source": ""}

    def installed_managed_llama_cpp_executable(
        self,
        profile: dict[str, Any] | None = None,
    ) -> str:
        """Resolve the trusted installed binary without executing a probe.

        Catalog/session resolution shares this with the launch path. Build and
        capability validation remain mandatory immediately before launch.
        """
        return self._managed_llama_executable_info(profile)["executable"]

    def compatible_managed_llama_cpp_executable(
        self,
        profile: dict[str, Any],
    ) -> str:
        """Return a managed llama-server only when it satisfies ``profile``."""

        if not isinstance(profile, dict):
            return ""
        info = self._managed_llama_executable_info(profile)
        executable = str(info.get("executable") or "").strip()
        if not executable or info.get("source") != "managed":
            return ""
        try:
            minimum = self._llama_cpp_profile_minimum_build(profile)
            required_commit = self._llama_cpp_required_commit(profile)
            validate_llama_cpp_executable(
                executable,
                minimum_build=minimum,
                required_commit=required_commit,
            )
        except (RuntimeError, TypeError, ValueError):
            return ""
        return executable

    def _llama_executable_info(
        self,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        configured = str(
            self._config_get(
                "openai_compatible_local.llama_cpp.executable",
                "",
            )
            or ""
        ).strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file():
                return {"executable": str(candidate), "source": "configured"}
            located = shutil.which(configured)
            if located:
                return {"executable": located, "source": "configured"}

        managed_info = self._managed_llama_executable_info(profile)
        if managed_info["executable"]:
            return managed_info

        executable_name = (
            "llama-server.exe"
            if self.platform_name.startswith("win")
            else "llama-server"
        )

        located = shutil.which(executable_name)
        if located:
            return {"executable": str(located), "source": "path"}

        for legacy in (
            os.getenv("LLAMA_CPP_EXECUTABLE"),
            os.getenv("LLAMA_SERVER_EXE"),
        ):
            value = str(legacy or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if candidate.is_file():
                return {"executable": str(candidate), "source": "legacy"}
            located = shutil.which(value)
            if located:
                return {"executable": str(located), "source": "legacy"}
        return {"executable": "", "source": ""}

    def _llama_executable(self, profile: dict[str, Any] | None = None) -> str:
        return self._llama_executable_info(profile)["executable"]

    def _managed_llama_metadata(
        self,
        profile: dict[str, Any] | None = None,
        *,
        current_dir: Path | None = None,
    ) -> dict[str, Any] | None:
        try:
            contract = self._llama_cpp_distribution(profile)
            marker_root = current_dir or self._llama_cpp_current_candidates(profile)[0]
        except RuntimeError:
            return None
        marker = marker_root / _LLAMA_CPP_INSTALL_MARKER
        try:
            # Marker files are part of the managed-runtime trust boundary.
            # Never follow a marker symlink when deciding whether a runtime is
            # manager-owned; a missing marker is likewise not a valid managed
            # install even if a llama-server happens to be present beside it.
            if not marker.is_file() or marker.is_symlink():
                return None
        except OSError:
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        marker_distribution = str(payload.get("distribution") or "").strip().lower()
        marker_repository = str(payload.get("repository") or "").strip()
        contract_id = str(contract["id"])
        contract_repository = str(contract["repository"])
        try:
            legacy_stock_current = self._legacy_stock_llama_cpp_current_dir()
            is_legacy_stock_marker = (
                contract_id == LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK
                and marker_root.resolve(strict=False)
                == legacy_stock_current.resolve(strict=False)
            )
        except (OSError, RuntimeError, ValueError):
            return None
        # A legacy marker without distribution/repository is accepted only for
        # the old single-current fallback tree. New stock/current and PrismML
        # installs must carry the explicit distribution contract.
        if marker_distribution:
            if marker_distribution != contract_id:
                return None
        elif not is_legacy_stock_marker:
            return None
        if marker_repository:
            if marker_repository != contract_repository:
                return None
        elif not is_legacy_stock_marker:
            return None
        release_pin = str(contract.get("release_pin") or "").strip()
        if release_pin and str(payload.get("release") or "").strip() != release_pin:
            return None
        required_commit = self._llama_cpp_required_commit(profile)
        if required_commit:
            marker_commit = str(payload.get("commit") or "").strip().casefold()
            if not re.fullmatch(r"[0-9a-f]{7,40}", marker_commit):
                return None
            if not (
                marker_commit.startswith(required_commit)
                or required_commit.startswith(marker_commit)
            ):
                return None
        return payload

    @staticmethod
    def _windows_ai_runtimes_anchor_from_executable(
        configured_executable: str | Path,
    ) -> Path | None:
        """Return the exact ``<drive>:/AI/runtimes`` legacy anchor.

        Legacy recovery is a Windows-only path contract.  Parse the persisted
        value without touching the filesystem so a stale or missing executable
        can still identify its drive, while relative, UNC, nested, and
        traversal paths fail closed.
        """

        value = str(configured_executable or "").strip()
        if not value:
            return None
        raw_parts = value.replace("/", "\\").split("\\")
        if any(part in {".", ".."} for part in raw_parts):
            return None
        try:
            candidate = PureWindowsPath(value)
        except (TypeError, ValueError):
            return None
        drive = str(candidate.drive or "")
        if (
            not candidate.is_absolute()
            or len(drive) != 2
            or not drive[0].isalpha()
            or drive[1] != ":"
            or len(candidate.parts) < 4
            or str(candidate.parts[1]).casefold() != "ai"
            or str(candidate.parts[2]).casefold() != "runtimes"
        ):
            return None
        return Path(
            str(
                PureWindowsPath(candidate.anchor)
                / "AI"
                / "runtimes"
            )
        )

    def compatible_marker_backed_legacy_llama_cpp_executable(
        self,
        configured_executable: str | Path,
        profile: dict[str, Any],
    ) -> str:
        """Recover one compatible executable from an older Windows install.

        The persisted executable is used only to derive the fixed
        ``<drive>:/AI/runtimes`` anchor.  We then inspect non-symlink roots
        named ``aoitalk-managed-*`` and require a valid current-runtime marker
        before asking a fresh ``ManagedLocalRuntimeManager`` to validate the
        candidate.  Recovery is intentionally fail-closed when zero or more
        than one candidates satisfy the profile.
        """

        if not isinstance(profile, dict):
            return ""
        if not self.platform_name.startswith("win"):
            return ""
        anchor = self._windows_ai_runtimes_anchor_from_executable(
            configured_executable
        )
        if anchor is None:
            return ""

        candidates: list[str] = []
        try:
            entries = sorted(
                anchor.iterdir(),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except (OSError, RuntimeError):
            return ""
        for root in entries:
            try:
                if (
                    not root.name.casefold().startswith("aoitalk-managed-")
                    or root.is_symlink()
                    or not root.is_dir()
                ):
                    continue
                current = root / "runtimes" / "llama_cpp" / "current"
                marker = current / _LLAMA_CPP_INSTALL_MARKER
                if (
                    not marker.is_file()
                    or marker.is_symlink()
                ):
                    continue
            except OSError:
                continue

            # Instantiate a manager for each root rather than interpreting
            # marker fields in this method.  This preserves the manager's
            # executable containment and profile validation contract.
            try:
                candidate_manager = ManagedLocalRuntimeManager(
                    self.config,
                    root=root,
                    platform_name=self.platform_name,
                    machine=self.machine,
                    release_provider=self._release_provider,
                    asset_downloader=self._asset_downloader,
                )
                candidate = str(
                    candidate_manager.compatible_managed_llama_cpp_executable(
                        profile
                    )
                    or ""
                ).strip()
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if not candidate:
                continue
            candidates.append(candidate)

        return candidates[0] if len(candidates) == 1 else ""

    def _llama_runtime_details(
        self,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        info = self._llama_executable_info(profile)
        executable = info["executable"]
        if not executable:
            supported = (
                self.machine in {"x86_64", "amd64"}
                and (
                    self.platform_name.startswith("win")
                    or self.platform_name.startswith("linux")
                )
            )
            return {
                "installed": False,
                "path": "",
                "status": "runtime_missing" if supported else "unsupported",
                "error": (
                    "llama-server is not installed or configured."
                    if supported
                    else "managed llama.cpp install supports only Windows/Linux x86_64."
                ),
                "executable": "",
                "install_source": "",
            }

        minimum = None
        required_commit = None
        if profile is not None:
            try:
                minimum = int(profile.get("minimum_llama_cpp_build") or 0) or None
            except (TypeError, ValueError):
                minimum = None
            required_commit = self._llama_cpp_required_commit(profile)
        try:
            version = validate_llama_cpp_executable(
                executable,
                minimum_build=minimum,
                required_commit=required_commit,
            )
        except RuntimeError as exc:
            managed = info["source"] == "managed"
            return {
                "installed": False,
                "path": executable,
                "status": "runtime_missing" if managed else "runtime_invalid",
                "error": str(exc),
                "executable": executable,
                "install_source": info["source"],
            }
        metadata: dict[str, Any] = {}
        if info["source"] == "managed":
            try:
                executable_path = Path(executable).resolve(strict=False)
                for current_dir in self._llama_cpp_current_candidates(profile):
                    try:
                        executable_path.relative_to(current_dir.resolve(strict=False))
                    except ValueError:
                        continue
                    metadata = (
                        self._managed_llama_metadata(
                            profile,
                            current_dir=current_dir,
                        )
                        or {}
                    )
                    break
            except (OSError, RuntimeError, ValueError):
                metadata = {}
        return {
            "installed": True,
            "path": executable,
            "status": None,
            "error": None,
            "executable": executable,
            "install_source": info["source"],
            "version": version.get("version") or "",
            "build": version.get("build"),
            "release": metadata.get("release") or "",
            "asset": metadata.get("asset") or "",
            "distribution": metadata.get("distribution")
            or llama_cpp_runtime_distribution(profile=profile),
        }

    def _runtime_state(
        self,
        runtime: str,
        profile: dict[str, Any] | None = None,
    ) -> tuple[bool, str, str | None, str | None]:
        """Return installed, path, terminal-state, error."""

        if runtime == "freetoken":
            if self.platform_name.startswith("win"):
                return (
                    False,
                    "",
                    "installer_required",
                    (
                        "FreeToken headless CLI has no confirmed public Windows "
                        "programmatic-install contract."
                    ),
                )
            if not self.platform_name.startswith("linux"):
                return (
                    False,
                    "",
                    "unsupported",
                    "FreeToken 0.1.2 managed runtime is supported here only on Linux x86_64.",
                )
            if self.machine not in {"x86_64", "amd64"}:
                return (
                    False,
                    "",
                    "unsupported",
                    "FreeToken 0.1.2 managed runtime requires Linux x86_64.",
                )
            executable = self._freetoken_executable()
            if not executable:
                return (
                    False,
                    "",
                    "runtime_missing",
                    "FreeToken CLI 'ft' is not installed or configured.",
                )
            return True, executable, None, None

        details = self._llama_runtime_details(profile)
        return (
            bool(details["installed"]),
            str(details["path"]),
            details.get("status"),
            details.get("error"),
        )

    def _select_llama_cpp_release_asset(
        self,
        release: dict[str, Any],
        *,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.machine not in {"x86_64", "amd64"}:
            raise RuntimeError("managed llama.cpp install requires x86_64")
        contract = self._llama_cpp_distribution(profile)
        if contract["id"] != LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK:
            tag = str(release.get("tag_name") or "").strip()
            release_pin = str(contract.get("release_pin") or "").strip()
            if release_pin and tag != release_pin:
                raise RuntimeError(
                    f"{contract['id']} release must be pinned to {release_pin}"
                )
            if release.get("draft"):
                raise RuntimeError("injected llama.cpp release is a draft")
            templates = contract.get("asset_templates")
            if not isinstance(templates, dict):
                raise RuntimeError("runtime distribution has no asset allowlist")
            if self.platform_name.startswith("win"):
                preference = str(
                    self._config_get(
                        "openai_compatible_local.llama_cpp.backend_preference",
                        self._config_get(
                            "openai_compatible_local.llama_cpp.backend",
                            "",
                        ),
                    )
                    or ""
                ).strip().casefold()
                nvidia_detected = (
                    preference in {"cuda", "nvidia", "cuda-13.3", "cuda-12.4"}
                    or shutil.which("nvidia-smi") is not None
                )
                if nvidia_detected:
                    keys = ["windows_cuda", "windows_vulkan", "windows_cpu"]
                    if preference.endswith("12.4"):
                        cuda_templates = list(templates.get("windows_cuda") or [])
                        cuda_templates.reverse()
                        templates = dict(templates)
                        templates["windows_cuda"] = tuple(cuda_templates)
                else:
                    keys = ["windows_vulkan", "windows_cpu"]
            elif self.platform_name.startswith("linux"):
                keys = ["linux_vulkan", "linux_cpu"]
            else:
                raise RuntimeError(
                    "managed llama.cpp install supports only Windows/Linux x86_64"
                )
            assets = trusted_llama_cpp_release_assets(
                release,
                repository=str(contract["repository"]),
            )
            for key in keys:
                raw_templates = templates.get(key)
                if not isinstance(raw_templates, (list, tuple)):
                    continue
                for template in raw_templates:
                    expected = str(template).format(tag=tag)
                    matches = [asset for asset in assets if asset["name"] == expected]
                    if len(matches) == 1:
                        return matches[0]
            raise RuntimeError(
                f"{contract['id']} release has no supported allowlisted x86_64 archive"
            )
        if self.platform_name.startswith("win"):
            preference = str(
                self._config_get(
                    "openai_compatible_local.llama_cpp.backend_preference",
                    self._config_get(
                        "openai_compatible_local.llama_cpp.backend",
                        "",
                    ),
                )
                or ""
            ).strip().casefold()
            nvidia_detected = (
                preference in {"cuda", "nvidia", "cuda-13.3", "cuda-12.4"}
                or shutil.which("nvidia-smi") is not None
            )
            cuda_versions = ("13.3", "12.4")
            if preference.endswith("12.4"):
                cuda_versions = ("12.4", "13.3")
            cuda_patterns = (
                *(
                    re.compile(
                        rf"^llama-b\d+-bin-win-cuda-{re.escape(version)}-x64\.zip$"
                    )
                    for version in cuda_versions
                ),
            )
            fallback_patterns = (
                re.compile(r"^llama-b\d+-bin-win-vulkan-x64\.zip$"),
                re.compile(r"^llama-b\d+-bin-win-cpu-x64\.zip$"),
            )
            patterns = (
                (*cuda_patterns, *fallback_patterns)
                if nvidia_detected
                else fallback_patterns
            )
        elif self.platform_name.startswith("linux"):
            patterns = (
                re.compile(r"^llama-b\d+-bin-ubuntu-vulkan-x64\.tar\.gz$"),
                re.compile(r"^llama-b\d+-bin-ubuntu-x64\.tar\.gz$"),
            )
        else:
            raise RuntimeError(
                "managed llama.cpp install supports only Windows/Linux x86_64"
            )

        assets = trusted_llama_cpp_release_assets(
            release,
            repository=str(contract["repository"]),
        )
        for pattern in patterns:
            matches = [
                asset
                for asset in assets
                if pattern.fullmatch(str(asset.get("name") or ""))
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError("ambiguous official llama.cpp release assets")
        raise RuntimeError(
            "official latest llama.cpp release has no supported x86_64 archive"
        )

    def _select_llama_cpp_release_assets(
        self,
        release: dict[str, Any],
        *,
        profile: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Select the binary and, for CUDA builds, its official DLL bundle."""

        contract = self._llama_cpp_distribution(profile)
        asset = self._select_llama_cpp_release_asset(release, profile=profile)
        name = str(asset.get("name") or "")
        if contract["id"] != LLAMA_CPP_RUNTIME_DISTRIBUTION_STOCK:
            tag = str(release.get("tag_name") or "").strip()
            match = re.fullmatch(
                rf"llama-{re.escape(tag)}-bin-win-cuda-(13\.3|12\.4)-x64\.zip",
                name,
            )
            if not match:
                return asset, None
            templates = contract.get("runtime_asset_templates")
            if not isinstance(templates, dict):
                raise RuntimeError(
                    "pinned llama.cpp distribution has no CUDA runtime allowlist"
                )
            runtime_name = templates.get(match.group(1))
            if not isinstance(runtime_name, str) or not runtime_name:
                raise RuntimeError("pinned llama.cpp distribution has no CUDA runtime asset")
            runtime_asset = select_llama_cpp_release_asset(
                release,
                allowed_names={runtime_name},
                repository=str(contract["repository"]),
            )
            return asset, runtime_asset
        match = re.fullmatch(
            r"llama-b\d+-bin-win-cuda-(13\.3|12\.4)-x64\.zip",
            name,
        )
        if not match:
            return asset, None

        cuda_version = match.group(1)
        runtime_name = f"cudart-llama-bin-win-cuda-{cuda_version}-x64.zip"
        try:
            runtime_asset = select_llama_cpp_release_asset(
                release,
                allowed_names={runtime_name},
                repository=str(contract["repository"]),
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "official llama.cpp CUDA binary is missing its matching "
                f"runtime DLL asset: {runtime_name}"
            ) from exc
        return asset, runtime_asset

    @staticmethod
    def _llama_cpp_profile_minimum_build(profile: dict[str, Any]) -> int | None:
        value = profile.get("minimum_llama_cpp_build")
        if value in (None, ""):
            return None
        try:
            minimum = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("trusted llama.cpp profile has invalid minimum build") from exc
        return minimum if minimum > 0 else None

    @staticmethod
    def _release_build_for_sort(release: Any) -> int:
        if not isinstance(release, dict):
            return -1
        build = parse_llama_cpp_release_build(release.get("tag_name"))
        if build is not None:
            return build
        declared_build = release.get("build")
        if isinstance(declared_build, int) and declared_build > 0:
            return declared_build
        assets = release.get("assets")
        if not isinstance(assets, list):
            return -1
        builds = [
            int(match.group("build"))
            for item in assets
            if isinstance(item, dict)
            and (
                match := _LLAMA_CPP_ASSET_BUILD_PATTERN.match(
                    str(item.get("name") or "")
                )
            ) is not None
        ]
        return max(builds, default=-1)

    def _release_meets_profile_minimum(
        self,
        release: Any,
        minimum_build: int | None,
        *,
        allow_prerelease: bool,
    ) -> bool:
        if not isinstance(release, dict) or release.get("draft"):
            return False
        if not allow_prerelease and release.get("prerelease"):
            return False
        build = parse_llama_cpp_release_build(release.get("tag_name"))
        if build is None:
            declared_build = release.get("build")
            if isinstance(declared_build, int) and declared_build > 0:
                build = declared_build
        if build is None:
            # A few known GitHub releases use a semantic version tag while
            # their platform archive still carries the canonical bNNNNN
            # build.  Infer only from the literal official asset names; never
            # parse arbitrary release text.
            assets = release.get("assets")
            if isinstance(assets, list):
                builds = [
                    int(match.group("build"))
                    for item in assets
                    if isinstance(item, dict)
                    and (
                        match := _LLAMA_CPP_ASSET_BUILD_PATTERN.match(
                            str(item.get("name") or "")
                        )
                    ) is not None
                ]
                build = max(builds, default=None)
        if minimum_build is None:
            return True
        return build is not None and build >= minimum_build

    @staticmethod
    def _release_error_is_skippable(exc: RuntimeError) -> bool:
        message = str(exc)
        return (
            message.startswith("official latest llama.cpp release has no supported")
            or message.startswith("official llama.cpp CUDA binary is missing its matching")
        )

    def _select_llama_cpp_release_for_profile(
        self,
        profile: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """Resolve a release and platform archive for one trusted profile.

        ``/releases/latest`` is the stable fast path.  Only the default
        provider may query the bounded recent-release list when that stable
        release is too old or lacks a usable platform archive.  Injected
        providers are intentionally self-contained and never trigger that
        network fallback.
        """

        contract = self._llama_cpp_distribution(profile)
        if contract.get("release_policy") == "pinned":
            latest = (
                self._release_provider()
                if not self._release_provider_is_default
                else _fetch_llama_cpp_release_json(str(contract["release_api"]))
            )
            if not isinstance(latest, dict):
                raise RuntimeError("invalid llama.cpp release JSON")
            release_pin = str(contract.get("release_pin") or "").strip()
            if release_pin and str(latest.get("tag_name") or "").strip() != release_pin:
                raise RuntimeError(
                    f"{contract['id']} release is not the trusted pin {release_pin}"
                )
            if latest.get("draft"):
                raise RuntimeError("trusted llama.cpp release is a draft")
            required_commit = self._llama_cpp_required_commit(profile)
            target_commit = str(latest.get("target_commitish") or "").strip().casefold()
            if required_commit:
                if not re.fullmatch(r"[0-9a-f]{7,40}", target_commit):
                    raise RuntimeError(
                        "trusted llama.cpp pinned release has no exact commit target"
                    )
                if not (
                    target_commit.startswith(required_commit)
                    or required_commit.startswith(target_commit)
                ):
                    raise RuntimeError(
                        "trusted llama.cpp pinned release target does not match "
                        "the required commit"
                    )
            asset, runtime_asset = self._select_llama_cpp_release_assets(
                latest,
                profile=profile,
            )
            return latest, asset, runtime_asset

        minimum_build = self._llama_cpp_profile_minimum_build(profile)
        latest = self._release_provider()
        if not isinstance(latest, dict):
            raise RuntimeError("invalid llama.cpp release JSON")

        latest_error: RuntimeError | None = None
        latest_eligible = self._release_meets_profile_minimum(
            latest,
            minimum_build,
            allow_prerelease=False,
        )
        if latest_eligible:
            try:
                asset, runtime_asset = self._select_llama_cpp_release_assets(
                    latest,
                    profile=profile,
                )
                return latest, asset, runtime_asset
            except RuntimeError as exc:
                latest_error = exc
        elif not self._release_provider_is_default:
            if latest.get("draft"):
                raise RuntimeError("injected llama.cpp release is a draft")
            if minimum_build is not None and not self._release_meets_profile_minimum(
                latest,
                minimum_build,
                allow_prerelease=True,
            ):
                raise RuntimeError(
                    f"injected llama.cpp release does not satisfy b{minimum_build}"
                )
            # A custom provider is an explicit offline/test contract.  Permit
            # its prerelease marker, but never fetch another release list.
            try:
                asset, runtime_asset = self._select_llama_cpp_release_assets(
                    latest,
                    profile=profile,
                )
                return latest, asset, runtime_asset
            except RuntimeError:
                raise

        if not self._release_provider_is_default:
            if latest_error is not None:
                raise latest_error
            raise RuntimeError("injected llama.cpp release is not eligible")

        recent = _default_llama_cpp_recent_releases_provider()
        if not isinstance(recent, list):
            raise RuntimeError("invalid llama.cpp releases JSON")
        ordered_candidates = sorted(
            enumerate(recent),
            key=lambda item: (
                self._release_build_for_sort(item[1]),
                -item[0],
            ),
            reverse=True,
        )
        for _index, candidate in ordered_candidates:
            # A profile with an explicit build floor requires a canonical
            # bNNNNN tag for recent-release comparison.  Legacy profiles
            # without a floor retain the prior asset-only fallback behavior.
            if minimum_build is not None and parse_llama_cpp_release_build(
                candidate.get("tag_name") if isinstance(candidate, dict) else None
            ) is None:
                continue
            if not self._release_meets_profile_minimum(
                candidate,
                minimum_build,
                allow_prerelease=True,
            ):
                continue
            try:
                asset, runtime_asset = self._select_llama_cpp_release_assets(
                    candidate,
                    profile=profile,
                )
            except RuntimeError as exc:
                # A malformed tag, stale asset set, or incomplete CUDA bundle
                # makes only this candidate ineligible.  Unexpected errors
                # (for example a broken trust boundary) must remain visible.
                if self._release_error_is_skippable(exc):
                    continue
                raise
            return candidate, asset, runtime_asset

        if minimum_build is not None:
            raise RuntimeError(
                f"no eligible llama.cpp release provides b{minimum_build} or newer"
            ) from latest_error
        raise RuntimeError("no eligible official llama.cpp release archive") from latest_error

    @staticmethod
    def _safe_archive_target(root: Path, path: PurePosixPath) -> Path:
        target = root.joinpath(*path.parts)
        try:
            target.resolve(strict=False).relative_to(root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise RuntimeError("archive member escaped extraction root") from exc
        return target

    def _extract_llama_cpp_archive(self, archive_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=False)
        name = archive_path.name.casefold()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    relative = validate_zip_archive_member(member)
                    output = self._safe_archive_target(target, relative)
                    if member.is_dir():
                        output.mkdir(parents=True, exist_ok=True)
                        continue
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if output.exists():
                        raise RuntimeError("duplicate llama.cpp zip archive member")
                    with archive.open(member) as source, output.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
            return
        if name.endswith(".tar.gz"):
            with tarfile.open(archive_path, mode="r:gz") as archive:
                for member in archive.getmembers():
                    relative = validate_tar_archive_member(member)
                    output = self._safe_archive_target(target, relative)
                    if member.isdir():
                        output.mkdir(parents=True, exist_ok=True)
                        continue
                    output.parent.mkdir(parents=True, exist_ok=True)
                    if output.exists():
                        raise RuntimeError("duplicate llama.cpp tar archive member")
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("invalid llama.cpp tar archive member")
                    with source, output.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                    output.chmod(int(member.mode) & 0o777)
            return
        raise RuntimeError("unsupported llama.cpp archive type")

    def _install_llama_cpp_runtime(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        with self._runtime_install_lock:
            current = self._llama_runtime_details(profile)
            if current["installed"]:
                return current
            invalid_managed_runtime = (
                current.get("status") == "runtime_invalid"
                and str(current.get("install_source") or "") == "managed"
            )
            if current.get("status") == "unsupported" or invalid_managed_runtime:
                raise RuntimeError(str(current.get("error") or current["status"]))

            release, asset, runtime_asset = (
                self._select_llama_cpp_release_for_profile(profile)
            )
            asset_name = str(asset["name"])
            asset_url = str(asset["browser_download_url"])
            runtime_asset_name = (
                str(runtime_asset["name"])
                if runtime_asset is not None
                else ""
            )
            runtime_asset_url = (
                str(runtime_asset["browser_download_url"])
                if runtime_asset is not None
                else ""
            )
            try:
                runtime_root = self._llama_cpp_distribution_root(profile)
            except RuntimeError:
                raise
            runtime_root.mkdir(parents=True, exist_ok=True)
            if runtime_root.is_symlink():
                raise RuntimeError("refusing symlink llama.cpp runtime directory")
            staging = runtime_root / f".install-{uuid.uuid4().hex}"
            archive_path = staging / asset_name
            extracted = staging / "extracted"
            runtime_extracted = staging / "runtime-extracted"
            current_dir = runtime_root / "current"
            backup = runtime_root / f".previous-{uuid.uuid4().hex}"
            for candidate in (staging, archive_path, extracted, runtime_extracted, current_dir, backup):
                try:
                    validate_managed_child(
                        self.root,
                        candidate,
                        kind="runtime",
                        create_parent=False,
                    )
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
            try:
                staging.mkdir(parents=False, exist_ok=False)
                self._asset_downloader(asset_url, archive_path)
                if not archive_path.is_file() or archive_path.is_symlink():
                    raise RuntimeError("llama.cpp release archive was not downloaded")
                verify_release_asset_digest(archive_path, asset)
                self._extract_llama_cpp_archive(archive_path, extracted)
                executable_name = (
                    "llama-server.exe"
                    if self.platform_name.startswith("win")
                    else "llama-server"
                )
                matches = [
                    path
                    for path in extracted.rglob(executable_name)
                    if path.is_file() and not path.is_symlink()
                ]
                if len(matches) != 1:
                    raise RuntimeError(
                        "llama.cpp archive did not contain exactly one llama-server"
                    )
                server = matches[0]
                if runtime_asset is not None:
                    runtime_archive = staging / runtime_asset_name
                    self._asset_downloader(runtime_asset_url, runtime_archive)
                    if (
                        not runtime_archive.is_file()
                        or runtime_archive.is_symlink()
                    ):
                        raise RuntimeError(
                            "llama.cpp CUDA runtime archive was not downloaded"
                        )
                    verify_release_asset_digest(runtime_archive, runtime_asset)
                    self._extract_llama_cpp_archive(
                        runtime_archive,
                        runtime_extracted,
                    )
                    runtime_dlls = [
                        path
                        for path in runtime_extracted.rglob("*")
                        if path.is_file()
                        and not path.is_symlink()
                        and path.suffix.casefold() == ".dll"
                    ]
                    if not runtime_dlls:
                        raise RuntimeError(
                            "llama.cpp CUDA runtime archive contained no DLLs"
                        )
                    for runtime_dll in runtime_dlls:
                        destination = server.parent / runtime_dll.name
                        if destination.exists() or destination.is_symlink():
                            raise RuntimeError(
                                "duplicate llama.cpp CUDA runtime DLL"
                            )
                        os.replace(runtime_dll, destination)
                server_relative = server.relative_to(extracted)
                minimum = int(profile.get("minimum_llama_cpp_build") or 0) or None
                required_commit = self._llama_cpp_required_commit(profile)
                version = validate_llama_cpp_executable(
                    server,
                    minimum_build=minimum,
                    required_commit=required_commit,
                    # Fresh Windows CUDA binaries can incur one-time loader
                    # latency immediately after extraction. Keep ordinary
                    # probes at the validator default but allow install-time
                    # validation enough time for that first launch.
                    timeout_seconds=60,
                )
                distribution = self._llama_cpp_distribution(profile)
                marker = {
                    "distribution": distribution["id"],
                    "repository": distribution["repository"],
                    "release": str(release.get("tag_name") or ""),
                    "asset": asset_name,
                    "runtime_asset": runtime_asset_name,
                    "version": version.get("version") or "",
                    "build": version.get("build"),
                    "commit": version.get("commit") or "",
                    "executable_relative": server_relative.as_posix(),
                }
                (extracted / _LLAMA_CPP_INSTALL_MARKER).write_text(
                    json.dumps(marker, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                try:
                    validate_managed_child(
                        self.root,
                        current_dir,
                        kind="runtime",
                    )
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc
                if current_dir.is_symlink():
                    raise RuntimeError("refusing symlink llama.cpp current directory")
                if current_dir.exists():
                    os.replace(current_dir, backup)
                try:
                    os.replace(extracted, current_dir)
                except Exception:
                    if backup.exists() and not current_dir.exists():
                        os.replace(backup, current_dir)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            installed = self._llama_runtime_details(profile)
            if not installed["installed"]:
                raise RuntimeError(
                    str(installed.get("error") or "installed llama.cpp runtime is invalid")
                )
            return installed

    @staticmethod
    def _payload(
        *,
        task_id: str | None,
        model: str,
        runtime: str | None,
        phase: str,
        status: str,
        runtime_installed: bool,
        model_installed: bool,
        done: bool,
        error: str | None,
        started_at: str | None,
        updated_at: str | None = None,
        prepare_supported: bool = True,
        runtime_path: str = "",
        model_path: str = "",
        runtime_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = runtime_metadata or {}
        profile = (
            llama_cpp_model_profile(model)
            if runtime == "llama_cpp"
            else None
        )
        auxiliary_artifacts = (
            llama_cpp_auxiliary_artifacts(profile=profile)
            if profile is not None
            else []
        )
        total = 2 if runtime in _MANAGED_RUNTIMES else 0
        completed = (
            int(bool(runtime_installed)) + int(bool(model_installed))
            if total
            else 0
        )
        percent = int((completed * 100) / total) if total else 0
        prepared = bool(
            runtime_installed
            and model_installed
            and status in {"ready", "succeeded"}
        )
        return {
            "task_id": task_id,
            "model": model,
            "runtime": runtime,
            "runtime_distribution": (
                str(metadata.get("distribution") or "").strip()
                or llama_cpp_runtime_distribution(profile=profile)
                if runtime == "llama_cpp"
                else None
            ),
            "phase": phase,
            "status": status,
            "completed": completed,
            "total": total,
            "percent": percent,
            "done": bool(done),
            "error": error,
            "runtime_installed": bool(runtime_installed),
            "model_installed": bool(model_installed),
            "prepared": prepared,
            "started_at": started_at,
            "updated_at": updated_at or _utc_now(),
            "prepare_supported": bool(prepare_supported),
            "runtime_path": runtime_path,
            "model_path": model_path,
            "auxiliary_artifacts": auxiliary_artifacts,
            "runtime_version": str(
                metadata.get("version") or metadata.get("runtime_version") or ""
            ),
            "runtime_build": metadata.get(
                "build", metadata.get("runtime_build")
            ),
            "runtime_executable": str(
                metadata.get("executable")
                or metadata.get("runtime_executable")
                or runtime_path
            ),
            "runtime_install_source": str(
                metadata.get("install_source")
                or metadata.get("runtime_install_source")
                or ""
            ),
            "runtime_release": str(
                metadata.get("release") or metadata.get("runtime_release") or ""
            ),
            "runtime_asset": str(
                metadata.get("asset") or metadata.get("runtime_asset") or ""
            ),
        }

    def status(
        self,
        model: str,
        runtime: str | None = None,
    ) -> dict[str, Any]:
        model_id = self._validate_model_identifier(model)
        resolved_runtime, profile = self._resolve_profile(
            model_id,
            runtime,
            require_trusted=False,
        )

        if not resolved_runtime or not profile:
            return self._payload(
                task_id=None,
                model=model_id,
                runtime=None,
                phase="external",
                status="external",
                runtime_installed=False,
                model_installed=False,
                done=True,
                error=None,
                started_at=None,
                prepare_supported=False,
            )

        canonical_model = str(profile.get("id") or model_id)
        key = (resolved_runtime, canonical_model)
        with self._lock:
            active_id = self._active.get(key)
            active = (
                copy.deepcopy(self._tasks.get(active_id))
                if active_id
                else None
            )
        if active and not active["done"]:
            return active

        destination = self._model_dir(
            resolved_runtime,
            canonical_model,
        )
        model_artifact: Path | None = (
            destination
            if self._model_installed(resolved_runtime, profile, destination)
            else None
        )
        if resolved_runtime == "llama_cpp" and model_artifact is None:
            model_artifact = self._discover_existing_llama_cpp_artifact(profile)
        model_installed = model_artifact is not None

        if resolved_runtime == "llama_cpp":
            runtime_metadata = self._llama_runtime_details(profile)
            runtime_installed = bool(runtime_metadata["installed"])
            runtime_path = str(runtime_metadata["path"])
            runtime_terminal_status = runtime_metadata.get("status")
            runtime_error = runtime_metadata.get("error")
        else:
            runtime_metadata = {}
            (
                runtime_installed,
                runtime_path,
                runtime_terminal_status,
                runtime_error,
            ) = self._runtime_state(resolved_runtime, profile)

        runtime_invalid_managed = (
            runtime_terminal_status == "runtime_invalid"
            and str(runtime_metadata.get("install_source") or "") == "managed"
        )
        if runtime_terminal_status in {"installer_required", "unsupported"} or runtime_invalid_managed:
            return self._payload(
                task_id=None,
                model=canonical_model,
                runtime=resolved_runtime,
                phase="runtime",
                status=runtime_terminal_status,
                runtime_installed=runtime_installed,
                model_installed=model_installed,
                done=True,
                error=runtime_error,
                started_at=None,
                prepare_supported=False,
                runtime_path=runtime_path,
                model_path=str(model_artifact) if model_artifact else "",
                runtime_metadata=runtime_metadata,
            )

        downloadable = bool(
            profile.get("source_repository")
            and (
                resolved_runtime == "freetoken"
                or profile.get("gguf_filename")
            )
        )
        if not model_installed and not downloadable:
            return self._payload(
                task_id=None,
                model=canonical_model,
                runtime=resolved_runtime,
                phase="model",
                status="manual_model_required",
                runtime_installed=runtime_installed,
                model_installed=False,
                done=True,
                error="This trusted profile has no managed-download artifact contract.",
                started_at=None,
                prepare_supported=False,
                runtime_path=runtime_path,
                runtime_metadata=runtime_metadata,
            )

        if not runtime_installed:
            return self._payload(
                task_id=None,
                model=canonical_model,
                runtime=resolved_runtime,
                phase="runtime",
                status=runtime_terminal_status or "runtime_missing",
                runtime_installed=False,
                model_installed=model_installed,
                done=True,
                error=runtime_error,
                started_at=None,
                runtime_path="",
                model_path=str(model_artifact) if model_artifact else "",
                runtime_metadata=runtime_metadata,
            )

        if not model_installed:
            return self._payload(
                task_id=None,
                model=canonical_model,
                runtime=resolved_runtime,
                phase="model",
                status="model_missing",
                runtime_installed=True,
                model_installed=False,
                done=True,
                error=None,
                started_at=None,
                runtime_path=runtime_path,
                runtime_metadata=runtime_metadata,
            )

        return self._payload(
            task_id=None,
            model=canonical_model,
            runtime=resolved_runtime,
            phase="complete",
            status="ready",
            runtime_installed=True,
            model_installed=True,
            done=True,
            error=None,
            started_at=None,
            runtime_path=runtime_path,
            model_path=str(model_artifact),
            runtime_metadata=runtime_metadata,
        )

    def start_prepare(
        self,
        model: str,
        runtime: str | None = None,
    ) -> dict[str, Any]:
        resolved_runtime, profile = self._resolve_profile(
            model,
            runtime,
            require_trusted=True,
        )
        assert resolved_runtime is not None
        assert profile is not None

        canonical_model = str(profile.get("id") or model).strip()
        current = self.status(canonical_model, resolved_runtime)
        if current["prepared"]:
            return current
        if not current["prepare_supported"]:
            return current

        key = (resolved_runtime, canonical_model)
        with self._lock:
            active_id = self._active.get(key)
            active = (
                self._tasks.get(active_id)
                if active_id
                else None
            )
            if active and not active["done"]:
                return copy.deepcopy(active)

            now = _utc_now()
            task_id = uuid.uuid4().hex
            task = self._payload(
                task_id=task_id,
                model=canonical_model,
                runtime=resolved_runtime,
                phase="queued",
                status="queued",
                runtime_installed=bool(current["runtime_installed"]),
                model_installed=bool(current["model_installed"]),
                done=False,
                error=None,
                started_at=now,
                updated_at=now,
                runtime_path=str(current.get("runtime_path") or ""),
                model_path=str(current.get("model_path") or ""),
                runtime_metadata=current,
            )
            self._tasks[task_id] = task
            self._active[key] = task_id

        thread = threading.Thread(
            target=self._run_prepare,
            args=(task_id, resolved_runtime, profile),
            daemon=True,
            name=f"local-llm-prepare-{task_id[:8]}",
        )
        thread.start()
        return copy.deepcopy(task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        value = str(task_id or "").strip()
        with self._lock:
            task = self._tasks.get(value)
            if task is not None:
                return copy.deepcopy(task)
        return self._payload(
            task_id=value or None,
            model="",
            runtime=None,
            phase="error",
            status="not_found",
            runtime_installed=False,
            model_installed=False,
            done=True,
            error="local runtime prepare task not found",
            started_at=None,
            prepare_supported=False,
        )

    def _set_task(
        self,
        task_id: str,
        *,
        phase: str,
        status: str,
        runtime_installed: bool,
        model_installed: bool,
        done: bool,
        error: str | None,
        runtime_path: str = "",
        model_path: str = "",
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            previous = self._tasks[task_id]
            self._tasks[task_id] = self._payload(
                task_id=task_id,
                model=str(previous["model"]),
                runtime=str(previous["runtime"]),
                phase=phase,
                status=status,
                runtime_installed=runtime_installed,
                model_installed=model_installed,
                done=done,
                error=error,
                started_at=previous["started_at"],
                runtime_path=runtime_path or str(previous.get("runtime_path") or ""),
                model_path=model_path or str(previous.get("model_path") or ""),
                runtime_metadata=runtime_metadata or previous,
            )

    def _run_prepare(
        self,
        task_id: str,
        runtime: str,
        profile: dict[str, Any],
    ) -> None:
        model = str(profile["id"])
        key = (runtime, model)
        try:
            if runtime == "llama_cpp":
                runtime_metadata = self._llama_runtime_details(profile)
                runtime_installed = bool(runtime_metadata["installed"])
                runtime_path = str(runtime_metadata["path"])
                runtime_status = runtime_metadata.get("status")
                runtime_error = runtime_metadata.get("error")
                can_replace_invalid_external = (
                    runtime_status == "runtime_invalid"
                    and str(runtime_metadata.get("install_source") or "") != "managed"
                )
                if not runtime_installed and (
                    runtime_status == "runtime_missing" or can_replace_invalid_external
                ):
                    self._set_task(
                        task_id,
                        phase="runtime",
                        status="running",
                        runtime_installed=False,
                        model_installed=False,
                        done=False,
                        error=None,
                        runtime_metadata=runtime_metadata,
                    )
                    try:
                        runtime_metadata = self._install_llama_cpp_runtime(profile)
                    except Exception as exc:
                        self._set_task(
                            task_id,
                            phase="runtime",
                            status="failed",
                            runtime_installed=False,
                            model_installed=False,
                            done=True,
                            error=str(exc),
                            runtime_metadata=runtime_metadata,
                        )
                        return
                    runtime_installed = True
                    runtime_path = str(runtime_metadata["path"])
            else:
                runtime_metadata = {}
                (
                    runtime_installed,
                    runtime_path,
                    runtime_status,
                    runtime_error,
                ) = self._runtime_state(runtime, profile)
            if not runtime_installed:
                self._set_task(
                    task_id,
                    phase="runtime",
                    status=runtime_status or "runtime_missing",
                    runtime_installed=False,
                    model_installed=False,
                    done=True,
                    error=runtime_error or "managed runtime is not installed",
                    runtime_metadata=runtime_metadata,
                )
                return

            destination = self._model_dir(runtime, model)
            existing_artifact: Path | None = (
                destination
                if self._model_installed(runtime, profile, destination)
                else None
            )
            if runtime == "llama_cpp" and existing_artifact is None:
                existing_artifact = self._discover_existing_llama_cpp_artifact(
                    profile
                )
            if existing_artifact is not None:
                self._set_task(
                    task_id,
                    phase="complete",
                    status="succeeded",
                    runtime_installed=True,
                    model_installed=True,
                    done=True,
                    error=None,
                    runtime_path=runtime_path,
                    model_path=str(existing_artifact),
                    runtime_metadata=runtime_metadata,
                )
                return

            self._set_task(
                task_id,
                phase="model",
                status="running",
                runtime_installed=True,
                model_installed=False,
                done=False,
                error=None,
                runtime_path=runtime_path,
                runtime_metadata=runtime_metadata,
            )
            destination = self._prepare_model(runtime, profile)
            verified = self._model_installed(runtime, profile, destination)
            if not verified:
                self._set_task(
                    task_id,
                    phase="verify",
                    status="failed",
                    runtime_installed=True,
                    model_installed=False,
                    done=True,
                    error="downloaded model failed managed-profile verification",
                    runtime_path=runtime_path,
                    runtime_metadata=runtime_metadata,
                )
                return

            self._set_task(
                task_id,
                phase="complete",
                status="succeeded",
                runtime_installed=True,
                model_installed=True,
                done=True,
                error=None,
                runtime_path=runtime_path,
                model_path=str(destination),
                runtime_metadata=runtime_metadata,
            )
        except Exception as exc:
            runtime_metadata = locals().get("runtime_metadata") or {}
            self._set_task(
                task_id,
                phase="error",
                status="failed",
                runtime_installed=bool(runtime_metadata.get("installed")),
                model_installed=False,
                done=True,
                error=str(exc),
                runtime_path=str(runtime_metadata.get("path") or ""),
                runtime_metadata=runtime_metadata,
            )
        finally:
            with self._lock:
                if self._active.get(key) == task_id:
                    self._active.pop(key, None)

    def _prepare_model(
        self,
        runtime: str,
        profile: dict[str, Any],
    ) -> Path:
        repository = str(profile.get("source_repository") or "").strip()
        model = str(profile.get("id") or "").strip()
        if not repository or not model:
            raise RuntimeError("trusted profile has no source repository")

        # Repository is obtained exclusively from the trusted in-process
        # registry. No caller-provided URL/path participates in this method.
        if runtime == "freetoken":
            trusted = freetoken_model_profile(model)
        elif runtime == "llama_cpp":
            trusted = llama_cpp_model_profile(model)
        else:
            trusted = None
        if (
            not trusted
            or str(trusted.get("source_repository") or "") != repository
        ):
            raise RuntimeError("managed download repository is not trusted")

        llama_contract: tuple[str, list[str], str] | None = None
        llama_mtp_contract: tuple[list[str], bool] | None = None
        llama_mtp_download_source: tuple[str, str, str] | None = None
        llama_auxiliary_contract: list[dict[str, Any]] | None = None
        if runtime == "llama_cpp":
            profile_contract = self._llama_cpp_artifact_contract(profile)
            trusted_contract = self._llama_cpp_artifact_contract(trusted)
            if profile_contract != trusted_contract:
                raise RuntimeError(
                    "managed llama.cpp artifact contract is not trusted"
                )
            llama_contract = trusted_contract
            profile_mtp_contract = self._llama_cpp_mtp_companion_contract(profile)
            trusted_mtp_contract = self._llama_cpp_mtp_companion_contract(trusted)
            if profile_mtp_contract != trusted_mtp_contract:
                raise RuntimeError(
                    "managed llama.cpp MTP artifact contract is not trusted"
                )
            llama_mtp_contract = trusted_mtp_contract
            profile_mtp_download_source = self._llama_cpp_mtp_download_source(profile)
            trusted_mtp_download_source = self._llama_cpp_mtp_download_source(trusted)
            if profile_mtp_download_source != trusted_mtp_download_source:
                raise RuntimeError(
                    "managed llama.cpp MTP download source is not trusted"
                )
            llama_mtp_download_source = trusted_mtp_download_source
            profile_auxiliary_contract = self._llama_cpp_auxiliary_contract(profile)
            trusted_auxiliary_contract = self._llama_cpp_auxiliary_contract(trusted)
            if profile_auxiliary_contract != trusted_auxiliary_contract:
                raise RuntimeError(
                    "managed llama.cpp auxiliary artifact contract is not trusted"
                )
            llama_auxiliary_contract = trusted_auxiliary_contract

        destination = self._model_dir(runtime, model)
        model_storage_root = (
            self._configured_llama_cpp_model_root()
            if runtime == "llama_cpp"
            else (
                self.root / "models"
                if self._runtime_root_explicit
                else default_managed_models_root()
            )
        )
        try:
            destination = validate_managed_child(
                model_storage_root,
                destination,
                kind="model",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if runtime == "llama_cpp" and not destination.exists():
            discovered = self._discover_existing_llama_cpp_artifact(trusted)
            if discovered is not None:
                return discovered

        try:
            partial = validate_managed_child(
                model_storage_root,
                destination.with_name(destination.name + ".partial"),
                kind="model",
                create_parent=True,
            )
            destination = validate_managed_child(
                model_storage_root,
                destination,
                kind="model",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        if destination.exists():
            if self._model_installed(runtime, profile, destination):
                return destination
            raise RuntimeError(
                "managed model destination exists but failed verification"
            )
        if partial.is_symlink():
            raise RuntimeError("refusing symlink managed-download staging path")
        partial.mkdir(parents=True, exist_ok=True)

        if runtime == "freetoken":
            snapshot_download(
                repo_id=repository,
                local_dir=str(partial),
            )
            if not any(
                path.is_file()
                for path in partial.rglob("*.safetensors")
            ):
                raise RuntimeError(
                    "trusted FreeToken repository did not produce safetensors"
                )
        else:
            assert llama_contract is not None
            assert llama_mtp_contract is not None
            assert llama_mtp_download_source is not None
            assert llama_auxiliary_contract is not None
            _, filenames, repository_subdir = llama_contract
            companions, companions_required = llama_mtp_contract
            (
                companion_repository,
                companion_revision,
                companion_repository_subdir,
            ) = llama_mtp_download_source
            required_filenames = list(
                dict.fromkeys(
                    [
                        *filenames,
                        *(companions if companions_required else []),
                        *(
                            str(artifact["filename"])
                            for artifact in llama_auxiliary_contract
                        ),
                    ]
                )
            )
            artifact_expectations: dict[str, dict[str, Any]] = {}
            if filenames:
                artifact_expectations[filenames[0]] = {
                    "size_bytes": trusted.get("gguf_size_bytes"),
                    "sha256": trusted.get("gguf_sha256"),
                }
            for artifact in llama_auxiliary_contract:
                artifact_expectations[str(artifact["filename"])] = artifact
            shutil.rmtree(partial, ignore_errors=True)
            partial.mkdir(parents=True, exist_ok=True)
            download_root = partial / ".download"
            try:
                for filename in required_filenames:
                    artifact_metadata = next(
                        (
                            artifact
                            for artifact in llama_auxiliary_contract
                            if str(artifact["filename"]) == filename
                        ),
                        None,
                    )
                    artifact_subdir = str(
                        (artifact_metadata or {}).get("repository_subdir") or ""
                    ).strip()
                    is_mtp_companion = filename in companions
                    effective_subdir = (
                        companion_repository_subdir
                        if is_mtp_companion and companion_repository_subdir
                        else artifact_subdir or repository_subdir
                    )
                    repository_filename = (
                        f"{effective_subdir}/{filename}"
                        if effective_subdir
                        else filename
                    )
                    download_repository = (
                        companion_repository
                        if is_mtp_companion and companion_repository
                        else repository
                    )
                    download_kwargs: dict[str, Any] = {
                        "repo_id": download_repository,
                        "filename": repository_filename,
                        "local_dir": str(download_root),
                    }
                    source_revision = (
                        companion_revision
                        if is_mtp_companion and companion_revision
                        else str(trusted.get("source_revision") or "").strip()
                    )
                    if source_revision:
                        download_kwargs["revision"] = source_revision
                    downloaded = Path(hf_hub_download(**download_kwargs))
                    if not downloaded.is_file() or downloaded.is_symlink():
                        raise RuntimeError(
                            "trusted llama.cpp GGUF was not downloaded"
                        )
                    try:
                        downloaded.resolve().relative_to(
                            download_root.resolve()
                        )
                    except (OSError, ValueError) as exc:
                        raise RuntimeError(
                            "trusted llama.cpp download escaped staging directory"
                        ) from exc
                    expectation = artifact_expectations.get(filename) or {}
                    self._verify_hf_artifact(
                        downloaded,
                        expected_size=expectation.get("size_bytes"),
                        expected_sha256=expectation.get("sha256"),
                    )
                    os.replace(downloaded, partial / filename)

                shutil.rmtree(download_root, ignore_errors=True)
                if not self._model_installed(
                    "llama_cpp",
                    trusted,
                    partial,
                ):
                    raise RuntimeError(
                        "trusted llama.cpp artifacts were not completely downloaded"
                    )
            except Exception:
                shutil.rmtree(partial, ignore_errors=True)
                raise

        # Stable .partial staging permits huggingface_hub to resume an
        # interrupted download. Only a completely verified directory becomes
        # visible at the final managed path.
        try:
            partial = validate_managed_child(
                model_storage_root,
                partial,
                kind="model",
            )
            destination = validate_managed_child(
                model_storage_root,
                destination,
                kind="model",
            )
            os.replace(partial, destination)
        except OSError:
            if self._model_installed(runtime, profile, destination):
                shutil.rmtree(partial, ignore_errors=True)
            else:
                if runtime == "llama_cpp":
                    shutil.rmtree(partial, ignore_errors=True)
                raise
        return destination
