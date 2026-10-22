"""Create/verify the isolated embedding runtime without changing host/TTS deps."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / "cache/embeddinggemma2/runtime/venv"
REQUIREMENTS = ROOT / "src/rag/embedding-worker-requirements.txt"


def checked(command):
    subprocess.run([str(x) for x in command], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument(
        "--cuda",
        action="store_true",
        help="Use the official PyTorch CUDA12.8 wheel index",
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--host-check",
        action="store_true",
        help="Also report pip check of the invoking host environment",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="Existing shared model storage (never a checkout-local model root)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Explicitly download the fixed official revision into --cache-dir",
    )
    parser.add_argument(
        "--load-check",
        action="store_true",
        help="Load the existing fixed model and make one query",
    )
    args = parser.parse_args()
    if not args.cuda and shutil.which("nvidia-smi"):
        args.cuda = (
            subprocess.run(
                ["nvidia-smi", "-L"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    runtime = args.runtime.resolve()
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not args.verify_only:
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(runtime)
        checked([python, "-m", "pip", "install", "--upgrade", "pip"])
        index = (
            "https://download.pytorch.org/whl/cu128"
            if args.cuda
            else "https://download.pytorch.org/whl/cpu"
        )
        checked(
            [
                python,
                "-m",
                "pip",
                "install",
                "torch==2.10.0",
                "torchvision==0.25.0",
                "torchaudio==2.10.0",
                "--index-url",
                index,
            ]
        )
        checked([python, "-m", "pip", "install", "-r", REQUIREMENTS])
    checked([python, "-m", "pip", "check"])
    checked(
        [
            python,
            "-c",
            "from sentence_transformers import SentenceTransformer; from transformers import EmbeddingGemma2Processor; import torchvision; print('EmbeddingGemma 2 worker dependencies: OK')",
        ]
    )
    if args.host_check:
        checked([sys.executable, "-m", "pip", "check"])
    if args.download:
        if args.cache_dir is None:
            parser.error(
                "--download requires an explicitly selected external --cache-dir"
            )
        cache = args.cache_dir.resolve()
        if cache.is_relative_to(ROOT):
            parser.error(
                "Model weights must use shared external storage, outside the checkout"
            )
        # This subprocess uses the authorized existing HF login implicitly; it
        # never reads/prints credentials, accepts terms or requests a grant.
        checked(
            [
                python,
                "-c",
                "from huggingface_hub import snapshot_download; import sys; snapshot_download('google/embeddinggemma-2', revision='914f7f89142e33e77833254d9c9b90c3cef7303b', cache_dir=sys.argv[1])",
                cache,
            ]
        )
    if args.load_check:
        sys.path.insert(0, str(ROOT))
        from src.rag.embedding_identity import EMBEDDING_FINGERPRINT

        requests = [
            {
                "request_id": "load-check",
                "op": "initialize",
                "identity": EMBEDDING_FINGERPRINT,
                "settings": {
                    "device": "cuda" if args.cuda else "cpu",
                    "cache_dir": str(args.cache_dir) if args.cache_dir else None,
                    "local_files_only": True,
                },
            },
            {
                "request_id": "query-check",
                "op": "encode",
                "identity": EMBEDDING_FINGERPRINT,
                "mode": "SearchQuery",
                "inputs": ["日本語の検索を確認する"],
                "batch_size": 1,
            },
        ]
        env = {
            k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}
        }
        result = subprocess.run(
            [str(python), "-u", str(ROOT / "src/rag/embedding_worker.py")],
            input="".join(json.dumps(r) + "\n" for r in requests),
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        if (
            result.returncode
            or len(responses) != 2
            or not all(r.get("ok") for r in responses)
        ):
            raise RuntimeError(
                "Worker load/query check failed: " + json.dumps(responses)
            )
        print(
            json.dumps(
                {
                    "worker_python": str(python),
                    "identity": EMBEDDING_FINGERPRINT,
                    "dimension": len(responses[1]["vectors"][0]),
                    "metrics": responses[1]["metrics"],
                }
            )
        )
    print(f"Worker interpreter: {python}")


if __name__ == "__main__":
    main()
