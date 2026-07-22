"""共通読み辞書・未解決候補のDBアクセス。"""

import uuid
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import desc

from ...memory.database import get_database_manager
from ...memory.models import (
    YomiDictionaryEntry,
    YomiDictionarySync,
    YomiUnresolvedCandidate,
)


class YomiRepository:
    def list_dictionary(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with get_database_manager().get_sync_session() as session:
            query = session.query(YomiDictionaryEntry)
            if enabled_only:
                query = query.filter(YomiDictionaryEntry.enabled.is_(True))
            return [row.to_dict() for row in query.order_by(YomiDictionaryEntry.surface).all()]

    def create_dictionary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        row = YomiDictionaryEntry(**data)
        with get_database_manager().get_sync_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    def update_dictionary(self, entry_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with get_database_manager().get_sync_session() as session:
            row = session.get(YomiDictionaryEntry, uuid.UUID(entry_id))
            if row is None:
                return None
            for key, value in data.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    def delete_dictionary(self, entry_id: str) -> bool:
        with get_database_manager().get_sync_session() as session:
            row = session.get(YomiDictionaryEntry, uuid.UUID(entry_id))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def list_syncs(self, tts_engine: str, base_url: str) -> List[Dict[str, Any]]:
        with get_database_manager().get_sync_session() as session:
            rows = session.query(YomiDictionarySync).filter(
                YomiDictionarySync.tts_engine == tts_engine,
                YomiDictionarySync.base_url == base_url,
            ).all()
            return [row.to_dict() for row in rows]

    def upsert_sync(self, data: Dict[str, Any]) -> Dict[str, Any]:
        entry_id = uuid.UUID(str(data["dictionary_entry_id"]))
        with get_database_manager().get_sync_session() as session:
            row = session.query(YomiDictionarySync).filter(
                YomiDictionarySync.dictionary_entry_id == entry_id,
                YomiDictionarySync.tts_engine == data["tts_engine"],
                YomiDictionarySync.base_url == data["base_url"],
            ).first()
            if row is None:
                row = YomiDictionarySync(dictionary_entry_id=entry_id)
                session.add(row)
            for key, value in data.items():
                if key != "dictionary_entry_id":
                    setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row.to_dict()

    def delete_sync(self, sync_id: str) -> None:
        with get_database_manager().get_sync_session() as session:
            row = session.get(YomiDictionarySync, uuid.UUID(sync_id))
            if row is not None:
                session.delete(row)
                session.commit()

    def list_candidates(self, status: Optional[str] = "unresolved") -> List[Dict[str, Any]]:
        with get_database_manager().get_sync_session() as session:
            query = session.query(YomiUnresolvedCandidate)
            if status:
                query = query.filter(YomiUnresolvedCandidate.status == status)
            rows = query.order_by(desc(YomiUnresolvedCandidate.updated_at)).limit(250).all()
            return [row.to_dict() for row in rows]

    def update_candidate(self, candidate_id: str, status: str) -> Optional[Dict[str, Any]]:
        with get_database_manager().get_sync_session() as session:
            row = session.get(YomiUnresolvedCandidate, uuid.UUID(candidate_id))
            if row is None:
                return None
            row.status = status
            session.commit()
            session.refresh(row)
            return row.to_dict()

    def record_candidates(
        self,
        *,
        original_text: str,
        detections: Iterable[Any],
        model_id: str,
        tts_engine: str,
        final_text: str,
        dictionary_surfaces: set[str],
        dictionary_applied: bool,
    ) -> None:
        with get_database_manager().get_sync_session() as session:
            for detection in detections:
                if detection.surface in dictionary_surfaces:
                    continue
                existing = (
                    session.query(YomiUnresolvedCandidate)
                    .filter(
                        YomiUnresolvedCandidate.detected_text == detection.surface,
                        YomiUnresolvedCandidate.model_id == model_id,
                        YomiUnresolvedCandidate.tts_engine == tts_engine,
                        YomiUnresolvedCandidate.status == "unresolved",
                    )
                    .first()
                )
                if existing:
                    existing.occurrence_count += 1
                    existing.confidence = max(existing.confidence, detection.confidence)
                    existing.original_text = original_text
                    existing.final_text = final_text
                    continue
                session.add(
                    YomiUnresolvedCandidate(
                        original_text=original_text,
                        detected_text=detection.surface,
                        start_offset=detection.start,
                        end_offset=detection.end,
                        confidence=detection.confidence,
                        model_id=model_id,
                        tts_engine=tts_engine,
                        dictionary_applied=dictionary_applied,
                        final_text=final_text,
                    )
                )
            session.commit()
