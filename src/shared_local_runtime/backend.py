"""Adapter to AoiTalk's existing validated launch plans and lifecycle ledger."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .registry import digest


def is_managed(config, model, core):
    from src.llm.deployment_resolver import resolve_llm_deployment
    deployment = resolve_llm_deployment(config)
    if deployment is not None and deployment.backend == "external":
        return False
    runtime = core.managed_local_runtime_for_model(config, model)
    if runtime == "strata":
        settings = core.strata_settings(config, model=model)
    elif runtime == "freetoken":
        settings = core._freetoken_settings(config, model=model, is_windows=core._IS_WINDOWS)
    elif runtime == "llama_cpp":
        settings = core._llama_cpp_settings(config, model=model, is_windows=core._IS_WINDOWS)
    else:
        return False
    return bool(settings.get("auto_start"))


def _artifact_identity(path):
    path = Path(path).expanduser()
    if not path.exists():
        return None
    info = path.stat()
    if path.is_file() and path.suffix == ".json" and info.st_size < 1024 * 1024:
        import hashlib
        return [str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()]
    return [str(path.resolve()), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class LifecycleBackend:
    def __init__(self):
        from src import service_manager
        # Explicit backend injection. Never inferred from pytest or exceptions.
        service_manager._shared_runtime_direct_backend = True
        self.core = service_manager

    def prepare(self, config, model):
        core = self.core
        if not is_managed(config, model, core):
            raise ValueError("Profile is external or auto_start is disabled")
        runtime = core.managed_local_runtime_for_model(config, model)
        if runtime == "llama_cpp":
            settings = core._llama_cpp_settings(config, model=model, is_windows=core._IS_WINDOWS)
            args, cwd = core._llama_cpp_launch_plan(config, model=model, is_windows=core._IS_WINDOWS)
            base_url = core._llama_cpp_base_url(config, model=model, is_windows=core._IS_WINDOWS)
            alias = str(settings.get("model_alias") or model)
        elif runtime == "freetoken":
            settings = core._freetoken_settings(config, model=model, is_windows=core._IS_WINDOWS)
            args, cwd = core._freetoken_launch_plan(config, model=model, is_windows=core._IS_WINDOWS)
            base_url = core._freetoken_base_url(config, model=model, is_windows=core._IS_WINDOWS)
            alias = str(settings.get("model_alias") or model)
        else:
            settings = core.strata_settings(config, model=model)
            # The legacy planner can relocate copied artifacts. Preflight and
            # sharing checks must not rewrite a live worker's config: require
            # that storage migration has already completed through preparation.
            from src.services.strata_runtime import relocate_strata_artifact_paths
            relocate_strata_artifact_paths(settings, apply=False)
            args, cwd = core.strata_launch_plan(config, model=model)
            base_url = core.strata_base_url(config, model=model)
            alias = model
        parsed = urlsplit(base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
            raise ValueError("Shared managed runtimes must use loopback endpoints")
        if settings.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Shared managed workers must bind loopback")
        artifacts = []
        candidates = {str(arg) for arg in args}
        for key, value in settings.items():
            if isinstance(value, (str, Path)) and ("path" in key or key in {"executable", "model_dir", "source_root", "gguf_dir"}):
                candidates.add(str(value))
        if runtime == "strata":
            from src.services.strata_runtime import _strata_config_path_values
            config_path = Path(settings["config_path"])
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            for _role, _container, _key, value in _strata_config_path_values(payload, use_gguf_root=bool(settings.get("gguf_dir"))):
                path = Path(value)
                candidates.add(str(path if path.is_absolute() else Path(payload.get("cwd") or cwd) / path))
        for value in sorted(candidates):
            path = Path(value).expanduser()
            try:
                if path.is_file():
                    artifacts.append(_artifact_identity(path))
                    if path.suffix.lower() == ".gguf":
                        # llama.cpp split naming identifies only this model's shards.
                        match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", path.name)
                        if match:
                            artifacts.extend(_artifact_identity(path.with_name(
                                f"{match[1]}-{index:05d}-of-{match[3]}.gguf"))
                                for index in range(1, int(match[3]) + 1))
                elif path.is_dir() and (value == str(settings.get("model_path")) or runtime == "strata" and value not in {str(settings.get("source_root")), str(settings.get("gguf_dir")), str(settings.get("data_dir"))}):
                    artifacts.extend(_artifact_identity(p) for p in sorted(path.rglob("*")) if p.is_file())
            except OSError as exc:
                raise ValueError("Cannot establish model artifact identity") from exc
        relevant = {key: value for key, value in settings.items()
                    if key in {"context_size", "gpu_layers", "extra_args", "reasoning_effort", "vision",
                               "prefill", "vram_reserve_mib", "expert_cache", "runtime_profile"}
                    or key.startswith("mtp_")}
        fingerprint = digest({"settings": relevant, "runtime": runtime, "profile": model, "argv": args, "cwd": str(cwd),
                              "artifacts": artifacts})
        return {"profile_id": model, "base_url": base_url, "model": alias,
                "runtime": runtime, "launch_fingerprint": fingerprint, "config": config,
                "executable": str(args[0])}

    def ensure(self, target, *, force_restart=False, wait_for_model_ready=True, cancel_event=None):
        core = self.core
        host, port = core._base_url_host_port(target["base_url"])
        # Never adopt an existing same-alias listener, including after broker crash.
        owned = core._owned_local_process_at(target["base_url"], runtime=target["runtime"])
        if core._is_port_open(host, port, timeout_seconds=0.2) and not owned:
            raise RuntimeError("The runtime port belongs to an untracked process; no process was adopted or stopped")
        return core.ensure_openai_compatible_local_server(
            target["config"], model=target["profile_id"], raise_on_launch_error=True,
            force_restart=force_restart, wait_for_model_ready=wait_for_model_ready,
            **({"cancel_event": cancel_event} if cancel_event is not None else {}))

    def acquire(self, target, kind, *, cancel_event=None):
        if kind == "generation":
            tickets = []
            ensure_kwargs = {
                "model": target["profile_id"],
                "raise_on_launch_error": True,
                "acquire_generation_lease": True,
                "lease_tickets": tickets,
            }
            if cancel_event is not None:
                ensure_kwargs["cancel_event"] = cancel_event
            # Strata's consumer lease is intentionally allowed to remain while
            # the strict generation phase reaches its own /load gate.  Passing
            # this only for Strata avoids changing the stock llama.cpp/LFM
            # broker contract, where the inner ensure remains strict.
            if target.get("runtime") == "strata":
                ensure_kwargs["wait_for_model_ready"] = False
            self.core.ensure_openai_compatible_local_server(
                target["config"], **ensure_kwargs)
            if not tickets:
                raise RuntimeError("Generation readiness did not return an owned lease")
            return tickets[0]
        return self.core._acquire_llama_cpp_runtime_consumer_lease(target["base_url"], target["model"], consumer="shared-client")

    def stop(self, target):
        return self.core.stop_owned_openai_compatible_local_servers_respecting_generation_leases(
            runtime=target["runtime"], model=target["model"], endpoint=target["base_url"],
            timeout_seconds=0, defer_on_timeout=True)

    def status(self, target):
        for proc in self.core._owned_processes_for_scope(endpoint=target["base_url"], runtime=target["runtime"], model=target["model"]):
            if proc.poll() is None:
                return {"pid": proc.pid, "executable": target["executable"], "running": True}
        return {"pid": None, "running": False}
