"""TTS固有の安全な辞書反映アダプター。原文は書き換えない。"""

import asyncio
import time
from typing import Any, Dict, Iterable, Set, Tuple

import httpx

from ...services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)


class VoicevoxCompatibleDictionaryAdapter:
    """明示語を同期し、AoiTalkが所有するremote UUIDだけを削除する。"""

    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._snapshots: Dict[Tuple[str, str], Tuple[Tuple[Any, ...], ...]] = {}
        self._snapshot_at: Dict[Tuple[str, str], float] = {}
        self._snapshot_ttl_seconds = 60.0
        self._fulfilled: Dict[Tuple[str, str], Set[str]] = {}
        self._known_clean: Set[Tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _gateway(engine: Any, base_url: str) -> OutboundPrivacyGateway:
        """Create a policy-scoped gateway for configurable VoiceVox routes."""

        config = getattr(engine, "config", None)
        if config is None:
            try:
                from ...config import Config

                config = Config()
            except Exception as exc:
                raise PrivacyError("VoiceVox dictionary privacy configuration is unavailable") from exc
        context = get_privacy_policy_context()
        session = context.session_context or {}
        return OutboundPrivacyGateway(
            config,
            user_id=str(session.get("user_id") or getattr(engine, "session_user_id", "") or ""),
            session_id=str(session.get("session_id") or session.get("id") or getattr(engine, "current_session_id", "") or ""),
            session_context=context.session_context,
            project_metadata=context.project_metadata,
        )

    @classmethod
    async def _request(
        cls,
        engine: Any,
        client: httpx.AsyncClient,
        base_url: str,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform exactly one dictionary HTTP operation behind the gateway."""

        normalized_method = str(method or "GET").upper()
        request = {
            "method": normalized_method,
            "path": str(path or ""),
            "params": dict(params or {}),
        }
        gateway = cls._gateway(engine, base_url)

        async def sender(final_payload: Any) -> httpx.Response:
            if not isinstance(final_payload, dict):
                raise PrivacyError("VoiceVox dictionary payload is malformed")
            if str(final_payload.get("method") or "").upper() != normalized_method:
                raise PrivacyError("VoiceVox dictionary method binding changed")
            if str(final_payload.get("path") or "") != str(path or ""):
                raise PrivacyError("VoiceVox dictionary destination binding changed")
            final_params = final_payload.get("params")
            if not isinstance(final_params, dict):
                raise PrivacyError("VoiceVox dictionary parameters are malformed")
            request_method = getattr(client, normalized_method.lower(), None)
            if not callable(request_method):
                raise PrivacyError("VoiceVox dictionary HTTP method is unsupported")
            return await request_method(str(path or ""), params=final_params)

        return await gateway.execute(
            request,
            provider="voicevox",
            descriptor=EgressDescriptor(
                action=f"voicevox.dictionary.{normalized_method.lower()}",
                transport="httpx.AsyncClient",
                destination=f"{base_url}{path}",
                provider="voicevox",
                tool="tts.yomi_linter",
            ),
            base_url=base_url,
            source_kind="voicevox_dictionary",
            sender=sender,
        )

    async def apply(
        self,
        engine: Any,
        entries: Iterable[Dict[str, Any]],
        *,
        engine_name: str,
        applicable_entry_ids: Set[str],
    ) -> bool:
        base_url = str(getattr(engine, "base_url", "")).rstrip("/")
        if not base_url:
            return False
        entries = list(entries)
        target = (engine_name, base_url)
        self._known_clean.discard(target)
        signature = tuple(sorted(
            (
                str(entry["id"]), entry["surface"], entry["reading"],
                int(entry.get("accent_type") or 0),
            )
            for entry in entries
        ))
        snapshot_fresh = (
            time.monotonic() - self._snapshot_at.get(target, 0.0)
            <= self._snapshot_ttl_seconds
        )
        if self._snapshots.get(target) == signature and snapshot_fresh:
            return bool(applicable_entry_ids) and applicable_entry_ids.issubset(
                self._fulfilled.get(target, set())
            )

        async with self._lock:
            snapshot_fresh = (
                time.monotonic() - self._snapshot_at.get(target, 0.0)
                <= self._snapshot_ttl_seconds
            )
            if self._snapshots.get(target) == signature and snapshot_fresh:
                return bool(applicable_entry_ids) and applicable_entry_ids.issubset(
                    self._fulfilled.get(target, set())
                )
            syncs = await asyncio.to_thread(
                self.repository.list_syncs, engine_name, base_url
            )
            desired = {str(entry["id"]): entry for entry in entries}
            fulfilled: Set[str] = set()
            async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                response = await self._request(
                    engine,
                    client,
                    base_url,
                    "GET",
                    "/user_dict",
                )
                response.raise_for_status()
                remote = response.json() or {}
                remote_words = remote if isinstance(remote, dict) else {}

                # 無効化・削除・編集された項目は、AoiTalkが追加したUUIDだけを除去する。
                current_syncs: Dict[str, Dict[str, Any]] = {}
                for sync in syncs:
                    entry_id = str(sync["dictionary_entry_id"])
                    entry = desired.get(entry_id)
                    remote_word = remote_words.get(str(sync["remote_word_uuid"]))
                    same = bool(entry) and (
                        sync["surface"] == entry["surface"]
                        and sync["reading"] == entry["reading"]
                        and int(sync.get("accent_type") or 0)
                        == int(entry.get("accent_type") or 0)
                        and bool(remote_word)
                        and remote_word.get("surface") == entry["surface"]
                        and remote_word.get("pronunciation") == entry["reading"]
                        and int(remote_word.get("accent_type") or 0)
                        == int(entry.get("accent_type") or 0)
                    )
                    if not same:
                        deleted = await self._request(
                            engine,
                            client,
                            base_url,
                            "DELETE",
                            f"/user_dict_word/{sync['remote_word_uuid']}",
                        )
                        if deleted.status_code not in {204, 404}:
                            deleted.raise_for_status()
                        await asyncio.to_thread(
                            self.repository.delete_sync, sync["id"]
                        )
                    else:
                        current_syncs[entry_id] = sync
                        fulfilled.add(entry_id)

                for entry_id, entry in desired.items():
                    if entry_id in current_syncs:
                        continue
                    accent = int(entry.get("accent_type") or 0)
                    exact_remote_uuid = next((
                        str(word_uuid)
                        for word_uuid, word in remote_words.items()
                        if word.get("surface") == entry["surface"]
                        and word.get("pronunciation") == entry["reading"]
                        and int(word.get("accent_type") or 0) == accent
                    ), None)
                    if exact_remote_uuid is None:
                        created = await self._request(
                            engine,
                            client,
                            base_url,
                            "POST",
                            "/user_dict_word",
                            params={
                                "surface": entry["surface"],
                                "pronunciation": entry["reading"],
                                "accent_type": accent,
                                "word_type": "PROPER_NOUN",
                                "priority": 5,
                            },
                        )
                        created.raise_for_status()
                        try:
                            remote_uuid = str(created.json())
                        except Exception:
                            remote_uuid = created.text.strip().strip('"')
                        if not remote_uuid:
                            raise RuntimeError("ユーザー辞書APIが登録UUIDを返しませんでした")
                        await asyncio.to_thread(
                            self.repository.upsert_sync,
                            {
                                "dictionary_entry_id": entry_id,
                                "tts_engine": engine_name,
                                "base_url": base_url,
                                "remote_word_uuid": remote_uuid,
                                "surface": entry["surface"],
                                "reading": entry["reading"],
                                "accent_type": accent,
                            },
                        )
                    # 既存のユーザー所有語は満たされたとみなすが、所有権は取得しない。
                    fulfilled.add(entry_id)

            self._snapshots[target] = signature
            self._snapshot_at[target] = time.monotonic()
            self._fulfilled[target] = fulfilled
            return bool(applicable_entry_ids) and applicable_entry_ids.issubset(fulfilled)

    async def cleanup_owned(self, engine: Any, *, engine_name: str) -> None:
        """機能無効化時、AoiTalkが登録した語だけを外部辞書から除去する。"""
        base_url = str(getattr(engine, "base_url", "")).rstrip("/")
        if not base_url:
            return
        target = (engine_name, base_url)
        if target in self._known_clean:
            return
        async with self._lock:
            syncs = await asyncio.to_thread(
                self.repository.list_syncs, engine_name, base_url
            )
            if syncs:
                async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                    for sync in syncs:
                        deleted = await self._request(
                            engine,
                            client,
                            base_url,
                            "DELETE",
                            f"/user_dict_word/{sync['remote_word_uuid']}",
                        )
                        if deleted.status_code not in {204, 404}:
                            deleted.raise_for_status()
                        await asyncio.to_thread(
                            self.repository.delete_sync, sync["id"]
                        )
            self._snapshots.pop(target, None)
            self._snapshot_at.pop(target, None)
            self._fulfilled.pop(target, None)
            self._known_clean.add(target)
