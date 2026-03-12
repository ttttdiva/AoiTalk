"""
バックエンド非依存のツール定義

OpenAI Agents SDK, Gemini API, CLI, SGLang 等のどのバックエンドでも
使える統一的なツール定義を提供する。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union, get_type_hints
import asyncio
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


@dataclass
class ToolDefinition:
    """バックエンド非依存のツール定義"""
    name: str
    description: str
    function: Callable
    parameters: List[ToolParam] = field(default_factory=list)
    is_async: bool = False

    def to_json_schema(self) -> Dict[str, Any]:
        """標準 JSON Schema フォーマットでパラメータ定義を返す"""
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for p in self.parameters:
            prop: Dict[str, Any] = {"type": p.type}
            if p.description:
                prop["description"] = p.description
            if p.enum:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def execute(self, **kwargs) -> Any:
        """ツールを実行（全バックエンドで共通）"""
        result = self.function(**kwargs)
        if asyncio.iscoroutine(result):
            # 非同期関数の場合、イベントループで実行
            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, result)
                    return future.result()
            except RuntimeError:
                return asyncio.run(result)
        return result

    async def execute_async(self, **kwargs) -> Any:
        """ツールを非同期実行"""
        result = self.function(**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


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
    origin = getattr(py_type, "__origin__", None)

    # Optional[X] = Union[X, None] の処理
    if origin is Union:
        args = py_type.__args__
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _TYPE_MAP.get(non_none[0], "string"), True
        return "string", True

    return _TYPE_MAP.get(py_type, "string"), False


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
            )
        )

    return ToolDefinition(
        name=fn.__name__,
        description=description,
        function=fn,
        parameters=params,
        is_async=inspect.iscoroutinefunction(fn),
    )
