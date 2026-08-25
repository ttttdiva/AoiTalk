"""Deterministic Windows PyTorch setup helpers.

The Windows installer must not use the currently installed ``torch`` build to
decide whether an NVIDIA adapter exists.  A stale CPU-only wheel would make
that check lie.  This module therefore separates three concerns:

* Windows-side NVIDIA adapter/driver detection (``nvidia-smi`` plus the
  Windows display-adapter inventory),
* the small, explicit PyTorch cu128 install command used before AoiTalk's
  normal extras, and
* a final verification after *all* pip operations have completed.

The public functions accept command/import hooks so the decision logic can be
tested without downloading wheels or requiring an NVIDIA machine.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence


PYTORCH_VERSION = "2.10.0"
TORCHAUDIO_VERSION = "2.10.0"
CUDA_VERSION_PREFIX = "12.8"
PYTORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
COMMAND_TIMEOUT_SECONDS = 15


class WindowsTorchSetupError(RuntimeError):
    """Base error for a setup decision or verification failure."""


class NvidiaDetectionError(WindowsTorchSetupError):
    """Raised when Windows hardware state cannot be determined safely."""


class NvidiaDriverError(WindowsTorchSetupError):
    """Raised when an NVIDIA adapter exists but its driver is unusable."""


class TorchVerificationError(WindowsTorchSetupError):
    """Raised when the installed torch runtime does not satisfy its contract."""


@dataclass(frozen=True)
class NvidiaEnvironment:
    """Result of Windows-side NVIDIA detection.

    ``hardware_present`` deliberately does not depend on torch.  A detected
    adapter with ``driver_usable=False`` is a hard setup error, not a CPU
    fallback.  For a machine without an NVIDIA adapter both fields are false.
    """

    system: str
    hardware_present: bool
    driver_usable: bool
    gpu_names: tuple[str, ...] = ()
    source: str = ""
    detail: str = ""

    @property
    def requires_cuda(self) -> bool:
        return self.hardware_present


@dataclass(frozen=True)
class TorchInstallPlan:
    """A side-effect-free description of the optional PyTorch install."""

    install_cuda: bool
    command: tuple[str, ...] = ()
    reason: str = ""
    index_url: str | None = None

    @property
    def packages(self) -> tuple[str, ...]:
        """Return explicitly selected packages, excluding pip options."""

        return (
            (f"torch=={PYTORCH_VERSION}", f"torchaudio=={TORCHAUDIO_VERSION}")
            if self.install_cuda
            else ()
        )


@dataclass(frozen=True)
class TorchRuntimeInfo:
    """Observable torch/torchaudio runtime values used by final verification."""

    torch_version: str
    torchaudio_version: str
    cuda_version: str | None
    cuda_available: bool
    cuda_device_count: int
    device_name: str | None

    @property
    def version(self) -> str:
        """Compatibility alias for callers that use ``version``."""

        return self.torch_version

    @property
    def is_cuda_build(self) -> bool:
        return bool(self.cuda_version)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ModuleImporter = Callable[[str], Any]
MetadataVersion = Callable[[str], str]


_NVIDIA_MARKERS = ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")


def _completed_output(result: subprocess.CompletedProcess[str]) -> str:
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    return "\n".join(part.strip() for part in (stdout, stderr) if part.strip())


def _lines(output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _looks_like_nvidia(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in _NVIDIA_MARKERS)


def _run_probe(
    command: Sequence[str],
    *,
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only probe with bounded output and no shell expansion."""

    try:
        return runner(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(command), 127, "", "command not found")
    except OSError as exc:
        return subprocess.CompletedProcess(list(command), 126, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(list(command), 124, "", f"timed out: {exc}")


def _inventory_probe(*, runner: CommandRunner) -> subprocess.CompletedProcess[str]:
    """Query Windows display adapters without consulting torch."""

    command = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Get-CimInstance Win32_VideoController -ErrorAction Stop | "
        "Select-Object -ExpandProperty Name",
    )
    return _run_probe(command, runner=runner)


def detect_nvidia_environment(
    *,
    runner: CommandRunner | None = None,
    system: str | None = None,
) -> NvidiaEnvironment:
    """Detect NVIDIA hardware independently from the installed torch wheel.

    ``nvidia-smi`` is preferred because a successful query proves that the
    driver is usable.  If it is unavailable or fails, the Windows inventory is
    consulted.  An NVIDIA adapter reported by the inventory while ``nvidia-smi``
    fails is classified as a driver failure and never silently treated as a
    CPU-only machine.  If both probes cannot run, the function raises rather
    than guessing.

    Non-Windows callers receive a no-op result; ``setup.bat`` never changes
    Linux/WSL dependency selection.
    """

    current_system = system or platform.system()
    if current_system.casefold() != "windows":
        return NvidiaEnvironment(
            system=current_system,
            hardware_present=False,
            driver_usable=False,
            source="non-windows",
            detail="Windows CUDA setup is not applicable",
        )

    command_runner = runner or subprocess.run
    smi = _run_probe(
        (
            "nvidia-smi",
            "--query-gpu=name",
            "--format=csv,noheader",
        ),
        runner=command_runner,
    )
    smi_lines = _lines(str(getattr(smi, "stdout", "") or ""))
    smi_names = tuple(name for name in smi_lines if _looks_like_nvidia(name))
    if getattr(smi, "returncode", 1) == 0 and smi_names:
        return NvidiaEnvironment(
            system=current_system,
            hardware_present=True,
            driver_usable=True,
            gpu_names=smi_names,
            source="nvidia-smi",
            detail="nvidia-smi returned a usable GPU query",
        )

    smi_detail = (
        _completed_output(smi) or f"exit code {getattr(smi, 'returncode', 'unknown')}"
    )
    inventory = _inventory_probe(runner=command_runner)
    inventory_lines = (
        _lines(str(getattr(inventory, "stdout", "") or ""))
        if getattr(inventory, "returncode", 1) == 0
        else ()
    )
    nvidia_names = tuple(name for name in inventory_lines if _looks_like_nvidia(name))

    if nvidia_names:
        return NvidiaEnvironment(
            system=current_system,
            hardware_present=True,
            driver_usable=False,
            gpu_names=nvidia_names,
            source="windows-inventory",
            detail=f"nvidia-smi failed ({smi_detail})",
        )

    if getattr(inventory, "returncode", 1) == 0:
        return NvidiaEnvironment(
            system=current_system,
            hardware_present=False,
            driver_usable=False,
            source="windows-inventory",
            detail="Windows inventory contains no NVIDIA display adapter",
        )

    inventory_detail = (
        _completed_output(inventory)
        or f"exit code {getattr(inventory, 'returncode', 'unknown')}"
    )
    raise NvidiaDetectionError(
        "NVIDIA hardware state could not be determined safely: "
        f"nvidia-smi: {smi_detail}; Windows display inventory: {inventory_detail}."
    )


def prepare_install_plan(
    environment: NvidiaEnvironment,
    *,
    python_executable: str | None = None,
) -> TorchInstallPlan:
    """Build the explicit, side-effect-free torch installation plan."""

    if environment.hardware_present and not environment.driver_usable:
        names = ", ".join(environment.gpu_names) or "NVIDIA adapter"
        raise NvidiaDriverError(
            f"{names} を検出しましたが NVIDIA ドライバーを利用できません。"
            f" ({environment.detail or 'nvidia-smi failed'})"
            " CPU-only PyTorchへ黙って切り替えず、ドライバーを修復してから再実行してください。"
        )

    if not environment.hardware_present:
        return TorchInstallPlan(
            install_cuda=False,
            reason="NVIDIA GPUがないため、通常のCPU対応依存解決を継続します。",
        )

    executable = python_executable or sys.executable
    command = (
        executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--index-url",
        PYTORCH_CUDA_INDEX,
        f"torch=={PYTORCH_VERSION}",
        f"torchaudio=={TORCHAUDIO_VERSION}",
    )
    return TorchInstallPlan(
        install_cuda=True,
        command=command,
        index_url=PYTORCH_CUDA_INDEX,
        reason=(
            "NVIDIA GPU/ドライバーを確認したため、公式 cu128 indexから "
            f"torch=={PYTORCH_VERSION} と torchaudio=={TORCHAUDIO_VERSION} を導入します。"
        ),
    )


def execute_install_plan(
    plan: TorchInstallPlan,
    *,
    runner: CommandRunner | None = None,
) -> None:
    """Execute only the explicit command produced by ``prepare_install_plan``."""

    if not plan.command:
        return
    command_runner = runner or subprocess.run
    result = command_runner(list(plan.command), check=False)
    if getattr(result, "returncode", 1) != 0:
        output = (
            _completed_output(result)
            or f"exit code {getattr(result, 'returncode', 'unknown')}"
        )
        raise WindowsTorchSetupError(f"PyTorch cu128 の導入に失敗しました: {output}")


def _normalise_version(value: Any) -> str:
    return str(value or "").split("+", 1)[0].strip()


def _module_version(
    module: Any,
    package_name: str,
    *,
    metadata_version: MetadataVersion,
) -> str:
    value = getattr(module, "__version__", None)
    if value:
        return str(value)
    try:
        return str(metadata_version(package_name))
    except importlib.metadata.PackageNotFoundError as exc:
        raise TorchVerificationError(
            f"{package_name} のバージョン情報を取得できません"
        ) from exc


def _torch_cuda_details(torch_module: Any) -> tuple[str | None, bool, int, str | None]:
    version_module = getattr(torch_module, "version", None)
    cuda_version = (
        getattr(version_module, "cuda", None) if version_module is not None else None
    )
    cuda = getattr(torch_module, "cuda", None)
    if cuda is None:
        return (str(cuda_version) if cuda_version else None, False, 0, None)
    try:
        available = bool(cuda.is_available())
    except (
        Exception
    ) as exc:  # pragma: no cover - exact torch exception varies by driver
        raise TorchVerificationError(
            f"torch.cuda.is_available() の確認に失敗しました: {exc}"
        ) from exc
    try:
        count = int(cuda.device_count())
    except (
        Exception
    ) as exc:  # pragma: no cover - exact torch exception varies by driver
        raise TorchVerificationError(
            f"torch.cuda.device_count() の確認に失敗しました: {exc}"
        ) from exc
    name: str | None = None
    if count > 0:
        try:
            name = str(cuda.get_device_name(0))
        except (
            Exception
        ) as exc:  # pragma: no cover - exact torch exception varies by driver
            raise TorchVerificationError(
                f"CUDAデバイス名の取得に失敗しました: {exc}"
            ) from exc
    return (str(cuda_version) if cuda_version else None, available, count, name)


def verify_torch_runtime(
    environment: NvidiaEnvironment,
    *,
    import_module: ModuleImporter | None = None,
    metadata_version: MetadataVersion | None = None,
) -> TorchRuntimeInfo:
    """Verify the final torch state after all pip installs.

    NVIDIA Windows hosts require the exact 2.10.0 cu128-capable runtime and a
    live CUDA device.  A non-NVIDIA Windows host keeps the version selected by
    the normal resolver; its CPU-only runtime is import-checked but is not
    forced through the NVIDIA version contract.
    """

    if environment.hardware_present and not environment.driver_usable:
        names = ", ".join(environment.gpu_names) or "NVIDIA adapter"
        raise NvidiaDriverError(
            f"{names} は存在しますがドライバー検証に失敗しています。"
            " CPU-only PyTorchを正常状態として扱いません。"
        )

    importer = import_module or importlib.import_module
    metadata_lookup = metadata_version or importlib.metadata.version
    try:
        torch_module = importer("torch")
    except Exception as exc:
        raise TorchVerificationError(f"torch の import に失敗しました: {exc}") from exc
    try:
        torchaudio_module = importer("torchaudio")
    except Exception as exc:
        raise TorchVerificationError(
            f"torchaudio の import に失敗しました: {exc}"
        ) from exc

    torch_version = _module_version(
        torch_module, "torch", metadata_version=metadata_lookup
    )
    torchaudio_version = _module_version(
        torchaudio_module, "torchaudio", metadata_version=metadata_lookup
    )
    cuda_version, cuda_available, device_count, device_name = _torch_cuda_details(
        torch_module
    )
    info = TorchRuntimeInfo(
        torch_version=torch_version,
        torchaudio_version=torchaudio_version,
        cuda_version=cuda_version,
        cuda_available=cuda_available,
        cuda_device_count=device_count,
        device_name=device_name,
    )

    errors: list[str] = []
    if environment.hardware_present:
        if _normalise_version(torch_version) != PYTORCH_VERSION:
            errors.append(f"torch={torch_version} (要求 {PYTORCH_VERSION})")
        if _normalise_version(torchaudio_version) != TORCHAUDIO_VERSION:
            errors.append(
                f"torchaudio={torchaudio_version} (要求 {TORCHAUDIO_VERSION})"
            )
        if not cuda_version:
            errors.append("torch.version.cuda が空です（CPU-only build）")
        elif not cuda_version.startswith(CUDA_VERSION_PREFIX):
            errors.append(
                f"torch.version.cuda={cuda_version} (要求 cu128/CUDA {CUDA_VERSION_PREFIX})"
            )
        if not cuda_available:
            errors.append("torch.cuda.is_available()=False")
        if device_count < 1:
            errors.append(f"CUDA device_count={device_count}")
        if not device_name:
            errors.append("CUDA device nameを取得できません")

    if errors:
        raise TorchVerificationError(
            "最終PyTorch検証に失敗しました: "
            + "; ".join(errors)
            + f" [torch.version.cuda={cuda_version!r}, "
            f"torch.cuda.is_available()={cuda_available}, "
            f"device_count={device_count}, device_name={device_name!r}]"
        )
    return info


def _print_environment(environment: NvidiaEnvironment) -> None:
    if environment.hardware_present:
        names = ", ".join(environment.gpu_names) or "(名称不明)"
        print(
            f"NVIDIA GPU: {names} ({'driver OK' if environment.driver_usable else 'driver failure'})"
        )
    else:
        print(f"NVIDIA GPUなし: {environment.detail}")


def _command_install() -> int:
    environment = detect_nvidia_environment()
    _print_environment(environment)
    plan = prepare_install_plan(environment)
    print(plan.reason)
    execute_install_plan(plan)
    return 0


def _command_verify() -> int:
    environment = detect_nvidia_environment()
    _print_environment(environment)
    info = verify_torch_runtime(environment)
    print(
        "PyTorch検証OK: "
        f"torch={info.torch_version}, torchaudio={info.torchaudio_version}, "
        f"torch.version.cuda={info.cuda_version!r}, "
        f"torch.cuda.is_available()={info.cuda_available}, "
        f"device_count={info.cuda_device_count}, device={info.device_name or '(CPU)'}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="install",
        choices=("detect", "install", "verify"),
        help="detect hardware, install the optional cu128 wheel, or verify the final runtime",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            environment = detect_nvidia_environment()
            _print_environment(environment)
            return 0
        if args.command == "verify":
            return _command_verify()
        return _command_install()
    except WindowsTorchSetupError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
