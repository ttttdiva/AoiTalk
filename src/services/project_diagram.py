"""案件情報（タスク階層・record table）からMermaid構成図を組み立てる純粋関数群。"""

from __future__ import annotations

import re
from typing import Any

FROM_FIELD_KEYWORDS = ("接続元", "始点", "上位", "from", "source", "src")
TO_FIELD_KEYWORDS = ("接続先", "終点", "下位", "to", "target", "dst", "destination")
EDGE_LABEL_FIELD_KEYWORDS = (
    "用途",
    "回線",
    "vlan",
    "ポート",
    "port",
    "プロトコル",
    "protocol",
    "ラベル",
    "label",
    "種別",
)
GROUP_FIELD_KEYWORDS = (
    "分類",
    "カテゴリ",
    "category",
    "区分",
    "拠点",
    "場所",
    "location",
    "グループ",
    "group",
    "種別",
)
CONNECTION_TABLE_KEYWORDS = ("接続", "connection", "配線", "リンク", "link")
DEVICE_TABLE_KEYWORDS = (
    "機器",
    "装置",
    "device",
    "server",
    "サーバ",
    "ノード",
    "node",
    "構成要素",
    "環境一覧",
)

MAX_NODES = 80
MAX_LABEL_LENGTH = 60


def mermaid_escape(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace('"', "'").replace("[", "(").replace("]", ")")
    if len(text) > MAX_LABEL_LENGTH:
        text = text[: MAX_LABEL_LENGTH - 1] + "…"
    return text


class _MermaidNodes:
    """ラベル→node idの採番と定義行の振り分けを行う。"""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}
        self.top_level: list[str] = []

    def ref(self, label: str, bucket: list[str] | None = None) -> str:
        label = label or "(無題)"
        node_id = self._ids.get(label)
        if node_id is None:
            node_id = f"n{len(self._ids)}"
            self._ids[label] = node_id
            (bucket if bucket is not None else self.top_level).append(
                f'{node_id}["{label}"]'
            )
        return node_id

    def __len__(self) -> int:
        return len(self._ids)


def _field_text(field: dict[str, Any]) -> str:
    return f"{field.get('label') or ''} {field.get('key') or ''}".casefold()


def _match_field(
    fields: list[dict[str, Any]], keywords: tuple[str, ...]
) -> dict[str, Any] | None:
    for field in fields:
        text = _field_text(field)
        if any(keyword in text for keyword in keywords):
            return field
    return None


def _row_value(row: dict[str, Any], field: dict[str, Any] | None) -> str:
    if not field:
        return ""
    values = row.get("values") or {}
    return str(values.get(field.get("key")) or "").strip()


def _row_label(row: dict[str, Any], fields: list[dict[str, Any]]) -> str:
    title = str(row.get("title") or "").strip()
    if title:
        return title
    title_field = next((f for f in fields if f.get("is_title")), None)
    value = _row_value(row, title_field)
    if value:
        return value
    for field in fields:
        value = _row_value(row, field)
        if value:
            return value
    return ""


def table_kind(name: str) -> str:
    text = str(name or "").casefold()
    if any(keyword in text for keyword in CONNECTION_TABLE_KEYWORDS):
        return "connection"
    if any(keyword in text for keyword in DEVICE_TABLE_KEYWORDS):
        return "device"
    return ""


def _status_class(status: Any) -> str:
    value = str(status or "").casefold()
    if any(k in value for k in ("done", "completed", "closed", "完了")):
        return "done"
    if any(k in value for k in ("in_progress", "doing", "active", "進行", "着手")):
        return "wip"
    return "todo"


def build_wbs_diagram(
    project_name: str,
    tasks: list[dict[str, Any]],
    max_nodes: int = MAX_NODES,
) -> str | None:
    if not tasks:
        return None
    lines = ["graph TD"]
    lines.append(f'    root(["{mermaid_escape(project_name) or "Project"}"])')
    ids: dict[str, str] = {}
    truncated = False
    for index, task in enumerate(tasks):
        if len(ids) >= max_nodes:
            truncated = True
            break
        ids[str(task.get("id"))] = f"t{index}"
    by_class: dict[str, list[str]] = {"done": [], "wip": [], "todo": []}
    for task in tasks:
        node = ids.get(str(task.get("id")))
        if node is None:
            continue
        label = mermaid_escape(task.get("title")) or "(無題)"
        lines.append(f'    {node}["{label}"]')
        by_class[_status_class(task.get("status"))].append(node)
    for task in tasks:
        node = ids.get(str(task.get("id")))
        if node is None:
            continue
        parent = ids.get(str(task.get("parent_task_id") or ""))
        lines.append(f"    {parent or 'root'} --> {node}")
    lines.append("    classDef done fill:#d3f9d8,stroke:#2b8a3e")
    lines.append("    classDef wip fill:#fff3bf,stroke:#e67700")
    lines.append("    classDef todo fill:#f1f3f5,stroke:#868e96")
    for klass, nodes in by_class.items():
        if nodes:
            lines.append(f"    class {','.join(nodes)} {klass}")
    if truncated:
        lines.append(f"    %% タスクが多いため先頭 {max_nodes} 件のみ表示")
    return "\n".join(lines)


def _build_edge_lines(
    rows: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    from_field: dict[str, Any],
    to_field: dict[str, Any],
    nodes: _MermaidNodes,
    max_nodes: int,
) -> tuple[list[str], bool]:
    label_field = next(
        (
            field
            for field in fields
            if field is not from_field
            and field is not to_field
            and any(k in _field_text(field) for k in EDGE_LABEL_FIELD_KEYWORDS)
        ),
        None,
    )
    edges: list[str] = []
    truncated = False
    for row in rows:
        src = mermaid_escape(_row_value(row, from_field))
        dst = mermaid_escape(_row_value(row, to_field))
        if not src or not dst:
            continue
        if len(nodes) >= max_nodes:
            truncated = True
            break
        src_id = nodes.ref(src)
        dst_id = nodes.ref(dst)
        edge_label = mermaid_escape(_row_value(row, label_field))
        if edge_label:
            edges.append(f"    {src_id} -->|{edge_label}| {dst_id}")
        else:
            edges.append(f"    {src_id} --> {dst_id}")
    return edges, truncated


def _build_grouped_lines(
    table_name: str,
    rows: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    nodes: _MermaidNodes,
    max_nodes: int,
    subgraph_prefix: str,
) -> tuple[list[str], bool]:
    group_field = next(
        (field for field in fields if field.get("field_type") == "select"), None
    ) or _match_field(fields, GROUP_FIELD_KEYWORDS)
    groups: dict[str, list[str]] = {}
    truncated = False
    for row in rows:
        label = mermaid_escape(_row_label(row, fields))
        if not label:
            continue
        if len(nodes) >= max_nodes:
            truncated = True
            break
        group = mermaid_escape(_row_value(row, group_field)) or mermaid_escape(
            table_name
        )
        groups.setdefault(group, [])
        nodes.ref(label, groups[group])
    lines: list[str] = []
    for index, (group, definitions) in enumerate(groups.items()):
        if not definitions:
            continue
        lines.append(f'    subgraph {subgraph_prefix}{index}["{group}"]')
        lines.extend(f"        {definition}" for definition in definitions)
        lines.append("    end")
    return lines, truncated


def build_record_table_diagram(
    table_name: str,
    fields: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    max_nodes: int = MAX_NODES,
) -> str | None:
    if not rows:
        return None
    nodes = _MermaidNodes()
    from_field = _match_field(fields, FROM_FIELD_KEYWORDS)
    to_field = _match_field(fields, TO_FIELD_KEYWORDS)
    if from_field and to_field and from_field is not to_field:
        edges, truncated = _build_edge_lines(
            rows, fields, from_field, to_field, nodes, max_nodes
        )
        if edges:
            lines = ["graph LR"]
            lines.extend(f"    {definition}" for definition in nodes.top_level)
            lines.extend(edges)
            if truncated:
                lines.append(f"    %% ノード上限 {max_nodes} 件で省略")
            return "\n".join(lines)
    group_lines, truncated = _build_grouped_lines(
        table_name, rows, fields, nodes, max_nodes, "g"
    )
    if not group_lines:
        return None
    lines = ["graph TB"]
    lines.extend(group_lines)
    if truncated:
        lines.append(f"    %% ノード上限 {max_nodes} 件で省略")
    return "\n".join(lines)


def build_system_diagram(
    project_name: str,
    tables: list[dict[str, Any]],
    max_nodes: int = MAX_NODES,
) -> str | None:
    """機器/接続系record tableを統合した全体構成図を作る。

    tables: {"name": str, "fields": [...], "rows": [...]} のリスト。
    """
    connection_tables = [t for t in tables if table_kind(t.get("name", "")) == "connection"]
    device_tables = [t for t in tables if table_kind(t.get("name", "")) == "device"]
    if not connection_tables and not device_tables:
        return None
    nodes = _MermaidNodes()
    subgraph_lines: list[str] = []
    edge_lines: list[str] = []
    truncated = False
    for index, table in enumerate(device_tables):
        lines, was_truncated = _build_grouped_lines(
            table.get("name", ""),
            table.get("rows") or [],
            table.get("fields") or [],
            nodes,
            max_nodes,
            f"d{index}_",
        )
        subgraph_lines.extend(lines)
        truncated = truncated or was_truncated
    for table in connection_tables:
        fields = table.get("fields") or []
        from_field = _match_field(fields, FROM_FIELD_KEYWORDS)
        to_field = _match_field(fields, TO_FIELD_KEYWORDS)
        if not from_field or not to_field or from_field is to_field:
            continue
        edges, was_truncated = _build_edge_lines(
            table.get("rows") or [], fields, from_field, to_field, nodes, max_nodes
        )
        edge_lines.extend(edges)
        truncated = truncated or was_truncated
    if not subgraph_lines and not edge_lines:
        return None
    lines = ["graph LR"]
    title = mermaid_escape(project_name)
    if title:
        lines.append(f"    %% {title} 構成図")
    lines.extend(subgraph_lines)
    lines.extend(f"    {definition}" for definition in nodes.top_level)
    lines.extend(edge_lines)
    if truncated:
        lines.append(f"    %% ノード上限 {max_nodes} 件で省略")
    return "\n".join(lines)
