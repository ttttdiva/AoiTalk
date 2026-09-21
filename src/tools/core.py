"""
バックエンド非依存のツール定義

OpenAI API, Gemini API, CLI, SGLang 等のどのバックエンドでも
使える統一的なツール定義を提供する。
"""
from dataclasses import dataclass, field
from copy import deepcopy
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)
import asyncio
import contextvars
import inspect


@dataclass
class ToolParam:
    """ツールパラメータの定義"""
    name: str
    type: str  # "string", "integer", "number", "boolean", "array", "object"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None
    schema: Optional[Dict[str, Any]] = None


class ToolArgumentValidationError(ValueError):
    """Safe deterministic failure raised before a tool callable is invoked."""

    def __init__(
        self,
        tool_name: str,
        *,
        code: str,
        argument_names: Iterable[str] = (),
    ) -> None:
        self.tool_name = str(tool_name or "tool").strip() or "tool"
        self.code = str(code or "tool_argument_invalid").strip()
        self.argument_names = tuple(
            sorted(
                {
                    str(name or "").strip()
                    for name in argument_names
                    if str(name or "").strip()
                }
            )
        )
        label = {
            "tool_argument_unknown": "unknown arguments",
            "tool_argument_conflict": "conflicting arguments",
            "tool_argument_missing": "missing arguments",
            "tool_argument_invalid": "invalid arguments",
        }.get(self.code, "invalid arguments")
        # Values and caller-controlled property names intentionally stay out
        # of exception text because provider/tool failures can reach logs.
        super().__init__(f"{self.tool_name}: {label}")


@dataclass
class ToolDefinition:
    """バックエンド非依存のツール定義"""
    name: str
    description: str
    function: Callable
    parameters: List[ToolParam] = field(default_factory=list)
    is_async: bool = False
    risk: str = "low"
    side_effect: str = "none"
    requires_approval: bool = False
    timeout_seconds: Optional[float] = None
    supports_parallel: bool = True
    owner: str = "core"
    availability: Optional[Dict[str, Any]] = None
    argument_aliases: Dict[str, str] = field(default_factory=dict)
    hidden_argument_names: tuple[str, ...] = field(default_factory=tuple)
    _legacy_variadic_kwargs: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Populate a schema for legacy definitions that omit ``parameters``.

        Most tools are built through :func:`tool` and already carry an
        explicit schema.  A number of direct/third-party integrations still
        construct ``ToolDefinition`` with only a callable, however.  Keeping
        those definitions executable after argument validation means the
        closed-schema contract can be applied consistently without silently
        treating every argument as unknown. Variadic callables remain
        schema-less because their accepted keys cannot be represented as a
        closed JSON object. A legacy definition whose only keyword surface is
        ``**kwargs`` retains that historical open-key behavior; explicit or
        inferable schemas remain closed.

        """

        # An explicit schema is always authoritative, including when the
        # backing callable itself happens to accept **kwargs.
        if self.parameters or not callable(self.function):
            return
        try:
            signature = inspect.signature(self.function)
        except (TypeError, ValueError):
            return
        try:
            hints = get_type_hints(self.function)
        except Exception:  # noqa: BLE001 - annotations are advisory only
            hints = {}

        inferred: List[ToolParam] = []
        has_variadic_kwargs = False
        for name, parameter in signature.parameters.items():
            if name in {"self", "cls"}:
                continue
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                # Tool invocations are keyword mappings; *args contributes no
                # representable JSON-object properties.
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_variadic_kwargs = True
                continue
            py_type = hints.get(name, str)
            json_type, is_optional = _resolve_type(py_type)
            has_default = parameter.default is not inspect.Parameter.empty
            inferred.append(
                ToolParam(
                    name=name,
                    type=json_type,
                    required=not (is_optional or has_default),
                    default=(parameter.default if has_default else None),
                    schema=_resolve_param_schema(py_type, json_type),
                )
            )
        self.parameters = inferred
        # Compatibility is intentionally limited to the otherwise
        # unrepresentable legacy shape ``ToolDefinition(function=fn)`` where
        # fn exposes no named keyword contract and accepts **kwargs. Mixed
        # ``named, **kwargs`` callables use the inferred named schema and stay
        # closed.
        self._legacy_variadic_kwargs = bool(
            has_variadic_kwargs and not inferred
        )

    def add_argument_alias(self, alias: str, canonical: str) -> None:
        """Add one explicitly supported compatibility alias.

        Aliases exist only at the tool boundary. The backing Python callable
        always receives the canonical key.
        """

        alias_name = str(alias or "").strip()
        canonical_name = str(canonical or "").strip()
        parameter_names = {param.name for param in self.parameters}
        if (
            not alias_name
            or not canonical_name
            or canonical_name not in parameter_names
            or alias_name in parameter_names
        ):
            raise ValueError(
                f"Invalid argument alias for {self.name}: "
                f"{alias_name or '<empty>'}->{canonical_name or '<empty>'}"
            )
        existing = self.argument_aliases.get(alias_name)
        if existing is not None and existing != canonical_name:
            raise ValueError(
                f"Conflicting argument alias for {self.name}: {alias_name}"
            )
        self.argument_aliases[alias_name] = canonical_name

    def normalize_arguments(
        self,
        arguments: Mapping[str, Any] | None,
    ) -> Dict[str, Any]:
        """Validate and canonicalize provider/model arguments.

        Unknown keys and conflicting aliases are rejected before the backing
        Python callable runs.
        """

        if arguments is None:
            raw: Dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            raw = dict(arguments)
        else:
            raise ToolArgumentValidationError(
                self.name,
                code="tool_argument_invalid",
            )

        if any(not isinstance(key, str) for key in raw):
            raise ToolArgumentValidationError(
                self.name,
                code="tool_argument_unknown",
                argument_names=("<non-string>",),
            )

        # There is no finite key set to validate for this narrow legacy shape.
        # Explicit and inferred schemas never enter this branch, so unknown-key
        # rejection remains authoritative everywhere a real schema exists.
        if self._legacy_variadic_kwargs:
            return raw

        canonical_names = {param.name for param in self.parameters}
        alias_names = set(self.argument_aliases)
        all_hidden_names = {
            str(name or "").strip()
            for name in self.hidden_argument_names
            if str(name or "").strip()
        }
        # Hidden arguments are consumed by a trusted boundary wrapper (for
        # example a TurnContext binder) and intentionally omitted from the
        # provider schema.  They still need to pass through this boundary so
        # the wrapper can validate/reject forged values itself.
        hidden_names = all_hidden_names - canonical_names - alias_names
        invalid_aliases = [
            alias
            for alias, canonical in self.argument_aliases.items()
            if canonical not in canonical_names or alias in canonical_names
        ]
        if invalid_aliases:
            raise ToolArgumentValidationError(
                self.name,
                code="tool_argument_invalid",
                argument_names=invalid_aliases,
            )

        unknown = sorted(set(raw) - canonical_names - alias_names - hidden_names)
        if unknown:
            raise ToolArgumentValidationError(
                self.name,
                code="tool_argument_unknown",
                argument_names=unknown,
            )

        normalized = dict(raw)
        for alias, canonical in sorted(self.argument_aliases.items()):
            if alias not in normalized:
                continue
            alias_value = normalized.pop(alias)
            if canonical in normalized:
                canonical_value = normalized[canonical]
                same_value = canonical_value == alias_value
                if isinstance(canonical_value, str) and isinstance(
                    alias_value,
                    str,
                ):
                    same_value = canonical_value.strip() == alias_value.strip()
                if not same_value:
                    raise ToolArgumentValidationError(
                        self.name,
                        code="tool_argument_conflict",
                        argument_names=(canonical, alias),
                    )
                continue
            normalized[canonical] = alias_value

        missing = [
            param.name
            for param in self.parameters
            if param.required
            and param.name not in normalized
            and param.name not in all_hidden_names
        ]
        if missing:
            raise ToolArgumentValidationError(
                self.name,
                code="tool_argument_missing",
                argument_names=missing,
            )
        return normalized

    def to_json_schema(self) -> Dict[str, Any]:
        """標準 JSON Schema フォーマットでパラメータ定義を返す"""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        hidden_names = {
            str(name or "").strip()
            for name in self.hidden_argument_names
            if str(name or "").strip()
        }
        for p in self.parameters:
            # Hidden arguments are trusted server-side inputs.  They remain
            # accepted by ``normalize_arguments`` for the wrapper boundary,
            # but must never be advertised to a model/provider schema.
            if p.name in hidden_names:
                continue
            prop: Dict[str, Any] = deepcopy(p.schema) if p.schema else {"type": p.type}
            prop.setdefault("type", p.type)
            if p.description:
                prop.setdefault("description", p.description)
            if p.enum and "enum" not in prop:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        for alias, canonical in sorted(self.argument_aliases.items()):
            canonical_schema = properties.get(canonical)
            if canonical_schema is None:
                continue
            alias_schema = deepcopy(canonical_schema)
            existing_description = str(
                alias_schema.get("description") or ""
            ).strip()
            compatibility_note = (
                f"Compatibility alias for `{canonical}`. Prefer `{canonical}`."
            )
            alias_schema["description"] = (
                f"{existing_description} {compatibility_note}".strip()
            )
            properties[alias] = alias_schema

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": bool(self._legacy_variadic_kwargs),
        }

    def execute(self, **kwargs) -> Any:
        """ツールを実行（全バックエンドで共通）"""
        normalized = self.normalize_arguments(kwargs)
        result = self.function(**normalized)
        if asyncio.iscoroutine(result):
            # 非同期関数の場合、イベントループで実行
            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures
                # ``asyncio.run`` in a fresh worker thread would otherwise
                # start with an empty ContextVar context.  Tool execution is
                # still part of the same logical turn, so copy the current
                # context (including TurnContext/task/reference scope) into
                # that thread before driving the coroutine.
                current_context = contextvars.copy_context()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(current_context.run, asyncio.run, result)
                    return future.result()
            except RuntimeError:
                return asyncio.run(result)
        return result

    async def execute_async(self, **kwargs) -> Any:
        """ツールを非同期実行

        同期ツールは必ずワーカースレッドへ逃がす。イベントループ上で直接呼ぶと、
        重いコマンドやユーザー承認待ちの間ループ全体が停止し、SSE配信も
        承認応答の受信も止まって実質フリーズする。
        """
        normalized = self.normalize_arguments(kwargs)
        if self.is_async or inspect.iscoroutinefunction(self.function):
            return await self._await_with_timeout(self.function(**normalized))

        return await self._await_with_timeout(
            asyncio.to_thread(lambda: self.function(**normalized))
        )

    async def _await_with_timeout(self, awaitable: Any) -> Any:
        """timeout_seconds が設定されていれば外側からも打ち切る。"""
        if asyncio.iscoroutine(awaitable) or asyncio.isfuture(awaitable):
            if self.timeout_seconds and self.timeout_seconds > 0:
                return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)
            return await awaitable
        return awaitable


# Python型 → JSON Schema型のマッピング
_TYPE_MAP: Dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _resolve_type(py_type: Any) -> tuple:
    """Python型からJSON Schema型と optional フラグを解決する"""
    origin = get_origin(py_type)

    # Optional[X] = Union[X, None] の処理
    if origin is Union:
        args = get_args(py_type)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner_type, _ = _resolve_type(non_none[0])
            return inner_type, True
        return "string", True

    # List[str] / Dict[str, Any] のような generic alias は origin で判定する。
    # ここを素の _TYPE_MAP 参照にすると list 系が全て "string" に落ち、
    # モデルが配列引数を文字列で送って必ず0件になる。
    if origin is not None:
        return _TYPE_MAP.get(origin, "string"), False

    return _TYPE_MAP.get(py_type, "string"), False


def _resolve_param_schema(py_type: Any, json_type: str) -> Optional[Dict[str, Any]]:
    """配列パラメータに items を付けて、要素型までモデルへ伝える。"""
    if json_type != "array":
        return None

    target = py_type
    if get_origin(target) is Union:
        non_none = [a for a in get_args(target) if a is not type(None)]
        if len(non_none) == 1:
            target = non_none[0]

    item_args = get_args(target)
    item_type = "string"
    if item_args:
        item_type = _TYPE_MAP.get(get_origin(item_args[0]) or item_args[0], "string")
    return {"type": "array", "items": {"type": item_type}}


def _extract_param_descriptions(docstring: str) -> Dict[str, str]:
    """docstring の Args セクションからパラメータの説明を抽出する"""
    descriptions: Dict[str, str] = {}
    if not docstring:
        return descriptions

    in_args = False
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if stripped.lower().startswith("returns:") or stripped.lower().startswith("raises:"):
                break
            if ":" in stripped and not stripped.startswith("-"):
                # "param_name: description" or "param_name (type): description"
                param_part, _, desc = stripped.partition(":")
                param_name = param_part.strip().split("(")[0].strip().split(" ")[0].strip()
                if param_name:
                    descriptions[param_name] = desc.strip()
            elif stripped.startswith("-"):
                # "- param_name: description"
                content = stripped.lstrip("- ")
                if ":" in content:
                    param_part, _, desc = content.partition(":")
                    param_name = param_part.strip().split("(")[0].strip().split(" ")[0].strip()
                    if param_name:
                        descriptions[param_name] = desc.strip()

    return descriptions


def _extract_description(docstring: str) -> str:
    """docstring から関数の説明（Args セクションより前）を抽出する"""
    if not docstring:
        return ""
    lines = []
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            break
        lines.append(stripped)
    return " ".join(line for line in lines if line).strip()


def tool(fn: Callable) -> "ToolDefinition":
    """@tool デコレータ — 関数から ToolDefinition を自動生成する

    type hints と docstring からパラメータ情報を自動抽出。

    Usage::

        @tool
        def get_current_time() -> str:
            \"\"\"現在時刻を取得する\"\"\"
            ...
    """
    if isinstance(fn, ToolDefinition):
        return fn
    if not callable(fn):
        raise TypeError(f"@tool expects a callable, got {type(fn)}")

    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    raw_doc = inspect.getdoc(fn) or ""
    param_docs = _extract_param_descriptions(raw_doc)
    description = _extract_description(raw_doc) or fn.__name__

    params: List[ToolParam] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue

        py_type = hints.get(name, str)
        json_type, is_optional = _resolve_type(py_type)
        has_default = param.default is not inspect.Parameter.empty

        params.append(
            ToolParam(
                name=name,
                type=json_type,
                description=param_docs.get(name, ""),
                required=not (is_optional or has_default),
                default=param.default if has_default else None,
                schema=_resolve_param_schema(py_type, json_type),
            )
        )

    return ToolDefinition(
        name=fn.__name__,
        description=description,
        function=fn,
        parameters=params,
        is_async=inspect.iscoroutinefunction(fn),
    )


def ensure_tool_definition(value: Any) -> ToolDefinition:
    """関数または ToolDefinition を ToolDefinition に正規化する。"""
    if isinstance(value, ToolDefinition):
        return value
    if callable(value):
        return tool(value)
    raise TypeError(f"Expected ToolDefinition or callable, got {type(value)}")


def ensure_tool_definitions(values: Iterable[Any]) -> List[ToolDefinition]:
    """複数ツールを ToolDefinition リストへ正規化する。"""
    return [ensure_tool_definition(value) for value in values]
