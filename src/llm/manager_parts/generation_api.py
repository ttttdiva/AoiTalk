"""AgentLLMClient の公開生成 API・ストリームイベント抽出・シーン画像生成 Mixin。

manager.py から責務分割したもの。メソッド本体のロジックは一切変更していない。
"""

import concurrent.futures
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Generator, List, Optional, Union

from ..native_runtime import responses_output_text

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
SteeringCallback = Callable[[], Awaitable[List[str]]]

_SEARCH_TOOL_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'、。]+")
_SEARCH_OUTPUT_LIMIT = 12000
_SEARCH_URL_LIMIT = 20


class GenerationApiMixin:
    """chat/generate_* 系の公開エントリポイント、ストリーム抽出、シーン画像生成。"""

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
        self, scene_description: str
    ) -> Optional[Dict[str, Any]]:
        """シーン画像を生成し、表示用タグと配信データを返す。"""
        try:
            # キャラクターの外見タグを取得
            appearance_tags = ""
            negative_tags = ""
            comfyui_overrides: Dict[str, Any] = {}
            try:
                from ...services.character_service import get_character_for_prompt

                char_data = await get_character_for_prompt(self.character_name)
                if char_data:
                    appearance_tags = char_data.get("appearance_tags", "")
                    negative_tags = char_data.get("negative_tags", "")
                    comfyui_overrides = char_data.get("comfyui_config", {}) or {}
            except Exception as e:
                logger.warning(f"外見タグ取得エラー: {e}")

            from ...services.image_prompt_builder import build_image_prompt

            prompt, default_negative = await build_image_prompt(
                self.history_manager.get_all(),
                appearance_tags,
                scene_description,
            )
            negative_parts = [p for p in [negative_tags, default_negative] if p]
            combined_negative = ", ".join(negative_parts)

            logger.info(
                f"[AgentLLMClient] シーン画像生成開始: {prompt[:80]}..."
            )

            # 画像生成エンジンに委譲
            try:
                from ...services.comfyui_service import generate_image

                result = await generate_image(
                    prompt=prompt,
                    negative_prompt=combined_negative,
                    overrides=comfyui_overrides,
                )
                if result and result.get("success"):
                    image_path = result.get("image_path")
                    tag = f"[GENERATED_IMAGE:{image_path}]"
                    logger.info(f"[AgentLLMClient] シーン画像生成完了: {image_path}")
                    return {
                        "content": tag,
                        "tag": tag,
                        "image_path": image_path,
                        "image_url": result.get("image_url"),
                        "filename": result.get("filename"),
                    }
            except ImportError:
                logger.info(
                    "[AgentLLMClient] ComfyUI サービスが利用できません。画像生成スキップ。"
                )
            except Exception as e:
                logger.warning(f"画像生成エラー: {e}")
            return None

        except Exception as e:
            logger.error(f"[AgentLLMClient] シーン画像生成タスクエラー: {e}")
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
                response = await self._openai_client.responses.create(
                    model=self.model_name,
                    instructions=system_prompt or "",
                    input=prompt or "",
                    store=False,
                )
            except Exception as first_error:
                print(f"[AgentLLMClient] Dreamingメモリ抽出に失敗: {first_error}")
                raise first_error
            return responses_output_text(response)

        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": prompt or ""},
        ]
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        if getattr(self, "provider_label", "") == "kimi" and self.model_name == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
            kwargs["max_completion_tokens"] = 1200
        else:
            kwargs["temperature"] = 0.0
            kwargs["max_tokens"] = 1200
        try:
            response = await self._openai_client.chat.completions.create(**kwargs)
        except Exception as first_error:
            # Some OpenAI-compatible providers reject optional sampling params.
            try:
                response = await self._openai_client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                )
            except Exception:
                print(f"[AgentLLMClient] Dreamingメモリ抽出に失敗: {first_error}")
                raise first_error
        choice = response.choices[0]
        message = choice.message
        return str(getattr(message, "content", "") or "")

    async def generate_plain_text_async(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
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
            response = await self._openai_client.responses.create(
                model=self.model_name,
                instructions=system,
                input=prompt or "",
                store=False,
            )
            return responses_output_text(response)

        kwargs: Dict[str, Any] = dict(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt or ""},
            ],
        )
        if getattr(self, "provider_label", "") == "kimi" and self.model_name == "kimi-k3":
            kwargs["reasoning_effort"] = "max"
        response = await self._openai_client.chat.completions.create(**kwargs)
        return str(response.choices[0].message.content or "")

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
        return await self._generate_async(
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
