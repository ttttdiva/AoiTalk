"""
Feedback management module for AoiTalk (api layer facade).

保存/読み込みの実装は services 層 (``src/services/feedback_store.py``) へ移設した。
services→api の層逆流を解消するための反転であり、本モジュールは従来どおりの公開
API を維持するために services 層の実装をそのまま re-export する。
"""

from ..services.feedback_store import (
    DATABASE_AVAILABLE,
    Feedback,
    FeedbackEntry,
    FeedbackRepository,
    FeedbackRequest,
    _generate_feedback_id,
    _get_feedback_file_path,
    _load_feedback_jsonl_fallback,
    _mark_feedback_resolved_jsonl_fallback,
    _save_feedback_jsonl_fallback,
    load_feedback,
    load_feedback_async,
    mark_feedback_resolved,
    mark_feedback_resolved_async,
    migrate_jsonl_to_database,
    save_feedback,
    save_feedback_async,
)

__all__ = [
    "DATABASE_AVAILABLE",
    "Feedback",
    "FeedbackEntry",
    "FeedbackRepository",
    "FeedbackRequest",
    "load_feedback",
    "load_feedback_async",
    "mark_feedback_resolved",
    "mark_feedback_resolved_async",
    "migrate_jsonl_to_database",
    "save_feedback",
    "save_feedback_async",
]
