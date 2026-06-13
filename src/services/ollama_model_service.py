"""Ollama model management helpers."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return default


def normalize_ollama_base_url(base_url: Optional[str]) -> str:
    clean = (base_url or DEFAULT_OLLAMA_BASE_URL).strip().rstrip("/")
    if clean.endswith("/v1"):
        clean = clean[:-3].rstrip("/")
    return clean or DEFAULT_OLLAMA_BASE_URL


def get_ollama_base_url(config: Any = None) -> str:
    configured = (
        os.getenv("OLLAMA_BASE_URL")
        or _config_get(config, "ollama_base_url")
        or _config_get(config, "ollama.base_url")
        or DEFAULT_OLLAMA_BASE_URL
    )
    return normalize_ollama_base_url(str(configured))


class OllamaModelManager:
    """Track Ollama model pulls and query the local Ollama daemon."""

    def __init__(self, config: Any = None):
        self.config = config
        self.base_url = get_ollama_base_url(config)
        self._pulls: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def status(self, timeout: float = 1.0) -> Dict[str, Any]:
        try:
            response = requests.get(f"{self.base_url}/api/version", timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return {
                "available": True,
                "base_url": self.base_url,
                "version": data.get("version"),
            }
        except Exception as exc:
            return {
                "available": False,
                "base_url": self.base_url,
                "error": str(exc),
            }

    def list_models(self, timeout: float = 1.0) -> Dict[str, Any]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=timeout)
            response.raise_for_status()
            data = response.json()
            models = data.get("models", [])
            if isinstance(models, list):
                models = sorted(models, key=lambda item: str(item.get("name", "")))
            else:
                models = []
            return {
                "success": True,
                "base_url": self.base_url,
                "models": models,
                "status": {"available": True, "base_url": self.base_url},
            }
        except Exception as exc:
            return {
                "success": False,
                "base_url": self.base_url,
                "models": [],
                "status": {
                    "available": False,
                    "base_url": self.base_url,
                    "error": str(exc),
                },
                "error": str(exc),
            }

    def delete_model(self, model: str, timeout: float = 10.0) -> Dict[str, Any]:
        model = (model or "").strip()
        if not model:
            raise ValueError("model is required")

        response = requests.delete(
            f"{self.base_url}/api/delete",
            json={"model": model},
            timeout=timeout,
        )
        response.raise_for_status()
        return {
            "success": True,
            "base_url": self.base_url,
            "model": model,
        }

    def start_pull(self, model: str) -> Dict[str, Any]:
        model = (model or "").strip()
        if not model:
            raise ValueError("model is required")

        task_id = uuid.uuid4().hex
        now = time.time()
        state = {
            "task_id": task_id,
            "model": model,
            "status": "queued",
            "message": "queued",
            "completed": 0,
            "total": 0,
            "percent": 0,
            "done": False,
            "error": None,
            "started_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._pulls[task_id] = state

        thread = threading.Thread(
            target=self._pull_worker,
            args=(task_id, model),
            name=f"ollama-pull-{task_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.get_pull(task_id) or state

    def get_pull(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._pulls.get(task_id)
            return dict(state) if state else None

    def list_pulls(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(value) for value in self._pulls.values()),
                key=lambda item: float(item.get("started_at") or 0),
                reverse=True,
            )

    def _update_pull(self, task_id: str, **updates: Any) -> None:
        with self._lock:
            state = self._pulls.get(task_id)
            if not state:
                return
            state.update(updates)
            total = int(state.get("total") or 0)
            completed = int(state.get("completed") or 0)
            if total > 0 and completed >= 0:
                state["percent"] = max(0, min(100, int(completed * 100 / total)))
            state["updated_at"] = time.time()

    def _pull_worker(self, task_id: str, model: str) -> None:
        self._update_pull(task_id, status="pulling", message="pulling")
        try:
            response = requests.post(
                f"{self.base_url}/api/pull",
                json={"model": model, "stream": True},
                stream=True,
                timeout=(5, None),
            )
            response.raise_for_status()

            last_status = "pulling"
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))

                last_status = str(payload.get("status") or last_status)
                updates: Dict[str, Any] = {
                    "status": last_status,
                    "message": last_status,
                }
                if payload.get("digest"):
                    updates["digest"] = payload["digest"]
                if payload.get("total") is not None:
                    updates["total"] = int(payload.get("total") or 0)
                if payload.get("completed") is not None:
                    updates["completed"] = int(payload.get("completed") or 0)
                self._update_pull(task_id, **updates)

            self._update_pull(
                task_id,
                status="success",
                message="success",
                done=True,
                percent=100,
                error=None,
            )
        except Exception as exc:
            self._update_pull(
                task_id,
                status="error",
                message=str(exc),
                done=True,
                error=str(exc),
            )
