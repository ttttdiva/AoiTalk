"""AgentLLMClient の公開生成 API・ストリームイベント抽出・シーン画像生成 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import concurrent.futures
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, Generator, List, Optional, Union

from ..native_runtime import responses_output_text
from ..openrouter_provider_routing import merge_provider_options_into_extra_body
from ..conversation_context import normalize_usage, persist_usage_sync
from ...services.outbound_privacy_service import OutboundPrivacyGateway
from ...services.turn_context import get_turn_context

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[List[str]]]

_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_SEARCH_OUTPUT_LIMIT = 12000
_SEARCH_URL_LIMIT = 20


class GenerationApiMixin:
    def _privacy_gateway_for_generation(self) -> OutboundPrivacyGateway:
        """Return a gateway scoped to the active user/session identity."""

        turn = get_turn_context()
        user_id = str(
            getattr(self, "session_user_id", None)
            or getattr(turn, "user_id", None)
            or "default_user"
        ).strip()
        session_id = str(
            getattr(self, "current_session_id", None)
            or getattr(turn, "session_id", None)
            or ""
        ).strip()
        runner = getattr(self, "_turn_runner", None)
        gateway = getattr(runner, "privacy_gateway", None)
        if not isinstance(gateway, OutboundPrivacyGateway) or (
            gateway.user_id != user_id or gateway.session_id != session_id
        ):
            gateway = OutboundPrivacyGateway(
                getattr(self, "config", None),
                user_id=user_id,
                session_id=session_id,
                session_context=(getattr(self, "_privacy_session_context", None) or None),
                project_metadata=(getattr(self, "_privacy_project_metadata", None) or None),
            )
            if runner is not None:
                runner.privacy_gateway = gateway
        else:
            session_context = getattr(self, "_privacy_session_context", None)
            project_metadata = getattr(self, "_privacy_project_metadata", None)
            if session_context or project_metadata:
                gateway.update_policy_context(
                    session_context=session_context,
                    project_metadata=project_metadata,
                )
            else:
                gateway.update_policy_context()
        return gateway

    """chat/generate_* 系の公開エントリポイント、ストリーム抽出、シーン画像生成。"""

    def _record_generation_usage(
        self,
        response: Any,
        *,
        request_type: str,
        started_at: float | None = None,
        is_streaming: bool = False,
    ) -> dict[str, Any]:
        """Persist one successful ephemeral/provider API response immediately.

        Native chat turns are recorded by ``TurnExecutionMixin``.  This helper
        is intentionally limited to the direct Responses/Chat Completions
        calls in this mixin so those calls are accounted for exactly once.
        Providers may return the served model on the response envelope; pass it
        through ``normalize_usage`` so requested and resolved models remain
        distinguishable in ``TokenUsage``.
        """

        raw_usage = getattr(response, "usage", None)
        resolved_model = getattr(response, "model", None)
        if isinstance(response, dict):
            if raw_usage is None:
                raw_usage = response.get("usage")
            if resolved_model is None:
                resolved_model = response.get("model")
        usage = normalize_usage(
            raw_usage,
            provider=str(getattr(self, "provider_label", "") or ""),
            resolved_model=(str(resolved_model) if resolved_model else None),
        )
        # ``normalize_usage({})`` intentionally returns a shape containing
        # ``None`` fields for callers that compare key sets.  Do not turn that
        # shape into a fabricated zero-token TokenUsage row.
        if not usage or all(
            usage.get(key) is None for key in ("input_tokens", "output_tokens")
        ):
            return {}
        latency_ms = 0
        if started_at is not None:
            try:
                latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
            except Exception:
                latency_ms = 0
        try:
            persist_usage_sync(
                self,
                provider=str(getattr(self, "provider_label", "openai") or "openai"),
                model=str(getattr(self, "model_name", "") or ""),
                usage=usage,
                request_type=request_type,
                latency_ms=latency_ms,
                is_streaming=bool(is_streaming),
            )
        except Exception:
            # Usage persistence must never turn a successful internal request
            # into a failed title/memory/plain-text operation.
            logger.debug("generation usage persistence failed", exc_info=True)
        return usage

    def _merge_openrouter_usage_extra_body(
        self,
        extra_body: Any,
    ) -> dict[str, Any]:
        """Ensure OpenRouter returns usage/cost metadata for direct calls."""

        merged = dict(extra_body) if isinstance(extra_body, dict) else {}
        usage_option = merged.get("usage")
        usage_option = dict(usage_option) if isinstance(usage_option, dict) else {}
        # ``usage.include`` is required for OpenRouter to include accounting
        # details (including provider-reported cost) on plain completions.
        usage_option["include"] = True
        merged["usage"] = usage_option
        return merge_provider_options_into_extra_body(
            merged,
            getattr(self, "config", None),
            str(getattr(self, "model_name", "") or ""),
        )

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        prompt = "\n".join(
            f"{message.get('role', 'user')}: {message.get('content', '')}"
            for message in messages
        )
        return self.generate_response(prompt, stream=False)

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        result = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        yield result

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": self.model_name}]

    def health_check(self) -> Dict[str, Any]:
        return {"ok": True, "provider": self.provider_label, "model": self.model_name}

    def generate_response(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        image_data: dict = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> Union[str, Generator[str, None, None]]:
        """Generate response using the AoiTalk-native agent runtime

        Args:
            user_input: User's input text
            temperature: Sampling temperature.
            max_tokens: Maximum tokens.
            stream: Whether to stream.
            image_data: Optional image data.
            stream_callback: Async callback for streaming events

        Returns:
            Generated response
        """
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._run_async_safe,
                    user_input,
                    stream_callback,
                    None,
                    image_data,
                )
                response = future.result()

            print(f"[AgentLLMClient] 応答: {response}")

            if stream:

                def response_generator():
                    yield response

                return response_generator()
            return response

        except concurrent.futures.TimeoutError:
            print(f"[AgentLLMClient] タイムアウトエラー")
            personality = (
                self.config.get_character_config(self.character_name).get(
                    "personality", {}
                )
                if self.config
                else {}
            )
            return personality.get("fallbackReply", "エラーが発生しました")

        except Exception as e:
            print(f"[AgentLLMClient] エラー: {e}")
            import traceback

            traceback.print_exc()
            personality = (
                self.config.get_character_config(self.character_name).get(
                    "personality", {}
                )
                if self.config
                else {}
            )
            return personality.get("fallbackReply", "エラーが発生しました")

    def _extract_stream_tool_call_id(self, item: Any) -> str | None:
        raw_item = getattr(item, "raw_item", None)
        call_id = getattr(raw_item, "call_id", None)
        if call_id:
            return str(call_id)
        if isinstance(raw_item, dict) and raw_item.get("call_id"):
            return str(raw_item["call_id"])
        return None

    def _extract_stream_tool_output_call_id(
        self, item: Any
    ) -> str | None:
        raw_item = getattr(item, "raw_item", None)
        call_id = getattr(raw_item, "call_id", None)
        if call_id:
            return str(call_id)
        if isinstance(raw_item, dict) and raw_item.get("call_id"):
            return str(raw_item["call_id"])
        return None

    def _extract_stream_tool_name(self, item: Any) -> str:
        raw_item = getattr(item, "raw_item", None)
        for attr in ("name", "tool_name"):
            value = getattr(raw_item, attr, None)
            if value:
                return str(value)
        function = getattr(raw_item, "function", None)
        function_name = getattr(function, "name", None)
        if function_name:
            return str(function_name)
        if isinstance(raw_item, dict):
            for key in ("name", "tool_name"):
                value = raw_item.get(key)
                if value:
                    return str(value)
            function_data = raw_item.get("function")
            if isinstance(function_data, dict) and function_data.get("name"):
                return str(function_data["name"])
        return "tool"

    def _extract_stream_tool_arguments(
        self, item: Any
    ) -> dict[str, Any] | None:
        raw_item = getattr(item, "raw_item", None)
        arguments = getattr(raw_item, "arguments", None)
        if arguments is None and isinstance(raw_item, dict):
            arguments = raw_item.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            try:
                parsed = json.loads(arguments)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return {"raw": arguments}
        if isinstance(arguments, dict):
            return arguments
        return None

    def _stringify_tool_output(self, output: Any) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False, default=str)
        except Exception:
            return str(output)

    def _build_search_tool_result(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        output_text: str,
    ) -> dict[str, Any] | None:
        if "search" not in tool_name.lower() or not output_text.strip():
            return None

        urls: list[str] = []
        for match in _SEARCH_TOOL_URL_RE.finditer(output_text):
            url = match.group(0).rstrip(".,;:!?")
            if url not in urls:
                urls.append(url)
            if len(urls) >= _SEARCH_URL_LIMIT:
                break

        query = None
        if tool_args:
            for key in ("query", "q", "search_query", "keyword", "keywords"):
                value = tool_args.get(key)
                if isinstance(value, str) and value.strip():
                    query = value.strip()
                    break

        clipped_output = output_text.strip()
        truncated = len(clipped_output) > _SEARCH_OUTPUT_LIMIT
        if truncated:
            clipped_output = clipped_output[:_SEARCH_OUTPUT_LIMIT].rstrip() + "\n...(省略)"

        return {
            "tool": tool_name,
            "query": query,
            "urls": urls,
            "output": clipped_output,
            "truncated": truncated,
        }

    def _extract_scene_description(self, response: str) -> Optional[str]:
        """応答から画像生成用のシーン描写を抽出する。"""
        import re

        match = re.search(r"\[SCENE_DESCRIPTION:\s*(.+?)\]", response, re.DOTALL)
        if not match:
            return None

        scene_description = match.group(1).strip()
        return scene_description or None

    def _strip_scene_description_markers(self, response: str) -> str:
        """表示・保存する応答から画像生成マーカーを除去する。"""
        import re

        return re.sub(r"\n?\[SCENE_DESCRIPTION:\s*.+?\]\s*", "", response, flags=re.DOTALL).strip()

    async def _generate_scene_image_async(
        self,
        scene_description: str,
        *,
        message_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Roleplay シーン画像を生成し、永続 media と表示用タグを返す。"""
        try:
            from ...services.character_service import get_character_for_prompt
            from ...services.generated_media_service import (
                generate_roleplay_scene_media,
                should_attempt_roleplay_generation,
            )

            char_data = await get_character_for_prompt(self.character_name)
            session_id = str(getattr(self, "current_session_id", "") or "")
            owner_user_id = str(
                getattr(self, "session_user_id", None)
                or self._get_session_user_id()
                or ""
            )
            if not session_id or not message_id:
                return None

            if not await should_attempt_roleplay_generation(
                character_data=char_data,
                scene_description=scene_description,
                session_id=session_id,
                config=getattr(self, "config", None),
            ):
                return None

            appearance_tags = ""
            negative_tags = ""
            comfyui_overrides: Dict[str, Any] = {}
            character_type = ""
            if char_data:
                appearance_tags = char_data.get("appearance_tags", "")
                negative_tags = char_data.get("negative_tags", "")
                comfyui_overrides = char_data.get("comfyui_config", {}) or {}
                character_type = str(char_data.get("character_type") or "")

            from ...services.image_prompt_builder import build_image_prompt

            prompt, default_negative = await build_image_prompt(
                self.history_manager.get_all(),
                appearance_tags,
                scene_description,
                usage_context=self,
                roleplay_pov=character_type == "roleplay",
            )
            negative_parts = [p for p in [negative_tags, default_negative] if p]
            combined_negative = ", ".join(negative_parts)

            logger.info(
                "[AgentLLMClient] シーン画像生成開始: %s...",
                prompt[:80],
            )

            return await generate_roleplay_scene_media(
                owner_user_id=owner_user_id,
                session_id=session_id,
                message_id=message_id,
                scene_description=scene_description,
                positive_prompt=prompt,
                negative_prompt=combined_negative,
                comfyui_overrides=comfyui_overrides,
                engine="comfyui",
                config=getattr(self, "config", None),
            )
        except Exception as e:
            logger.error("[AgentLLMClient] シーン画像生成タスクエラー: %s", e)
            return None

    async def generate_memory_extraction_async(
        self,
        prompt: str,
        *,
        system_prompt: str,
    ) -> str:
        """Generate Dreaming extraction JSON without mutating chat history or using tools."""
        if getattr(self, "provider_label", "openai") == "openai":
            # 公式 OpenAI 経路は Responses API を使う（tools なしの単純呼び出し）。
            try:
                # temperature は reasoning 系モデルが拒否するため指定しない。
                # max_output_tokens は reasoning トークンも消費するため、
                # 小さい値で JSON が途中打ち切りになるのを避けて未指定とする。
                started_at = time.monotonic()
                request_kwargs = {
                    "model": self.model_name,
                    "instructions": system_prompt or "",
                    "input": prompt or "",
                    "store": False,
                }
                gateway = self._privacy_gateway_for_generation()
                protected = await gateway.protect(
                    request_kwargs,
                    provider=str(getattr(self, "provider_label", "openai") or "openai"),
                    base_url=str(getattr(self._openai_client, "base_url", "") or ""),
                    source_kind="memory_extraction",
                )
                response = await self._openai_client.responses.create(
                    **protected.payload
                )
            except Exception as first_error:
                print(f"[AgentLLMClient] Dreamingメモリ抽出に失敗: {first_error}")
                raise first_error
            self._record_generation_usage(
                response,
                request_type="memory_extraction",
                started_at=started_at,
            )
            return gateway.restore(responses_output_text(response))

        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": prompt or ""},
        ]
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        if getattr(self, "provider_label", "") == "deepseek":
            effort = str(
                self.config.get("deepseek.reasoning_effort", "high")
                if self.config is not None and hasattr(self.config, "get")
                else "high"
            ).strip().lower()
            if effort not in {"none", "high", "max"}:
                effort = "high"
            kwargs["extra_body"] = {
                "thinking": {
                    "type": "disabled" if effort == "none" else "enabled"
                }
            }
            if effort != "none":
                kwargs["reasoning_effort"] = effort
            kwargs["max_tokens"] = 1200
        elif getattr(self, "provider_label", "") == "deepinfra":
            effort = str(
                self.config.get("deepinfra.reasoning_effort", "high")
                if self.config is not None and hasattr(self.config, "get")
                else "high"
            ).strip().lower()
            if effort not in {"none", "low", "medium", "high"}:
                effort = "high"
            kwargs["extra_body"] = {"reasoning_effort": effort}
            kwargs["max_tokens"] = 1200
        elif getattr(self, "provider_label", "") == "kimi" and self.model_name == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
            kwargs["max_completion_tokens"] = 1200
        else:
            kwargs["temperature"] = 0.0
            kwargs["max_tokens"] = 1200
        if getattr(self, "provider_label", "") == "openrouter":
            merged_extra_body = self._merge_openrouter_usage_extra_body(
                kwargs.get("extra_body")
            )
            if merged_extra_body:
                kwargs["extra_body"] = merged_extra_body
        try:
            started_at = time.monotonic()
            gateway = self._privacy_gateway_for_generation()
            protected = await gateway.protect(
                kwargs,
                provider=str(getattr(self, "provider_label", "openai") or "openai"),
                base_url=str(getattr(self._openai_client, "base_url", "") or ""),
                source_kind="memory_extraction",
            )
            response = await self._openai_client.chat.completions.create(
                **protected.payload
            )
        except Exception as first_error:
            if getattr(self, "provider_label", "") in {"deepseek", "deepinfra", "openrouter"}:
                print(f"[AgentLLMClient] Dreamingメモリ抽出に失敗: {first_error}")
                raise
            # Some OpenAI-compatible providers reject optional sampling params.
            try:
                retry_started_at = time.monotonic()
                retry_kwargs = {"model": self.model_name, "messages": messages}
                retry_protected = await gateway.protect(
                    retry_kwargs,
                    provider=str(getattr(self, "provider_label", "openai") or "openai"),
                    base_url=str(getattr(self._openai_client, "base_url", "") or ""),
                    source_kind="memory_extraction",
                )
                response = await self._openai_client.chat.completions.create(
                    **retry_protected.payload
                )
            except Exception:
                print(f"[AgentLLMClient] Dreamingメモリ抽出に失敗: {first_error}")
                raise first_error
            self._record_generation_usage(
                response,
                # Compatibility fallback is still part of the same memory
                # extraction operation.  Keep the dashboard category stable
                # so the fallback response does not split totals into a
                # separate generic retry bucket.
                request_type="memory_extraction",
                started_at=retry_started_at,
            )
        else:
            self._record_generation_usage(
                response,
                request_type="memory_extraction",
                started_at=started_at,
            )
        choice = response.choices[0]
        message = choice.message
        return gateway.restore(str(getattr(message, "content", "") or ""))

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        request_type: str = "plain",
    ) -> str:
        """ツールなしのプレーンテキスト応答を生成する。

        公式 OpenAI 経路は Responses API、openrouter などは chat.completions を使う。
        """
        system = system_prompt or (
            "You are a concise Japanese assistant. Follow the user "
            "instruction exactly. Do not call tools and output only "
            "the requested format."
        )
        if getattr(self, "provider_label", "openai") == "openai":
            started_at = time.monotonic()
            gateway = self._privacy_gateway_for_generation()
            request_kwargs = {
                "model": self.model_name,
                "instructions": system,
                "input": prompt or "",
                "store": False,
            }
            protected = await gateway.protect(
                request_kwargs,
                provider=str(getattr(self, "provider_label", "openai") or "openai"),
                base_url=str(getattr(self._openai_client, "base_url", "") or ""),
                source_kind=request_type,
            )
            response = await self._openai_client.responses.create(
                **protected.payload
            )
            self._record_generation_usage(
                response,
                request_type=request_type,
                started_at=started_at,
            )
            return gateway.restore(responses_output_text(response))

        kwargs: Dict[str, Any] = dict(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt or ""},
            ],
        )
        if getattr(self, "provider_label", "") == "deepseek":
            effort = str(
                self.config.get("deepseek.reasoning_effort", "high")
                if self.config is not None and hasattr(self.config, "get")
                else "high"
            ).strip().lower()
            if effort not in {"none", "high", "max"}:
                effort = "high"
            kwargs["extra_body"] = {
                "thinking": {
                    "type": "disabled" if effort == "none" else "enabled"
                }
            }
            if effort != "none":
                kwargs["reasoning_effort"] = effort
        elif getattr(self, "provider_label", "") == "deepinfra":
            effort = str(
                self.config.get("deepinfra.reasoning_effort", "high")
                if self.config is not None and hasattr(self.config, "get")
                else "high"
            ).strip().lower()
            if effort not in {"none", "low", "medium", "high"}:
                effort = "high"
            kwargs["extra_body"] = {"reasoning_effort": effort}
        elif getattr(self, "provider_label", "") == "kimi" and self.model_name == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
        if getattr(self, "provider_label", "") == "openrouter":
            merged_extra_body = self._merge_openrouter_usage_extra_body(
                kwargs.get("extra_body")
            )
            if merged_extra_body:
                kwargs["extra_body"] = merged_extra_body
        started_at = time.monotonic()
        gateway = self._privacy_gateway_for_generation()
        protected = await gateway.protect(
            kwargs,
            provider=str(getattr(self, "provider_label", "openai") or "openai"),
            base_url=str(getattr(self._openai_client, "base_url", "") or ""),
            source_kind=request_type,
        )
        response = await self._openai_client.chat.completions.create(
            **protected.payload
        )
        self._record_generation_usage(
            response,
            request_type=request_type,
            started_at=started_at,
        )
        return gateway.restore(str(response.choices[0].message.content or ""))

    async def generate_title_async(self, prompt: str) -> str:
        """Generate a title through the side-effect-free API and meter it as title."""

        return await self.generate_plain_text_async(
            prompt,
            request_type="title",
        )

    async def generate_response_async(
        self,
        user_input: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        image_data: dict = None,
        stream_callback: Optional[StreamCallback] = None,
        steering_callback: Optional[SteeringCallback] = None,
    ) -> str:
        """Async version of generate_response"""
        from ...services.session_llm_generation import run_session_aware_generation

        return await run_session_aware_generation(
            self,
            self.config,
            user_input,
            stream_callback=stream_callback,
            steering_callback=steering_callback,
            image_data=image_data,
        )

    def generate(self, prompt: str) -> str:
        """Simple synchronous generate method for reasoning mode

        Args:
            prompt: The prompt to generate from

        Returns:
            Generated text
        """
        # Use the existing generate_response method
        return self.generate_response(prompt, stream=False)

    async def generate_async(self, prompt: str) -> str:
        """Simple async generate method for reasoning mode

        Args:
            prompt: The prompt to generate from

        Returns:
            Generated text
        """
        # Use the existing async method
        return await self.generate_response_async(prompt)
