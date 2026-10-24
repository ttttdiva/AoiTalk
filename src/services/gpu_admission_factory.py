"""Opt-in construction of the cooperative GPU trial runtime from real ports.

Nothing here is a stub: with the trial profile configured it builds the real ``psutil`` process
port, the ``nvidia-smi`` capacity port, the shared-broker port and the durable state store around
the accepted ``CooperativeGpuBackend``. Without configuration it returns ``None`` *and says why*,
which keeps the coordinator a pass-through watchdog (no lease can be granted, nothing is paused)
and is never reported as readiness. A configured-but-invalid setting also fails closed with its
own reason instead of guessing a default for a safety threshold.

Settings are deployment-level environment variables (the feature changes what the machine may
run next to what, so it is not an end-user setting):

* ``AOITALK_GPU_TRIAL_PROFILE`` - the managed local-runtime profile the trial launches (required to enable)
* ``AOITALK_GPU_TRIAL_GRANT_FREE_MIB`` - free GPU memory required before the trial may start (required)
* ``AOITALK_GPU_TRIAL_REOPEN_FREE_MIB`` - free GPU memory required before embedding is admitted again (required)
* ``AOITALK_GPU_TRIAL_STATE_PATH`` - absolute path of the durable trial state (default ``data/gpu_admission/``)
* ``AOITALK_GPU_TRIAL_GPU_INDICES`` - comma separated devices to watch (default: all, smallest free wins)
* ``AOITALK_GPU_TRIAL_NVIDIA_SMI`` - path of ``nvidia-smi`` (default: found on PATH)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..shared_local_runtime import registry
from ..shared_local_runtime.backend import is_managed
from .gpu_admission import Conflict
from .gpu_admission_driver import GpuAdmissionDriver
from .gpu_trial_adapter import CooperativeGpuBackend, FileTrialStateStore, RuntimeEmbeddingPort
from .gpu_trial_launcher import GpuTrialLauncher
from .gpu_trial_ports import ManagedBrokerPort, NvidiaSmiCapacityPort, PsutilProcessPort

log = logging.getLogger(__name__)

ENV_PROFILE = "AOITALK_GPU_TRIAL_PROFILE"
ENV_GRANT = "AOITALK_GPU_TRIAL_GRANT_FREE_MIB"
ENV_REOPEN = "AOITALK_GPU_TRIAL_REOPEN_FREE_MIB"
ENV_STATE = "AOITALK_GPU_TRIAL_STATE_PATH"
ENV_INDICES = "AOITALK_GPU_TRIAL_GPU_INDICES"
ENV_SMI = "AOITALK_GPU_TRIAL_NVIDIA_SMI"
NOT_CONFIGURED = "not_configured"
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "gpu_admission" / "trial-state.json"
MEBIBYTE = 1024 * 1024


class GpuTrialConfigError(ValueError):
    """A configured setting is unusable; the message is the short reason code."""


@dataclass
class GpuTrialRuntime:
    backend: CooperativeGpuBackend
    broker: ManagedBrokerPort
    profile: str
    client_factory: Callable
    snapshot_factory: Callable[[], str | None]
    api_key: Callable[[], str]

    def launcher(self, driver: GpuAdmissionDriver) -> GpuTrialLauncher:
        return GpuTrialLauncher(driver, self.broker, profile=self.profile, client_factory=self.client_factory,
                                snapshot_factory=self.snapshot_factory, api_key=self.api_key)


def _positive_mebibytes(environ: Mapping[str, str], name: str) -> int:
    raw = str(environ.get(name, "")).strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise GpuTrialConfigError(f"invalid_{name.lower()}")  # a safety threshold has no safe default
    return int(raw) * MEBIBYTE


def _state_path(environ: Mapping[str, str]) -> Path:
    raw = str(environ.get(ENV_STATE, "")).strip()
    if not raw:
        return DEFAULT_STATE_PATH
    path = Path(raw)
    if not path.is_absolute():
        raise GpuTrialConfigError(f"invalid_{ENV_STATE.lower()}")  # a cwd-relative file is lost on a restart elsewhere
    return path


def _indices(environ: Mapping[str, str]) -> tuple[int, ...] | None:
    raw = str(environ.get(ENV_INDICES, "")).strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if not all(part.isdigit() for part in parts):
        raise GpuTrialConfigError(f"invalid_{ENV_INDICES.lower()}")
    return tuple(int(part) for part in parts)


def _default_client():
    from ..service_manager import _shared_local_runtime

    return _shared_local_runtime.client()


def build_gpu_trial_runtime(config, environ: Mapping[str, str] | None = None, *,
                            client_factory: Callable | None = None,
                            ) -> tuple[GpuTrialRuntime | None, str | None]:
    """``(runtime, None)`` when enabled, else ``(None, reason)``. Never raises for a bad setting."""
    environ = os.environ if environ is None else environ
    profile = str(environ.get(ENV_PROFILE, "")).strip()
    if not profile:
        return None, NOT_CONFIGURED
    try:
        grant = _positive_mebibytes(environ, ENV_GRANT)
        reopen = _positive_mebibytes(environ, ENV_REOPEN)
        state_path = _state_path(environ)
        indices = _indices(environ)
    except GpuTrialConfigError as exc:
        log.warning("GPU trial backend disabled: %s", exc)
        return None, str(exc)
    client_factory = client_factory or _default_client
    smi = str(environ.get(ENV_SMI, "")).strip()

    def api_key() -> str:
        from .strata_runtime import strata_api_key

        return strata_api_key(config)

    def snapshot() -> str:
        from .. import service_manager as core

        if not is_managed(config, profile, core):
            raise Conflict("trial_profile_not_managed")  # an external endpoint is nobody's to launch or stop
        return registry.register(config, profile, None, core=core)

    # The broker port recognises only the launches this backend recorded: bound late, because the
    # backend is built after (and around) the port.
    recorded: list[Callable] = [lambda: ()]
    broker = ManagedBrokerPort(client_factory=client_factory, known_bindings=lambda: recorded[0](),
                               api_key=api_key)
    backend = CooperativeGpuBackend(
        profile=profile, fingerprint="", process=PsutilProcessPort(), broker=broker,
        capacity=NvidiaSmiCapacityPort(command=(smi,) if smi else ("nvidia-smi",), indices=indices),
        embedding=RuntimeEmbeddingPort(), store=FileTrialStateStore(state_path),
        grant_free_bytes=grant, reopen_free_bytes=reopen)
    recorded[0] = backend.recorded_bindings
    return GpuTrialRuntime(backend, broker, profile, client_factory, snapshot, api_key), None
