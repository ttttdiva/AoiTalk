"""Sync v3 の応答ページ組み立て（DBを使わない純粋関数）。

changes は (commit_seq, seq) 順の raw 変更を1件ずつ処理し、byte予算で切る。
cursor は「そのページで返した entity に必ず含まれている raw の位置」までしか進めない。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .contract import ChangeRef, LoadState

_OP_BY_STATE: dict[str, str] = {"row": "upsert", "missing": "delete", "hidden": "hide"}

LoadedEntry = tuple[LoadState, "dict[str, Any] | None", int, "list[str]"]


def encoded_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def make_change(table: str, entity_id: str, entry: LoadedEntry) -> dict[str, Any]:
    state, payload, version, scope_keys = entry
    op = _OP_BY_STATE[state]
    return {
        "table": table,
        "entity_id": entity_id,
        "op": op,
        "row": payload if state == "row" else None,
        "row_version": str(int(version)),
        "scope_keys": list(scope_keys) if state == "row" else [],
    }


def build_changes_page(
    raw: Iterable[ChangeRef],
    loaded: Mapping[tuple[str, str], LoadedEntry],
    *,
    byte_budget: int,
) -> tuple[list[dict[str, Any]], tuple[int, int] | None, int]:
    """(changes, last_covered, 含まれた raw の件数) を返す。

    raw は (commit_seq, seq) 順に処理する。既にこのページで返した entity の raw は
    追加の容量を使わずに「含まれた」とする。未返却の entity は change を作り、
    合計が予算を超え、かつ既に1件以上返していればそこで停止する（その raw は含めない）。
    0件目は予算を超えても必ず返すため、ページの進捗が0になることはない。
    """
    ordered = sorted(raw, key=lambda ref: (ref.commit_seq, ref.seq))
    changes: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    total = 0
    last_covered: tuple[int, int] | None = None
    covered = 0
    for ref in ordered:
        key = (ref.entity_table, ref.entity_id)
        if key not in emitted:
            change = make_change(ref.entity_table, ref.entity_id, loaded[key])
            size = encoded_size(change)
            if changes and total + size > byte_budget:
                break
            changes.append(change)
            emitted.add(key)
            total += size
        last_covered = (ref.commit_seq, ref.seq)
        covered += 1
    return changes, last_covered, covered


def build_bootstrap_page(
    rows: Iterable[tuple[str, "dict[str, Any]", int]],
    *,
    byte_budget: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """(emitted, last_entity_id)。0件目は必ず返し、予算超過で停止する。"""
    emitted: list[dict[str, Any]] = []
    total = 0
    last_entity_id: str | None = None
    for entity_id, payload, version in rows:
        item = {"entity_id": entity_id, "row": payload, "row_version": str(int(version))}
        size = encoded_size(item)
        if emitted and total + size > byte_budget:
            break
        emitted.append(item)
        total += size
        last_entity_id = entity_id
    return emitted, last_entity_id
