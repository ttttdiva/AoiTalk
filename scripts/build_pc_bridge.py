"""Build a standalone Windows executable with its Edge extension embedded."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--onedir", action="store_true")
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--name", default="AoiTalk-PC-Bridge")
    parser.add_argument("--dist-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--install-path", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", args.name):
        raise SystemExit("Executable name must match [A-Za-z][A-Za-z0-9_.-]{0,95}")

    default_work = ROOT / ".local" / "portable-bridge" / "pyinstaller"
    dist_root = (args.dist_root or (default_work / ("dist" if args.onedir else "onefile"))).resolve()
    work_root = (args.work_root or (default_work / "build")).resolve()
    spec_root = work_root / "spec"
    dist_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    spec_root.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", args.name, "--console",
        "--onedir" if args.onedir else "--onefile",
        "--distpath", str(dist_root),
        "--workpath", str(work_root),
        "--specpath", str(spec_root),
        "--paths", str(ROOT),
        "--collect-submodules", "pywinauto",
        "--hidden-import", "comtypes",
        "--hidden-import", "pythoncom",
        "--add-data", str(ROOT / "resources/edge-browser-extension") + ";resources/edge-browser-extension",
        str(ROOT / "scripts/pc_bridge_main.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    exe = dist_root / args.name / f"{args.name}.exe" if args.onedir else dist_root / f"{args.name}.exe"
    subprocess.run([str(exe), "--self-test"], cwd=work_root, check=True)

    if not args.onedir and not args.no_install:
        install_path = (args.install_path or (ROOT / exe.name)).resolve()
        install_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(exe, install_path)
        except PermissionError:
            raise SystemExit(
                "BridgeとEdge拡張の接続を終了してから、ビルド済みexeを配置してください: " + str(exe)
            )
        exe = install_path
    print("EXE=" + str(exe), flush=True)


if __name__ == "__main__":
    main()
