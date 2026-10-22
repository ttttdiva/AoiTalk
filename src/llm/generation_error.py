"""LLM応答生成の失敗理由を分類し、ユーザー向け日本語文言に変換するモジュール。

目的:
    例外を文字列へ潰す前に分類し、「なぜ失敗したのか」「次に何をすればよいのか」が
    分かる日本語をユーザー可視経路（チャット返信・生成ステータス）へ届ける。
    技術詳細は agent_runs.error などの可観測性経路向けに別途保持する。

重要:
    OpenAI は insufficient_quota（残高切れ・課金の問題）と rate_limit_exceeded
    （一時的なレート超過・待てば回復）の両方を HTTP 429 で返す。意味もユーザーが
    取るべき行動も全く違うため、ステータスコードだけで一括りにせず必ず
    error.code / error.type を見て分岐する。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Optional


class GenerationErrorKind:
    """分類結果の種別。"""

    INSUFFICIENT_QUOTA = "insufficient_quota"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    PERMISSION_DENIED = "permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    PRICE_LIMIT = "price_limit"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LENGTH = "context_length"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    EMPTY_RESPONSE = "empty_response"
    OUTPUT_INCOMPLETE = "output_incomplete"
    LLM_NOT_CONFIGURED = "llm_not_configured"
    TOOL_FAILURE_CIRCUIT_OPEN = "tool_failure_circuit_open"
    TOOL_EXECUTION_BLOCKED = "tool_execution_blocked"
    READ_STAGNATION = "read_stagnation"
    UNKNOWN = "unknown"


# ユーザー向け文言（原因と次の行動が分かることを最優先にする）
_USER_MESSAGES: Dict[str, str] = {
    GenerationErrorKind.INSUFFICIENT_QUOTA: (
        "OpenAI APIプロジェクトの利用上限またはクレジットに問題があります。"
        "OpenAIのProject Limits / Billingを確認してください。"
    ),
    GenerationErrorKind.RATE_LIMIT: (
        "OpenAI APIのレート制限に達しました。"
        "しばらく待ってから再試行してください。"
    ),
    GenerationErrorKind.AUTHENTICATION: (
        "OpenAI APIキーが無効です。APIキーの設定を確認してください。"
    ),
    GenerationErrorKind.PERMISSION_DENIED: (
        "このAPIキーでは指定されたモデルまたは機能を利用する権限がありません。"
        "OpenAIのプロジェクト権限とモデル設定を確認してください。"
    ),
    GenerationErrorKind.PRICE_LIMIT: (
        "指定された上限価格を満たす接続先がありません。"
        "モデルと価格条件を確認してください。"
    ),
    GenerationErrorKind.MODEL_NOT_FOUND: (
        "指定されたモデルが利用できません。モデル設定を確認してください。"
    ),
    GenerationErrorKind.CONTEXT_LENGTH: (
        "入力がモデルのコンテキスト上限を超えました。"
        "会話を新しく始めるか、添付・参照する内容を減らしてください。"
    ),
    GenerationErrorKind.INVALID_REQUEST: (
        "LLMへのリクエストが受け付けられませんでした。"
        "モデル設定または送信内容を確認してください。"
    ),
    GenerationErrorKind.CONNECTION: (
        "LLMサーバーへ接続できません。"
        "ネットワークまたはサーバー設定を確認してください。"
    ),
    GenerationErrorKind.TIMEOUT: (
        "LLMサーバーからの応答がタイムアウトしました。"
        "しばらく待ってから再試行してください。"
    ),
    GenerationErrorKind.SERVER_ERROR: (
        "LLMサーバー側でエラーが発生しました。"
        "しばらく待ってから再試行してください。"
    ),
    GenerationErrorKind.EMPTY_RESPONSE: (
        "モデルから応答が返りませんでした。しばらく待ってから再試行してください。"
    ),
    GenerationErrorKind.OUTPUT_INCOMPLETE: (
        "モデルの出力が上限で打ち切られたか本文が空だったため、結果を保存しませんでした。"
        "出力上限・推論強度の設定を確認してから再試行してください。"
    ),
    GenerationErrorKind.LLM_NOT_CONFIGURED: (
        "クリップ取り込み用LLMが設定されていません。"
        "管理者にLLM設定を確認してください。"
    ),
    GenerationErrorKind.TOOL_FAILURE_CIRCUIT_OPEN: (
        "同じツールの失敗が繰り返されたため、自動処理を停止しました。"
        "途中の結果と保存状態を確認してください。"
    ),
    GenerationErrorKind.READ_STAGNATION: (
        "新しい情報が得られない読取が繰り返され、変更や検証に進めなかったため、自動処理を停止しました。"
        "ここまでの調査結果と未達の内容を確認してください。"
    ),
    GenerationErrorKind.TOOL_EXECUTION_BLOCKED: (
        "ツール実行が許可されていないため、自動処理を停止しました。"
        "権限・承認状態を確認してください。"
    ),
}

# 分類できない場合に使う既定文言（詳細は握り潰さず末尾へ付ける）
DEFAULT_GENERATION_FAILURE_MESSAGE = "応答生成に失敗しました。"


def user_message_for_generation_kind(kind: str) -> str | None:
    """Return the canonical user message for an allowlisted failure kind."""

    return _USER_MESSAGES.get(str(kind or "").strip().lower())


@dataclass(frozen=True)
class GenerationFailure:
    """応答生成失敗の分類結果。

    Attributes:
        kind: GenerationErrorKind のいずれか。
        user_message: ユーザーへそのまま見せる日本語文言。
        technical_detail: agent_runs.error などへ記録する技術詳細。
    """

    kind: str
    user_message: str
    technical_detail: str

    @property
    def is_empty_response(self) -> bool:
        return self.kind == GenerationErrorKind.EMPTY_RESPONSE


class ModelOutputIncompleteError(RuntimeError):
    """A helper model call returned a truncated or empty body.

    Callers must not substitute a fixed fallback for this error: the request
    was accepted and billed, but the requested artifact was never produced.
    """

    def __init__(self, reason: str, *, transport: str, request_type: str = "") -> None:
        self.reason = str(reason or "unknown")
        self.transport = str(transport or "")
        self.request_type = str(request_type or "")
        super().__init__(
            f"model output incomplete: reason={self.reason} "
            f"transport={self.transport} request_type={self.request_type}"
        )


def _response_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def require_complete_model_output(
    response: Any,
    text: str,
    *,
    transport: str,
    request_type: str = "",
) -> str:
    """Return ``text`` only when the provider reports a complete, non-empty body.

    Responses API: ``status == "incomplete"`` (e.g. ``incomplete_details.reason
    == "max_output_tokens"`` when reasoning consumed the output budget).
    Chat Completions: ``finish_reason == "length"``.
    """

    if transport == "openai.responses":
        if str(_response_field(response, "status") or "") == "incomplete":
            details = _response_field(response, "incomplete_details")
            reason = str(_response_field(details, "reason") or "incomplete")
            raise ModelOutputIncompleteError(reason, transport=transport, request_type=request_type)
    else:
        choices = _response_field(response, "choices") or []
        first = choices[0] if choices else None
        if first is not None and str(_response_field(first, "finish_reason") or "") == "length":
            raise ModelOutputIncompleteError("length", transport=transport, request_type=request_type)
    if not str(text or "").strip():
        raise ModelOutputIncompleteError("empty_output", transport=transport, request_type=request_type)
    return text


def empty_response_failure() -> GenerationFailure:
    """例外ではなく純粋に空応答だった場合の分類結果。"""
    return GenerationFailure(
        kind=GenerationErrorKind.EMPTY_RESPONSE,
        user_message=_USER_MESSAGES[GenerationErrorKind.EMPTY_RESPONSE],
        technical_detail="Assistant generation returned no response (empty response)",
    )


def tool_failure_circuit_open_failure(
    *,
    family: str,
    signature: str = "",
) -> GenerationFailure:
    """Return the terminal failure for a framework-owned tool circuit.

    ``family`` and ``signature`` are already normalized by the breaker.  Keep
    the technical detail limited to those bounded identifiers so provider or
    tool output can never be copied into a persisted generation failure.
    """

    safe_family = str(family or "unknown").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", safe_family):
        safe_family = "unknown"
    safe_signature = str(signature or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,255}", safe_signature):
        safe_signature = ""
    detail = f"tool_failure_circuit_open family={safe_family}"
    if safe_signature:
        detail += f" signature={safe_signature}"
    return GenerationFailure(
        kind=GenerationErrorKind.TOOL_FAILURE_CIRCUIT_OPEN,
        user_message=_USER_MESSAGES[GenerationErrorKind.TOOL_FAILURE_CIRCUIT_OPEN],
        technical_detail=detail,
    )


def read_stagnation_failure(*, signature: str = "") -> GenerationFailure:
    """Return the terminal checkpoint failure for repeated no-new-information reads."""

    safe_signature = str(signature or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,255}", safe_signature):
        safe_signature = ""
    detail = "read_stagnation"
    if safe_signature:
        detail += f" signature={safe_signature}"
    return GenerationFailure(
        kind=GenerationErrorKind.READ_STAGNATION,
        user_message=_USER_MESSAGES[GenerationErrorKind.READ_STAGNATION],
        technical_detail=detail,
    )


def tool_execution_blocked_failure(*, source: str) -> GenerationFailure:
    """Return a safe terminal failure for a framework-owned execution block."""

    safe_source = str(source or "framework").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", safe_source):
        safe_source = "framework"
    return GenerationFailure(
        kind=GenerationErrorKind.TOOL_EXECUTION_BLOCKED,
        user_message=_USER_MESSAGES[GenerationErrorKind.TOOL_EXECUTION_BLOCKED],
        technical_detail=f"tool_execution_blocked source={safe_source}",
    )


def _openai_module() -> Optional[Any]:
    """OpenAI SDK を安全に import する（未インストールでも壊れない）。"""
    try:
        import openai  # type: ignore

        return openai
    except Exception:
        return None


def _error_payload(exc: BaseException) -> Dict[str, Any]:
    """例外から OpenAI エラーボディ（error オブジェクト）を可能な範囲で取り出す。"""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error
        return body

    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parsed = response.json()
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                return error
            return parsed
    return {}


def _error_code(exc: BaseException) -> str:
    """error.code / error.type / 例外の code 属性を正規化して返す。"""
    payload = _error_payload(exc)
    for key in ("code", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    value = getattr(exc, "code", None)
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    # Some gateways flatten the JSON body into the exception message.  Keep a
    # narrow allowlist so the billing exhaustion marker is still classified
    # correctly without treating arbitrary provider prose as an error code.
    lowered_message = str(exc or "").strip().lower()
    for candidate in (
        "credit_balance_exhausted",
        "insufficient_quota",
        "rate_limit_exceeded",
    ):
        if candidate in lowered_message:
            return candidate
    return ""


def _status_code(exc: BaseException) -> Optional[int]:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _classify_by_code(code: str) -> Optional[str]:
    """error.code / error.type から種別を決める。429 の分岐はここが要。"""
    if not code:
        return None
    if code in {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "project_spend_limit_exceeded",
        "organization_spend_limit_exceeded",
        "quota_exceeded",
        # AoiTalk/OpenAI gateway responses may expose the billing exhaustion
        # reason under this code instead of OpenAI's ``insufficient_quota``.
        "credit_balance_exhausted",
    }:
        return GenerationErrorKind.INSUFFICIENT_QUOTA
    if code in {"rate_limit_exceeded", "requests", "tokens"}:
        return GenerationErrorKind.RATE_LIMIT
    if code in {
        "invalid_api_key",
        "invalid_authentication",
        "authentication_error",
        "account_deactivated",
    }:
        return GenerationErrorKind.AUTHENTICATION
    if code in {"model_not_found", "unknown_model"}:
        return GenerationErrorKind.MODEL_NOT_FOUND
    if code in {"context_length_exceeded", "string_above_max_length"}:
        return GenerationErrorKind.CONTEXT_LENGTH
    if code in {"insufficient_permissions", "permission_denied", "unsupported_country_region_territory"}:
        return GenerationErrorKind.PERMISSION_DENIED
    return None



def _is_price_routing_failure(exc: BaseException) -> bool:
    """A routing 404 is not proof that the requested model does not exist."""
    if _status_code(exc) != 404:
        return False
    payload = _error_payload(exc)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        step = metadata.get("failed_routing_step")
        if isinstance(step, str) and step.strip().lower() == "filter by max price":
            return True
    # Narrow phrase only, including gateways that flatten their JSON body.
    marker = "no endpoints found that satisfy the max price for this request"
    message = payload.get("message")
    return (isinstance(message, str) and marker in message.lower()) or marker in str(exc).lower()


def _classify_by_status(status: Optional[int]) -> Optional[str]:
    if status is None:
        return None
    if status == 401:
        return GenerationErrorKind.AUTHENTICATION
    if status == 403:
        return GenerationErrorKind.PERMISSION_DENIED
    if status == 404:
        return GenerationErrorKind.MODEL_NOT_FOUND
    if status == 408:
        return GenerationErrorKind.TIMEOUT
    if status == 429:
        # code で判別できなかった 429 は一時的なレート超過として扱う。
        return GenerationErrorKind.RATE_LIMIT
    if status in {400, 422}:
        return GenerationErrorKind.INVALID_REQUEST
    if status >= 500:
        return GenerationErrorKind.SERVER_ERROR
    return None


def _classify_by_exception_type(exc: BaseException) -> Optional[str]:
    openai = _openai_module()
    if openai is None:
        return None

    def _is(name: str) -> bool:
        error_type = getattr(openai, name, None)
        return isinstance(error_type, type) and isinstance(exc, error_type)

    if _is("APITimeoutError"):
        return GenerationErrorKind.TIMEOUT
    if _is("APIConnectionError"):
        return GenerationErrorKind.CONNECTION
    if _is("AuthenticationError"):
        return GenerationErrorKind.AUTHENTICATION
    if _is("PermissionDeniedError"):
        return GenerationErrorKind.PERMISSION_DENIED
    if _is("NotFoundError"):
        return GenerationErrorKind.MODEL_NOT_FOUND
    if _is("RateLimitError"):
        # 429 は insufficient_quota と rate_limit_exceeded の両方が来る。
        # ここへ到達するのは code で判別できなかった場合のみ。
        return GenerationErrorKind.RATE_LIMIT
    if _is("InternalServerError"):
        return GenerationErrorKind.SERVER_ERROR
    if _is("BadRequestError") or _is("UnprocessableEntityError"):
        return GenerationErrorKind.INVALID_REQUEST
    return None


def _classify_by_builtin_type(exc: BaseException) -> Optional[str]:
    if isinstance(exc, TimeoutError):
        return GenerationErrorKind.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return GenerationErrorKind.CONNECTION
    return None


def classify_generation_error(exc: BaseException) -> GenerationFailure:
    """例外オブジェクトを分類し、ユーザー向け文言と技術詳細を返す。

    Args:
        exc: 応答生成中に発生した例外。

    Returns:
        GenerationFailure: 種別・ユーザー向け日本語文言・技術詳細。
    """
    technical_detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ModelOutputIncompleteError):
        return GenerationFailure(
            kind=GenerationErrorKind.OUTPUT_INCOMPLETE,
            user_message=_USER_MESSAGES[GenerationErrorKind.OUTPUT_INCOMPLETE],
            technical_detail=technical_detail,
        )

    # code / type による判別を最優先する（429 の意味分岐がここに依存する）。
    kind = _classify_by_code(_error_code(exc))
    if kind in (None, GenerationErrorKind.MODEL_NOT_FOUND) and _is_price_routing_failure(exc):
        kind = GenerationErrorKind.PRICE_LIMIT
    if kind is None:
        kind = _classify_by_exception_type(exc)
    if kind is None:
        kind = _classify_by_status(_status_code(exc))
    if kind is None:
        kind = _classify_by_builtin_type(exc)

    if kind is None:
        # 未知の例外は詳細を握り潰さず、そのまま添えて返す。
        return GenerationFailure(
            kind=GenerationErrorKind.UNKNOWN,
            user_message=(
                f"{DEFAULT_GENERATION_FAILURE_MESSAGE}"
                f"詳細: {technical_detail}"
            ),
            technical_detail=technical_detail,
        )

    return GenerationFailure(
        kind=kind,
        user_message=_USER_MESSAGES[kind],
        technical_detail=technical_detail,
    )
