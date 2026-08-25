"""話速調整キーワード検出器

「話速」「速度」「スピード」などのキーワードを検出し、
LLMを使って適切な話速調整係数を決定する。
"""

from typing import Optional, Dict, Any
import re
import json
import logging
import time
from collections.abc import Mapping
from types import SimpleNamespace
from ..base import LLMKeywordDetector, KeywordDetectionResult, KeywordAction
from ....services.outbound_privacy_service import OutboundPrivacyGateway

logger = logging.getLogger(__name__)


def normalize_usage(*args, **kwargs):
    """Lazy usage normalization keeps keyword detectors cycle-free."""

    from ....llm.conversation_context import normalize_usage as _normalize

    return _normalize(*args, **kwargs)


def persist_usage_sync(*args, **kwargs):
    """Lazy usage persistence keeps keyword detectors lightweight."""

    from ....llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

_RECORDED_SPEECH_RESPONSES: list[object] = []


def _usage_client(context=None):
    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context
    try:
        from ....services.turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None

    def _value(name, default=None):
        if isinstance(context, Mapping):
            value = context.get(name)
            if value is not None:
                return value
        value = getattr(context, name, None)
        if value is not None:
            return value
        return getattr(turn, name, default) if turn is not None else default

    user_id = _value("user_id")
    return SimpleNamespace(
        current_session_id=_value("current_session_id", _value("session_id")),
        current_project_id=_value("current_project_id", _value("project_id")),
        character_name=_value("character_name"),
        _get_session_user_id=lambda: user_id,
    )


def _mark_response_recorded(response: object) -> bool:
    try:
        if getattr(response, "_aoitalk_usage_recorded", False):
            return True
        object.__setattr__(response, "_aoitalk_usage_recorded", True)
        return False
    except Exception:
        if any(item is response for item in _RECORDED_SPEECH_RESPONSES):
            return True
        _RECORDED_SPEECH_RESPONSES.append(response)
        del _RECORDED_SPEECH_RESPONSES[:-16]
        return False


def _record_speech_usage(
    response,
    *,
    usage_context=None,
    model: str | None = None,
    started: float | None = None,
) -> bool:
    raw_usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
    if raw_usage is None:
        return False
    usage = normalize_usage(
        raw_usage,
        provider="openai",
        resolved_model=(
            response.get("model") if isinstance(response, Mapping) else getattr(response, "model", None)
        ),
    )
    if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
        return False
    if _mark_response_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(usage_context),
            provider="openai",
            model=str(model or "gpt-5.6-luna"),
            usage=usage,
            request_type="speech_rate",
            latency_ms=(
                max(0, int((time.monotonic() - started) * 1000))
                if started is not None
                else 0
            ),
            is_streaming=False,
        )
        return True
    except Exception:
        logger.debug("話速検出LLMのusage記録に失敗しました", exc_info=True)
        return False


class SpeechRateDetector(LLMKeywordDetector):
    """話速調整のキーワード検出器"""
    
    def __init__(
        self,
        llm_client: Any = None,
        enabled: bool = True,
        use_llm_extraction: bool = True,
        confidence_threshold: float = 0.7,
        fallback_to_regex: bool = True,
        config: Dict[str, Any] = None,
        usage_context: Any = None,
    ):
        super().__init__("speech_rate", llm_client, enabled)
        self.use_llm_extraction = use_llm_extraction
        self.confidence_threshold = confidence_threshold
        self.fallback_to_regex = fallback_to_regex
        self.config = config or {}
        self.usage_context = usage_context
        
        # 話速調整に関連するキーワード
        self.speed_keywords = [
            "話速", "話す速度", "話すスピード",
            "速度", "スピード", "speed",
            "早く", "速く", "ゆっくり", "遅く",
            "早め", "速め", "遅め", "ゆったり",
            "もっと早く", "もっと速く", "もっとゆっくり",
            "普通に", "通常に", "標準に", "デフォルト"
        ]
        
        # キーワードパターンの正規表現
        self.keyword_pattern = re.compile(
            r'(' + '|'.join(re.escape(kw) for kw in self.speed_keywords) + r')',
            re.IGNORECASE
        )
        
        # 現在の話速調整係数（グローバル状態として保持）
        self._current_speed_adjustment = 1.0
    
    def detect(self, text: str) -> KeywordDetectionResult:
        """テキストから話速調整キーワードを検出"""
        if not self.enabled:
            return KeywordDetectionResult(detected=False)
        
        # キーワードの存在をチェック
        if not self.keyword_pattern.search(text):
            return KeywordDetectionResult(detected=False)
        
        # LLMを使用して意図を抽出
        extraction_result = None
        if self.use_llm_extraction and self.llm_client:
            try:
                extraction_result = self._extract_with_llm(text)
            except Exception as e:
                logger.error(f"LLM抽出エラー: {e}")
                if not self.fallback_to_regex:
                    return KeywordDetectionResult(detected=False)
        
        # LLMが使用できない場合は正規表現で基本的な抽出
        if not extraction_result and self.fallback_to_regex:
            extraction_result = self._extract_with_regex(text)
        
        if extraction_result:
            return KeywordDetectionResult(
                detected=True,
                action=KeywordAction.PROCESS,
                data=extraction_result,
                bypass_llm=True  # LLM処理をバイパス（キーワード処理で完結）
            )
        
        return KeywordDetectionResult(detected=False)
    
    def _extract_with_llm(self, text: str) -> Optional[Dict[str, Any]]:
        """LLMを使って話速調整の意図を抽出"""
        prompt = f"""ユーザーのテキストから話速調整の意図を抽出してください。

入力テキスト: "{text}"

現在の話速調整係数: {self._current_speed_adjustment}

以下のJSON形式で回答してください：
{{
    "confidence": 0.0-1.0の数値（話速調整の意図の確信度）,
    "action": "increase" | "decrease" | "reset" | "set",
    "target_speed": 推奨される話速調整係数（0.5-2.0の範囲）,
    "description": "どのような調整か（例：「もっと速く」「ゆっくり」「普通に戻す」）"
}}

話速調整の目安：
- 「もっと速く」「早く」→ 現在の1.2-1.5倍
- 「速め」→ 現在の1.1-1.2倍
- 「ゆっくり」「遅く」→ 現在の0.7-0.8倍
- 「もっとゆっくり」→ 現在の0.5-0.7倍
- 「普通に」「標準に」→ 1.0にリセット

        JSON以外の説明は不要です。"""

        try:
            # Resolve the deployment before touching an injected client.  An
            # Enterprise fixed backend owns the endpoint/model, so a caller
            # that passes an AgentLLMClient (often constructed for OpenAI)
            # cannot silently bypass Gemma/vLLM or another fixed target.
            from ....llm.deployment_resolver import (
                preflight_deployment,
                resolve_llm_deployment,
            )

            deployment = resolve_llm_deployment(self.config)
            if deployment is not None:
                preflight_deployment(self.config)

            active_client = self.llm_client
            if deployment is not None and deployment.fixed:
                try:
                    from ....llm.manager import create_llm_client

                    active_client = create_llm_client(self.config)
                except Exception as exc:
                    raise RuntimeError(
                        "Enterprise speech-rate LLM is unavailable"
                    ) from exc
                if active_client is None:
                    raise RuntimeError("Enterprise speech-rate LLM is unavailable")

            def _invoke_client(client):
                """Call the synchronous shape exposed by native LLM clients."""

                if hasattr(client, "generate_simple"):
                    return client.generate_simple(prompt)
                if hasattr(client, "generate_response"):
                    return client.generate_response(prompt, stream=False)
                if hasattr(client, "generate"):
                    return client.generate(prompt)
                if hasattr(client, "get_response"):
                    return client.get_response(
                        [{"role": "user", "content": prompt}]
                    )
                return None

            response = _invoke_client(active_client) if active_client is not None else None
            if (
                response is None
                and active_client is not None
                and deployment is not None
                and deployment.fixed
            ):
                raise RuntimeError(
                    "Speech-rate extraction requires a synchronous LLM client"
                )

            if response is None:
                # Prefer the normal factory so an Enterprise deployment's
                # effective provider/endpoint is enforced.  The historical
                # direct OpenAI fallback remains only for personal/no-env
                # callers that do not supply a usable configuration.
                effective_client = None
                if deployment is not None:
                    try:
                        from ....llm.manager import create_llm_client

                        effective_client = create_llm_client(self.config)
                    except Exception as exc:
                        raise RuntimeError(
                            "Enterprise speech-rate LLM is unavailable"
                        ) from exc
                    response = _invoke_client(effective_client)
                    if response is None:
                        raise RuntimeError(
                            "Speech-rate extraction requires a synchronous LLM client"
                        )
                else:
                    import openai

                    client = openai.OpenAI()
                    config_getter = getattr(self.config, "get", None)

                    def _config_value(key: str, default: Any = None) -> Any:
                        if callable(config_getter):
                            try:
                                value = config_getter(key, default)
                            except TypeError:
                                value = config_getter(key)
                            if value is not None:
                                return value
                        if isinstance(self.config, Mapping):
                            current: Any = self.config
                            for part in key.split("."):
                                if not isinstance(current, Mapping) or part not in current:
                                    return default
                                current = current[part]
                            return current
                        return default

                    model = str(
                        getattr(self.llm_client, "model_name", None)
                        or _config_value("llm_model", "")
                        or _config_value("openai.model", "")
                        or "gpt-5.6-luna"
                    ).strip()
                    model_leaf = model.lower().rsplit("/", 1)[-1]
                    effort = str(
                        _config_value("openai.reasoning_effort", "") or ""
                    ).strip().lower()
                    if not effort and model_leaf.startswith("gpt-5.6-luna"):
                        effort = "max"
                    try:
                        from ....services.llm_model_catalog import (
                            reasoning_effort_options_for_model,
                        )

                        if effort not in reasoning_effort_options_for_model(
                            "openai", model
                        ):
                            effort = ""
                    except Exception:
                        effort = ""
                    gateway = OutboundPrivacyGateway(
                        self.config,
                        session_id=str(getattr(self.usage_context, "current_session_id", "") or ""),
                        user_id=str(getattr(self.usage_context, "session_user_id", "") or ""),
                    )
                    protected = gateway.protect_sync(
                        {"input": prompt},
                        provider="openai",
                        source_kind="speech_rate_detector",
                    )
                    request_input = str(
                        protected.payload.get("input", prompt)
                        if isinstance(protected.payload, Mapping)
                        else prompt
                    )
                    started = time.monotonic()
                    request_kwargs: dict[str, Any] = {
                        "model": model,
                        "input": request_input,
                    }
                    if effort:
                        request_kwargs["reasoning"] = {
                            "effort": effort,
                            "summary": "auto",
                        }
                    completion = client.responses.create(**request_kwargs)
                    _record_speech_usage(
                        completion,
                        usage_context=self.usage_context or self.llm_client,
                        model=model,
                        started=started,
                    )
                    response = gateway.restore(
                        getattr(completion, "output_text", "") or ""
                    ) or None
            
            if not response:
                return None
                
            # JSON部分を抽出
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                result = json.loads(json_match.group())
                
                # 確信度チェック
                if result.get('confidence', 0) >= self.confidence_threshold:
                    return result
        except Exception as e:
            logger.error(f"LLM話速抽出エラー: {e}")
        
        return None
    
    def _extract_with_regex(self, text: str) -> Optional[Dict[str, Any]]:
        """正規表現を使った基本的な話速調整抽出"""
        text_lower = text.lower()
        
        # 増速パターン
        if any(kw in text_lower for kw in ["もっと速く", "もっと早く", "すごく速く", "すごく早く"]):
            return {
                "action": "increase",
                "target_speed": min(self._current_speed_adjustment * 1.3, 2.0),
                "description": "もっと速く"
            }
        elif any(kw in text_lower for kw in ["速く", "早く", "速め", "早め"]):
            return {
                "action": "increase", 
                "target_speed": min(self._current_speed_adjustment * 1.15, 2.0),
                "description": "速く"
            }
        
        # 減速パターン
        elif any(kw in text_lower for kw in ["もっとゆっくり", "すごくゆっくり", "もっと遅く"]):
            return {
                "action": "decrease",
                "target_speed": max(self._current_speed_adjustment * 0.6, 0.5),
                "description": "もっとゆっくり"
            }
        elif any(kw in text_lower for kw in ["ゆっくり", "遅く", "遅め", "ゆったり"]):
            return {
                "action": "decrease",
                "target_speed": max(self._current_speed_adjustment * 0.8, 0.5),
                "description": "ゆっくり"
            }
        
        # リセットパターン
        elif any(kw in text_lower for kw in ["普通", "通常", "標準", "デフォルト", "元に"]):
            return {
                "action": "reset",
                "target_speed": 1.0,
                "description": "標準速度に戻す"
            }
        
        return None
    
    def process(self, result: KeywordDetectionResult) -> Optional[str]:
        """話速調整を実行"""
        if not result.data:
            return None
        
        data = result.data
        new_speed = data.get('target_speed', 1.0)
        description = data.get('description', '話速調整')
        
        # 話速調整を適用（設定ファイルに保存）
        try:
            # 現在の速度を保存
            old_speed = self._current_speed_adjustment
            
            # 新しい速度を適用
            self._apply_speed_adjustment(new_speed)
            self._current_speed_adjustment = new_speed
            
            # フィードバックメッセージ
            if new_speed == 1.0:
                return f"話速を標準に戻しました"
            elif new_speed > old_speed:
                return f"話速を速くしました（{new_speed:.1f}倍）"
            elif new_speed < old_speed:
                return f"話速をゆっくりにしました（{new_speed:.1f}倍）"
            else:
                return f"話速を{new_speed:.1f}倍に調整しました"
                
        except Exception as e:
            logger.error(f"話速調整エラー: {e}")
            return "話速の調整に失敗しました"
    
    def _apply_speed_adjustment(self, speed_adjustment: float):
        """設定ファイルに話速調整を保存"""
        from ....app_config_store import update_app_config_key_sync
        update_app_config_key_sync('tts.speed_adjustment', float(speed_adjustment))
        
        # 設定ファイルを読み込み
        
        # TTSセクションに話速調整を追加
        
        # 設定ファイルに書き戻し
