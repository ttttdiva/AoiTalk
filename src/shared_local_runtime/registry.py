"""Trusted local configuration snapshots. HTTP clients can only name profiles."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import time
from urllib.parse import urlsplit
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "cache" / "shared_local_runtime"
LLAMA_KEYS = frozenset("executable model_path model_root model_dir model_alias host port context_size gpu_layers extra_args auto_start readiness_timeout reasoning_effort mtp_enabled".split())
PROFILE_KEYS = frozenset("model_path model_alias context_size extra_args gpu_layers reasoning_effort mtp_enabled auto_start readiness_timeout".split())
FREETOKEN_KEYS = frozenset("host port auto_start readiness_timeout readiness_timeout_seconds extra_args".split())
STRATA_KEYS = frozenset("source_root data_dir gguf_dir host port context_size vision auto_start readiness_timeout prefill vram_reserve_mib expert_cache".split())


_PROFILE_OPTION_ALIASES = {
    "--parallel": frozenset({"--parallel", "-np"}),
    "-np": frozenset({"--parallel", "-np"}),
    "--reasoning": frozenset({"--reasoning", "-rea"}),
    "-rea": frozenset({"--reasoning", "-rea"}),
    "--log-verbosity": frozenset({"--log-verbosity", "--verbosity", "-lv"}),
    "--verbosity": frozenset({"--log-verbosity", "--verbosity", "-lv"}),
    "-lv": frozenset({"--log-verbosity", "--verbosity", "-lv"}),
}


def _argv_option(token):
    return str(token).split("=", 1)[0].casefold()


def _argv_span(values, index):
    """Return the option/value span for one trusted or operator token."""

    token = str(values[index])
    if (
        token.startswith("-")
        and "=" not in token
        and index + 1 < len(values)
        and not str(values[index + 1]).startswith("-")
    ):
        return 2
    return 1


def _profile_option_aliases(option):
    return _PROFILE_OPTION_ALIASES.get(option, frozenset({option}))


def _profile_launch_args(model):
    """Return the trusted profile-owned argv contract for a model.

    The coordinator may outlive the application process that selected a model.
    Keep the small, model-owned launch contract in the signed-by-source
    snapshot as ordinary ``extra_args`` so an older coordinator which does not
    know a newly registered profile still launches the same parser mode.  The
    profile registry is the only source for these tokens; request/config
    values are never promoted into this contract.
    """

    try:
        from src.llm.openai_compatible_local_profiles import llama_cpp_model_profile

        profile = llama_cpp_model_profile(model)
    except Exception:
        profile = None
    if not isinstance(profile, dict) or profile.get("runtime") != "llama_cpp":
        return []

    values = []
    for key in ("default_args", "required_args"):
        raw = profile.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(str(item).strip() for item in raw if str(item).strip())

    # ``thinking_mode=always`` is a parser contract.  Remove a stale operator
    # override from the frozen copy before adding the trusted value.  Newer
    # launch planners do the same; doing it here also protects an older broker
    # process that has not imported this profile yet.
    if str(profile.get("thinking_mode") or "").strip().lower() == "always":
        normalized = []
        index = 0
        while index < len(values):
            token = values[index]
            option = _argv_option(token)
            if option in _profile_option_aliases("--reasoning"):
                index += _argv_span(values, index)
                continue
            normalized.append(token)
            index += 1
        values = normalized
        values.extend(["--reasoning", "on"])

    # De-duplicate profile-owned option/value pairs.  Keeping the complete span
    # together avoids leaving a stray value when two profile fragments mention
    # the same switch.
    result = []
    seen_options = set()
    index = 0
    while index < len(values):
        token = values[index]
        option = _argv_option(token)
        if token.startswith("-"):
            aliases = _profile_option_aliases(option)
            if aliases & seen_options:
                index += _argv_span(values, index)
                continue
            seen_options.update(aliases)
        span = _argv_span(values, index)
        result.extend(values[index : index + span])
        index += span
    return result


def _export_trusted_profile_args(local, model):
    """Project trusted profile flags into the allowlisted llama snapshot."""

    llama = local.get("llama_cpp")
    if not isinstance(llama, dict):
        return
    trusted = _profile_launch_args(model)
    if not trusted:
        return
    try:
        from src.llm.openai_compatible_local_profiles import llama_cpp_model_profile

        profile = llama_cpp_model_profile(model)
    except Exception:
        profile = None
    required_options = set()
    if isinstance(profile, dict):
        for token in profile.get("required_args") or ():
            if str(token).startswith("-"):
                required_options.update(_profile_option_aliases(_argv_option(token)))
        if str(profile.get("thinking_mode") or "").strip().lower() == "always":
            required_options.update(_profile_option_aliases("--reasoning"))
    raw = llama.get("extra_args", [])
    if isinstance(raw, str):
        try:
            raw = shlex.split(raw, posix=os.name != "nt")
        except ValueError as exc:
            raise ValueError("Shared launch arguments cannot be parsed") from exc
    if not isinstance(raw, (list, tuple)):
        raw = []
    current = [str(item).strip() for item in raw if str(item).strip()]

    # Required profile flags must win over stale operator extras, including
    # aliases such as ``-rea`` for ``--reasoning``.  Profile defaults retain
    # the existing launch-plan rule: an explicit operator value wins, while a
    # missing default is appended from the trusted profile.
    filtered = []
    index = 0
    while index < len(current):
        token = current[index]
        if str(token).startswith("-") and _argv_option(token) in required_options:
            index += _argv_span(current, index)
            continue
        filtered.append(token)
        index += 1
    current = filtered
    existing_options = {
        _argv_option(token)
        for token in current
        if str(token).startswith("-")
    }
    index = 0
    while index < len(trusted):
        token = trusted[index]
        option = _argv_option(token)
        span = _argv_span(trusted, index)
        equivalent = bool(_profile_option_aliases(option) & existing_options)
        if option in required_options or not equivalent:
            current.extend(trusted[index : index + span])
            if token.startswith("-"):
                existing_options.update(_profile_option_aliases(option))
        index += span
    llama["extra_args"] = current


class SnapshotConfig:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        value = self.data
        for segment in key.split("."):
            if not isinstance(value, dict) or segment not in value:
                return default
            value = value[segment]
        return value


def _get(config, key, default=None):
    if isinstance(config, dict):
        return SnapshotConfig(config).get(key, default)
    return config.get(key, default) if config is not None else default


def snapshot(config, model, overrides=None):
    """Allowlist only launch settings, excluding API keys and unrelated user data."""
    local = {k: _get(config, "openai_compatible_local." + k) for k in ("base_url", "runtime_root")}
    local["model"] = model
    for name, keys in (("llama_cpp", LLAMA_KEYS), ("freetoken", FREETOKEN_KEYS), ("strata", STRATA_KEYS)):
        raw = _get(config, "openai_compatible_local." + name, {}) or {}
        raw = dict(raw) if isinstance(raw, dict) else {}
        if name == "llama_cpp" and overrides:
            raw.update(overrides)
        filtered = {key: raw[key] for key in keys if key in raw}
        if name == "llama_cpp" and isinstance(raw.get("profile_runtime"), dict):
            filtered["profile_runtime"] = {
                key: {field: value[field] for field in PROFILE_KEYS if field in value}
                for key, value in raw["profile_runtime"].items() if isinstance(value, dict)
            }
        local[name] = filtered
    _export_trusted_profile_args(local, model)
    data = {"llm_provider": "openai_compatible_local", "llm_model": model,
            "llm_base_url": _get(config, "llm_base_url"), "openai_compatible_local": local}
    # Authentication and model-download switches are outside this local lifecycle
    # contract. Do not accidentally persist a credential inside free-form argv.
    forbidden = re.compile(r"(?:^|\s)(?:--(?:api-key(?:-file)?|hf-token|hf-repo|hf-file|model-url|mmproj-url|ssl-key-file|ssl-cert-file)|-hf|-hff|-hft|-mu)(?:=|\s|$)", re.I)
    for name in ("llama_cpp", "freetoken"):
        values = [local[name]]
        values.extend(local[name].get("profile_runtime", {}).values())
        for value in values:
            extra = value.get("extra_args", [])
            text = " ".join(str(item) for item in extra) if isinstance(extra, list) else " ".join(shlex.split(str(extra)))
            if forbidden.search(text):
                raise ValueError("Shared launch arguments cannot contain credentials or model-download switches")
    for value in (local.get("base_url"), data.get("llm_base_url")):
        if value and (urlsplit(value).username or urlsplit(value).password or urlsplit(value).query):
            raise ValueError("Shared runtime snapshots cannot contain credential-bearing URLs")
    # Reject non-JSON objects rather than serializing reprs or credentials.
    return json.loads(json.dumps(data, allow_nan=False))


# Only these known launch/deployment inputs are isolated in the coordinator.
# PATH, CUDA/device visibility, model-cache and storage safety policy stay intact.
RUNTIME_ENV_INPUTS = frozenset("""
LLAMA_CPP_AUTO_START LLAMA_CPP_CONTEXT_SIZE LLAMA_CPP_EXTRA_ARGS LLAMA_CPP_GPU_LAYERS
LLAMA_CPP_HOST LLAMA_CPP_MODEL_ALIAS LLAMA_CPP_MODEL_PATH LLAMA_CPP_MODEL_ROOT
LLAMA_CPP_PORT LLAMA_CPP_READINESS_TIMEOUT LLAMA_CPP_EXECUTABLE LLAMA_SERVER_EXE
MUSE_GLIMMER_MODEL_PATH AOITALK_LLAMA_CPP_MODEL_ROOT AOITALK_LOCAL_LLM_RUNTIME_ROOT
FREETOKEN_AUTO_START FREETOKEN_EXTRA_ARGS FREETOKEN_HOST FREETOKEN_PORT FREETOKEN_READINESS_TIMEOUT
STRATA_AUTO_START STRATA_DATA_DIR STRATA_SOURCE_ROOT OPENAI_COMPATIBLE_LOCAL_BASE_URL
AOITALK_ENTERPRISE_BACKEND AOITALK_BACKEND AOITALK_ACTIVE_DEPLOYMENT_BACKEND
AOITALK_LLM_BACKEND AOITALK_DEPLOYMENT_BACKEND AOITALK_RUNTIME_BACKEND AOITALK_LLM_MODE
""".split())


def isolate_runtime_environment(environ):
    """Remove first-launcher's overrides only after clients froze effective values."""
    for name in RUNTIME_ENV_INPUTS:
        environ.pop(name, None)


def effective_snapshot(config, model, core, overrides=None):
    """Resolve approved launch inputs in the requesting app, before crossing IPC.

    Reuse the actual runtime resolvers, so environment/config/profile precedence
    remains identical to an ordinary launch. Never copy arbitrary environment data.
    """
    from src.services.local_llm_paths import resolve_managed_runtime_root, resolve_llama_cpp_model_root
    data = snapshot(config, model, overrides)
    local = data["openai_compatible_local"]
    runtime = core.managed_local_runtime_for_model(config, model)
    if runtime == "llama_cpp":
        settings = core._llama_cpp_settings(config, model=model, overrides=overrides, is_windows=core._IS_WINDOWS)
        keys = LLAMA_KEYS
        endpoint = core._llama_cpp_base_url(config, model=model, overrides=overrides, is_windows=core._IS_WINDOWS)
    elif runtime == "freetoken":
        settings = core._freetoken_settings(config, model=model, is_windows=core._IS_WINDOWS)
        keys = FREETOKEN_KEYS
        endpoint = core._freetoken_base_url(config, model=model, is_windows=core._IS_WINDOWS)
    elif runtime == "strata":
        settings = core.strata_settings(config, model=model)
        keys = STRATA_KEYS
        endpoint = core.strata_base_url(config, model=model)
    else:
        raise ValueError("Only managed local profiles have effective snapshots")
    local[runtime].update({key: settings[key] for key in keys if key in settings})
    local["runtime_root"] = str(resolve_managed_runtime_root(config, create=False).path)
    if not local["llama_cpp"].get("model_root"):
        local["llama_cpp"]["model_root"] = str(resolve_llama_cpp_model_root(config, create=False).path)
    local["base_url"] = endpoint
    data["llm_base_url"] = endpoint
    # Reapply the allowlist and credential/download guards to resolved argv too.
    return snapshot(SnapshotConfig(data), model)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


_ATOMIC_REPLACE_ATTEMPTS = 4
_ATOMIC_REPLACE_DELAY_SECONDS = 0.02
_ATOMIC_RETRYABLE_WINERRORS = frozenset({5, 32, 33})


def _same_bytes(path, payload):
    try:
        return path.read_bytes() == payload
    except FileNotFoundError:
        return False
    except OSError:
        # A reader/AV can hold the canonical path open while the writer is
        # deciding whether it can skip the replace.  Treat that as unknown and
        # let the bounded replace path establish the result.
        return None


def _retryable_replace_error(error):
    if getattr(error, "winerror", None) in _ATOMIC_RETRYABLE_WINERRORS:
        return True
    return getattr(error, "errno", None) in {
        errno.EACCES,
        errno.EPERM,
        errno.EBUSY,
    }


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if _same_bytes(path, payload) is True:
        # Snapshot/profile files are content addressed.  Avoid replacing an
        # identical canonical file so Windows readers and AV scanners do not
        # contend on a needless same-value rename.
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(payload)
        for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
            if _same_bytes(path, payload) is True:
                return
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if not _retryable_replace_error(error):
                    raise
                if _same_bytes(path, payload) is True:
                    return
                if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(_ATOMIC_REPLACE_DELAY_SECONDS * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def register(config, model, overrides=None, *, root=ROOT, core=None):
    data = effective_snapshot(config, model, core, overrides) if core is not None else snapshot(config, model, overrides)
    identifier = digest(data)
    _atomic(root / "snapshots" / (identifier + ".json"), data)
    return identifier


def publish(model, identifier, *, runtime=None, root=ROOT):
    # Publish only after successful validation/ensure, never on a failed switch.
    _atomic(root / "profiles" / (digest(model) + ".json"), {"model": model, "snapshot_id": identifier, "runtime": runtime})


def read(model, identifier=None, *, root=ROOT):
    if not isinstance(model, str) or not model or len(model) > 512:
        raise ValueError("A registered profile_id is required")
    if identifier is None:
        path = root / "profiles" / (digest(model) + ".json")
        try:
            identifier = json.loads(path.read_text(encoding="utf-8"))["snapshot_id"]
        except FileNotFoundError as exc:
            raise ValueError("Profile is not registered; select it once in AoiTalk") from exc
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("Invalid snapshot_id")
    path = root / "snapshots" / (identifier + ".json")
    if path.stat().st_size > 65536:
        raise ValueError("Configuration snapshot is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if digest(data) != identifier or data.get("llm_model") != model:
        raise ValueError("Configuration snapshot identity mismatch")
    return SnapshotConfig(data), identifier
