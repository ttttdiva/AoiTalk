"""LLM モード/エンジン切替・モデルカタログ・Ollama モデル管理ルート (server.py から移設)"""

import asyncio
import json
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ...services.llm_model_catalog import (
    build_engine_options as build_llm_engine_options,
    build_llm_mode_state,
    build_model_catalog as build_llm_model_catalog,
    load_model_catalog_cache,
    save_model_catalog_cache,
    update_model_catalog_cache,
)
from ..router_helpers import cookie_auth_dependency
from .payloads import OllamaModelPayload, OllamaPullPayload
from ...memory.database import get_database_manager
from ...memory.models import (
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise ValueError("empty LLM response")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", value)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def _normalize_docs_ai_result(command: str, parsed: dict[str, Any]) -> dict[str, Any]:
    mode = str(parsed.get("mode") or "").strip()
    if command == "rewrite":
        replacement = str(parsed.get("replacement") or parsed.get("title") or "").strip()
        return {
            "mode": "replace_title",
            "replacement": replacement[:500],
            "summary": str(parsed.get("summary") or "AI rewrite proposal").strip()[:500],
        }
    if command == "fill_fields":
        fields = parsed.get("fields")
        normalized_fields = []
        if isinstance(fields, list):
            for item in fields[:20]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("field") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    normalized_fields.append({"name": name[:120], "value": value[:1000]})
        return {
            "mode": "field_suggestions",
            "fields": normalized_fields,
            "summary": str(parsed.get("summary") or "AI field suggestions").strip()[:500],
        }
    lines = parsed.get("lines")
    if not isinstance(lines, list):
        lines = parsed.get("children")
    normalized_lines = [
        str(item).strip()[:500]
        for item in (lines if isinstance(lines, list) else [])
        if str(item).strip()
    ][:12]
    if command == "extract_tasks":
        normalized_lines = [
            line if "#Task" in line or "#タスク" in line else f"{line} #Task"
            for line in normalized_lines
        ]
    return {
        "mode": mode if mode == "insert_children" else "insert_children",
        "lines": normalized_lines,
        "summary": str(parsed.get("summary") or "AI child node proposal").strip()[:500],
    }


async def _run_docs_ai_completion(llm_client: Any, prompt: str) -> str:
    async_generate = getattr(llm_client, "generate_response_async", None)
    if callable(async_generate):
        return str(await async_generate(prompt))
    async_generic = getattr(llm_client, "generate_async", None)
    if callable(async_generic):
        return str(await async_generic(prompt))
    sync_generate = getattr(llm_client, "generate_response", None)
    if callable(sync_generate):
        return str(await asyncio.to_thread(lambda: sync_generate(prompt, stream=False)))
    sync_generic = getattr(llm_client, "generate", None)
    if callable(sync_generic):
        return str(await asyncio.to_thread(lambda: sync_generic(prompt)))
    raise RuntimeError("Configured LLM client does not support text generation")


async def _build_docs_ai_context(node_id: str | None) -> str:
    if not node_id:
        return ""
    try:
        parsed_id = uuid.UUID(str(node_id))
    except ValueError:
        return ""
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        node = await session.get(KnowledgeNode, parsed_id)
        if node is None:
            return ""
        parent = await session.get(KnowledgeNode, node.parent_id) if node.parent_id else None
        siblings_result = await session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == node.workspace_id,
                KnowledgeNode.parent_id == node.parent_id,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
            .limit(16)
        )
        siblings = list(siblings_result.scalars().all())
        tags_result = await session.execute(
            select(KnowledgeSupertag)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id == node.id)
            .order_by(KnowledgeSupertag.name)
        )
        tags = list(tags_result.scalars().all())
        fields_result = await session.execute(
            select(KnowledgeField, KnowledgeFieldValue)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeField.supertag_id)
            .outerjoin(
                KnowledgeFieldValue,
                (KnowledgeFieldValue.node_id == node.id)
                & (KnowledgeFieldValue.field_id == KnowledgeField.id),
            )
            .where(KnowledgeNodeSupertag.node_id == node.id)
            .order_by(KnowledgeField.sort_order, KnowledgeField.name)
        )
        field_lines = []
        for field, value in fields_result.all():
            raw_value = ""
            if value is not None:
                raw_value = (
                    value.value_text
                    or (str(value.value_number) if value.value_number is not None else "")
                    or (value.value_datetime.isoformat() if value.value_datetime else "")
                    or (str(value.target_node_id) if value.target_node_id else "")
                    or (json.dumps(value.value_json, ensure_ascii=False) if value.value_json else "")
                )
            field_lines.append(f"- {field.name} ({field.system_key or field.field_type}): {raw_value or '(empty)'}")
        instructions = "\n".join(
            str(tag.ai_instructions or "").strip()
            for tag in tags
            if str(tag.ai_instructions or "").strip()
        )
        return "\n".join(
            [
                "Docs context:",
                f"Current node: {node.title or ''}",
                f"Body: {(node.body_text or '')[:1000]}",
                f"Parent: {parent.title if parent else '(root)'}",
                "Sibling outline:",
                *[f"- {item.title or ''}" for item in siblings],
                "Tags: " + ", ".join(f"#{tag.name}" for tag in tags),
                "Fields:",
                *field_lines,
                "AI instructions:",
                instructions or "(none)",
            ]
        )
    finally:
        await session.close()


def _should_stop_previous_openai_compatible_local_server(
    previous_provider: str,
    previous_model: str,
    next_provider: str,
    next_model: str,
) -> bool:
    previous_provider_id = str(previous_provider or "").strip().lower()
    next_provider_id = str(next_provider or "").strip().lower()
    if previous_provider_id != "openai_compatible_local":
        return False
    if next_provider_id != "openai_compatible_local":
        return True
    return str(previous_model or "").strip() != str(next_model or "").strip()


def register_llm_routes(app: FastAPI, server: "WebChatServer") -> None:
    """LLM mode / models / engine / Ollama 管理ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    # ── LLM Mode API Endpoints ──────────────────────────────────────────
    @app.get("/api/llm/mode")
    async def get_llm_mode():
        """Get current LLM response mode or reasoning effort."""
        return JSONResponse(
            build_llm_mode_state(server.config, client=server._llm_client)
        )

    @app.post("/api/ai/docs/command")
    async def run_docs_ai_command(request: Request, _: None = Depends(require_auth)):
        """Run Docs AI commands through the configured LLM client."""
        body = await request.json()
        command = str(body.get("command") or "continue").strip()[:80]
        prompt = str(body.get("prompt") or "").strip()[:8000]
        node_id = str(body.get("node_id") or "").strip()[:80] or None

        llm_client = server._llm_client
        if llm_client is None:
            raise HTTPException(status_code=503, detail="LLM client is not configured")

        mode_instruction = {
            "continue": "Return child outline lines that naturally continue the selected Docs node.",
            "extract_tasks": "Extract actionable task lines. Each line must be a task title.",
            "rewrite": "Rewrite the selected node title. Return replacement only in JSON.",
            "fill_fields": "Suggest field values for empty Docs fields.",
        }.get(command, "Return child outline lines that naturally continue the selected Docs node.")
        json_shape = (
            '{"mode":"replace_title","replacement":"...","summary":"..."}'
            if command == "rewrite"
            else '{"mode":"field_suggestions","fields":[{"name":"...","value":"..."}],"summary":"..."}'
            if command == "fill_fields"
            else '{"mode":"insert_children","lines":["..."],"summary":"..."}'
        )
        docs_context = await _build_docs_ai_context(node_id)
        llm_prompt = (
            "You are AoiTalk Docs AI. Respond with strict JSON only, no markdown.\n"
            f"Command: {command}\n"
            f"Node ID: {node_id or ''}\n"
            f"Instruction: {mode_instruction}\n"
            f"Required JSON shape: {json_shape}\n"
            "Write concise Japanese content unless the user prompt clearly uses another language.\n"
            "User/selection context:\n"
            f"{prompt or '(empty)'}\n\n"
            f"{docs_context}"
        )
        try:
            raw = await _run_docs_ai_completion(llm_client, llm_prompt)
            parsed = _extract_json_object(raw)
            result = _normalize_docs_ai_result(command, parsed)
        except Exception as exc:
            logger.exception("Docs AI command failed")
            raise HTTPException(status_code=502, detail="Docs AI generation failed", headers={"x-error": str(exc)})
        return JSONResponse({"result": result, "confidence": 0.78})

    @app.post("/api/llm/mode")
    async def set_llm_mode(request: Request, _: None = Depends(require_auth)):
        """Set the current LLM response mode or reasoning effort."""
        try:
            body = await request.json()
            mode = str(body.get("mode", "")).strip()
            state = build_llm_mode_state(server.config, client=server._llm_client)
            available_modes = state.get("available_modes") or []

            if mode not in available_modes:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid mode. Use one of: {', '.join(available_modes)}",
                )

            def _apply_config(key: str, next_value: Any) -> None:
                if hasattr(server.config, "save_to_file"):
                    if not server.config.save_to_file(key, next_value):
                        raise RuntimeError(f"Failed to persist {key}")
                else:
                    server.config.set(key, next_value)

            provider = str(state.get("provider") or "").strip()
            kind = str(state.get("kind") or "response_mode")
            should_recreate_client = False

            if kind == "reasoning_effort":
                if provider == "codex-cli":
                    _apply_config("codex_cli.reasoning_effort", mode)
                    should_recreate_client = True
                elif provider == "claude-cli":
                    _apply_config("claude_cli.reasoning_effort", mode)
                    should_recreate_client = True
                elif provider == "openai":
                    _apply_config("openai.reasoning_effort", mode)
                    should_recreate_client = True
                elif provider == "kimi" and mode == "max":
                    _apply_config("kimi.reasoning_effort", "max")
                    should_recreate_client = True
                server._current_llm_mode = mode
            elif server._llm_client and hasattr(server._llm_client, "set_llm_mode"):
                server._llm_client.set_llm_mode(mode)
                logger.info(f"LLM mode set to: {mode}")
                server._current_llm_mode = mode

            server.config.set("llm_runtime_mode", mode)

            if should_recreate_client:
                from ...llm.manager import create_llm_client

                server.set_llm_client(create_llm_client(server.config))

            next_state = build_llm_mode_state(server.config, client=server._llm_client)

            # Broadcast mode change to all WebSocket clients
            await server.manager.broadcast(
                {"type": "llm_mode_change", "data": next_state}
            )

            return JSONResponse(
                {
                    "success": True,
                    **next_state,
                    "message": f"LLM設定を {mode} に切り替えました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to set LLM mode: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def _build_model_catalog(
        cfg,
        include_remote: bool = False,
        refresh_provider: Optional[str] = None,
        cached_catalog: Optional[Dict[str, Any]] = None,
    ):
        return build_llm_model_catalog(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
            include_remote=include_remote,
            refresh_provider=refresh_provider,
            cached_catalog=cached_catalog,
        )

    def _build_engine_list(cfg):
        """APIキーの有無でフィルタリングした利用可能エンジン一覧を返す"""
        return build_llm_engine_options(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
        )

    @app.get("/api/llm/models")
    async def get_llm_models(
        refresh: bool = Query(False),
        provider: Optional[str] = Query(None),
        _: None = Depends(require_auth),
    ):
        """Return provider-grouped model options for the settings screen."""
        cached_catalog = load_model_catalog_cache()
        catalog = _build_model_catalog(
            server.config,
            include_remote=refresh,
            refresh_provider=provider,
            cached_catalog=cached_catalog,
        )
        if refresh and provider:
            refreshed_provider = next(
                (item for item in catalog["providers"] if item["id"] == provider),
                None,
            )
            if refreshed_provider:
                next_cache = update_model_catalog_cache(
                    cached_catalog,
                    provider,
                    refreshed_provider.get("models") or [],
                )
                if next_cache != cached_catalog:
                    try:
                        save_model_catalog_cache(next_cache)
                        catalog = _build_model_catalog(
                            server.config,
                            include_remote=False,
                            refresh_provider=None,
                            cached_catalog=next_cache,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to save LLM model catalog cache: %s",
                            exc,
                        )
        return JSONResponse(catalog)

    @app.get("/api/llm/engine")
    async def get_llm_engine():
        """現在のLLMエンジン情報と利用可能エンジン一覧を返す"""
        provider = server.config.get("llm_provider", "openai")
        model = server.config.get("llm_model", "gpt-4o")
        return JSONResponse(
            {
                "provider": provider,
                "model": model,
                "available": _build_engine_list(server.config),
            }
        )

    @app.post("/api/llm/engine")
    async def set_llm_engine(request: Request, _: None = Depends(require_auth)):
        """LLMエンジンをホットスイッチする（再起動不要）"""
        try:
            body = await request.json()
            provider = str(body.get("provider", "")).strip()
            model = str(body.get("model", "")).strip()
            base_url = body.get("base_url")
            if not provider or not model:
                raise HTTPException(
                    status_code=400, detail="provider と model は必須です"
                )
            if provider == "kimi":
                supplied_key = str(body.get("api_key") or "").strip()
                configured_key = str(server.config.get("kimi_api_key", "") or "").strip()
                if not supplied_key and not configured_key and not os.getenv("MOONSHOT_API_KEY"):
                    raise HTTPException(
                        status_code=400,
                        detail="Kimi APIキーを設定してください",
                    )
            if provider == "routing-profile" and model != "free-team":
                raise HTTPException(
                    status_code=400,
                    detail="未対応のルーティングプロファイルです",
                )
            if (
                provider == "routing-profile"
                and model == "free-team"
                and server.config.get("routing_profiles.free-team.enabled", True)
                is False
            ):
                raise HTTPException(
                    status_code=409,
                    detail="無料Teamを有効にしてから選択してください",
                )

            previous_provider = str(server.config.get("llm_provider", "") or "")
            previous_model = str(server.config.get("llm_model", "") or "")
            should_stop_previous_local_server = (
                _should_stop_previous_openai_compatible_local_server(
                    previous_provider,
                    previous_model,
                    provider,
                    model,
                )
            )
            stopped_local_servers = 0

            if provider == "openai_compatible_local":
                from src.service_manager import (
                    validate_openai_compatible_local_launch_selection,
                )

                try:
                    validate_openai_compatible_local_launch_selection(
                        server.config,
                        provider=provider,
                        model=model,
                        base_url=base_url if isinstance(base_url, str) else None,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=str(exc))

            def _apply_config(key: str, next_value: Any) -> None:
                if hasattr(server.config, "save_to_file"):
                    if not server.config.save_to_file(key, next_value):
                        raise RuntimeError(f"Failed to persist {key}")
                else:
                    server.config.set(key, next_value)

            # configを更新して永続化する
            _apply_config("llm_provider", provider)
            _apply_config("llm_model", model)
            _apply_config(
                "llm_selection_kind",
                "routing_profile" if provider == "routing-profile" else "static",
            )
            _apply_config(
                "routing_profile_id",
                "free-team" if provider == "routing-profile" else "",
            )
            if provider == "sglang":
                _apply_config("sglang.model", model)
            elif provider == "ollama":
                _apply_config("ollama.model", model)
            elif provider == "openai_compatible_local":
                _apply_config("openai_compatible_local.model", model)
            elif provider == "openrouter":
                _apply_config("openrouter.model", model)
            elif provider == "codex-cli":
                _apply_config("codex_cli.model", model)
            elif provider == "claude-cli":
                _apply_config("claude_cli.model", model)
            elif provider == "antigravity-cli":
                _apply_config("antigravity_cli.model", model)
            elif provider == "grok-cli":
                _apply_config("grok_cli.model", model)
            elif provider == "gemini":
                _apply_config("gemini.model", model)
            elif provider == "openai":
                _apply_config("openai.model", model)
            elif provider == "kimi":
                _apply_config("kimi.model", model)

            if isinstance(base_url, str) and base_url.strip():
                if provider in {
                    "ollama",
                    "openrouter",
                    "kimi",
                    "openai_compatible_local",
                }:
                    _apply_config(f"{provider}.base_url", base_url.strip())
                elif provider == "sglang":
                    _apply_config("sglang_base_url", base_url.strip())
            elif provider == "openai_compatible_local":
                from src.llm.openai_compatible_local_profiles import (
                    local_server_profile_for_model,
                )

                profile = local_server_profile_for_model(model)
                if profile:
                    _apply_config(
                        "openai_compatible_local.base_url",
                        profile["base_url"],
                    )

            api_key = body.get("api_key")
            if isinstance(api_key, str) and api_key.strip():
                if provider == "openrouter":
                    _apply_config("openrouter_api_key", api_key.strip())
                elif provider == "kimi":
                    _apply_config("kimi_api_key", api_key.strip())
                elif provider == "ollama":
                    _apply_config("ollama.api_key", api_key.strip())
                elif provider == "openai_compatible_local":
                    _apply_config(
                        "openai_compatible_local.api_key",
                        api_key.strip(),
                    )
                elif provider == "sglang":
                    _apply_config("sglang_api_key", api_key.strip())

            if "enable_tools" in body and provider in {
                "ollama",
                "openai_compatible_local",
            }:
                _apply_config(f"{provider}.enable_tools", bool(body["enable_tools"]))

            if (
                "enable_response_format" in body
                and provider == "openai_compatible_local"
            ):
                _apply_config(
                    "openai_compatible_local.enable_response_format",
                    bool(body["enable_response_format"]),
                )

            if "enable_extra_body" in body and provider == "openai_compatible_local":
                _apply_config(
                    "openai_compatible_local.enable_extra_body",
                    bool(body["enable_extra_body"]),
                )

            reasoning_effort = body.get("reasoning_effort")
            if isinstance(reasoning_effort, str):
                effort = reasoning_effort.strip()
                if effort and provider == "codex-cli":
                    _apply_config("codex_cli.reasoning_effort", effort)
                elif effort and provider == "claude-cli":
                    _apply_config("claude_cli.reasoning_effort", effort)
                elif effort and provider == "kimi" and effort == "max":
                    _apply_config("kimi.reasoning_effort", "max")

            if provider == "openai_compatible_local":
                from src.service_manager import (
                    ensure_openai_compatible_local_server,
                    stop_openai_compatible_local_servers,
                )

                if should_stop_previous_local_server:
                    stopped_local_servers = stop_openai_compatible_local_servers()
                    if stopped_local_servers:
                        logger.info(
                            "Stopped %s managed OpenAI-compatible local server "
                            "process(es) before local model switch",
                            stopped_local_servers,
                        )

                try:
                    ensure_openai_compatible_local_server(
                        server.config,
                        raise_on_launch_error=True,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=str(exc))

            # 新しいLLMクライアントを生成して差し替え
            from ...llm.manager import create_llm_client

            new_client = create_llm_client(server.config)

            if (
                should_stop_previous_local_server
                and provider != "openai_compatible_local"
            ):
                from src.service_manager import stop_openai_compatible_local_servers

                stopped_local_servers = stop_openai_compatible_local_servers()
                if stopped_local_servers:
                    logger.info(
                        "Stopped %s managed OpenAI-compatible local server "
                        "process(es) after switching away",
                        stopped_local_servers,
                    )

            next_mode_state = build_llm_mode_state(server.config, client=new_client)
            server._current_llm_mode = str(next_mode_state.get("mode") or "fast")
            server.config.set("llm_runtime_mode", server._current_llm_mode)
            if (
                next_mode_state.get("kind") == "response_mode"
                and hasattr(new_client, "set_llm_mode")
            ):
                new_client.set_llm_mode(server._current_llm_mode)

            server.set_llm_client(new_client)
            logger.info(f"LLM engine switched to {provider}/{model}")

            # 全WebSocketクライアントに通知
            await server.manager.broadcast(
                {
                    "type": "llm_engine_change",
                    "data": {"provider": provider, "model": model},
                }
            )
            await server.manager.broadcast(
                {
                    "type": "llm_mode_change",
                    "data": next_mode_state,
                }
            )

            opts = _build_engine_list(server.config)
            label = next(
                (
                    o["label"]
                    for o in opts
                    if o["provider"] == provider and o["model"] == model
                ),
                f"{model} ({provider})",
            )
            return JSONResponse(
                {
                    "success": True,
                    "provider": provider,
                    "model": model,
                    "message": f"言語モデルを {label} に切り替えました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to switch LLM engine: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/ollama/status")
    async def get_ollama_status(_: None = Depends(require_auth)):
        """Get local Ollama daemon status."""
        return JSONResponse(server._ollama_model_manager.status())

    @app.get("/api/ollama/models")
    async def get_ollama_models(_: None = Depends(require_auth)):
        """List installed Ollama models."""
        return JSONResponse(server._ollama_model_manager.list_models())

    @app.delete("/api/ollama/models")
    async def delete_ollama_model(
        payload: OllamaModelPayload, _: None = Depends(require_auth)
    ):
        """Delete an installed Ollama model."""
        try:
            result = server._ollama_model_manager.delete_model(payload.model)
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Failed to delete Ollama model: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/ollama/pulls")
    async def get_ollama_pulls(_: None = Depends(require_auth)):
        """List recent Ollama pull tasks."""
        return JSONResponse({"pulls": server._ollama_model_manager.list_pulls()})

    @app.post("/api/ollama/pull")
    async def start_ollama_pull(
        payload: OllamaPullPayload, _: None = Depends(require_auth)
    ):
        """Start downloading an Ollama model in the background."""
        try:
            task = server._ollama_model_manager.start_pull(payload.model)
            return JSONResponse(task)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Failed to start Ollama pull: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/ollama/pull/{task_id}")
    async def get_ollama_pull(task_id: str, _: None = Depends(require_auth)):
        """Get an Ollama pull task status."""
        task = server._ollama_model_manager.get_pull(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="pull task not found")
        return JSONResponse(task)
