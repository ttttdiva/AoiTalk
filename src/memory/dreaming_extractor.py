"""Extract Dreaming memory candidates from conversations."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..services.outbound_privacy_service import (
    EgressDescriptor,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You maintain AoiTalk Dreaming Memory and Project curation.
Return only a JSON array. Do not include Markdown or commentary.
Use only the current user message as evidence for durable memory facts. The assistant
response is context only for memory, but may support the answer of an explicitly marked
project_qa artifact. The extraction scope is only a hint; a deterministic server-side
router is authoritative. Set scope_intent to user for durable cross-project user facts,
project for explicit, durable facts/decisions/constraints/responsibilities/workflows,
rules, or stable configuration that belong to the active project, docs_candidate for
knowledge that should be reviewed as a Doc, or discard when nothing should be retained.
Never invent a project scope when no active project is available. Set
explicit_evidence=true only when the user intentionally states the durable fact or scope
(never for an inference). Project Q&A is a separate review-only artifact and must never
contain a transcript fragment. For every project memory item, emit a short stable
semantic_key for the durable concept; reuse that key when equivalent wording changes."""

EXTRACTION_PROMPT = """Extract durable user-state memory operations from the user message below.
Dreaming memories are canonical long-term notes for understanding the current user.

Save only:
- Stable user preferences, constraints, and response-style expectations
- Stable personal workflows, tools, or environment facts that apply across projects
- Explicit durable facts the user intentionally shared in the user message
- When an active Project is present, explicit durable Project-local facts, decisions,
  constraints, responsibilities/ownership, recurring workflows, rules, and stable
  environment or configuration needed for future work in that Project

Project scope rules:
- Use scope_intent=project only when Active Project bound to this turn is yes and the
  user explicitly states a durable fact about that Project.
- When Active Project bound to this turn is no, never emit a project memory and never
  downgrade project-specific text into a user memory; use docs_candidate or discard.

Update/delete when:
- The user explicitly corrects a saved memory
- The user explicitly asks to forget a saved memory
- The user explicitly asks to clear all memories

Do not save:
- Temporary plans, appointments, moods, or one-off tasks
- Project-specific details that are transient, one-off, incident-only, or not clearly
  about the currently active Project
- Project-scoped facts when an active Project cannot be safely identified (emit no user
  fallback; use docs_candidate only when a review artifact is appropriate)
- One-turn response instructions such as "today", "this time", or "for now"
- Guesses or uncertain inferences
- Passwords, API keys, secrets, or highly sensitive personal data
- Content already covered by the existing memories
- Facts that only appear in the assistant response
- General knowledge, search results, or assistant suggestions

Existing Dreaming memories:
{existing}

Current turn:
Active Project bound to this turn: {active_project}
User: {user_input}
Assistant response for context only, never as evidence:
{assistant_response}

Return only a JSON array. Do not wrap it in Markdown.
Each memory item must have:
- action: upsert / update / delete / delete_all
- memory_id: existing memory id for update/delete, or null
- content: concise single-sentence memory beginning with "The user ..." for
  user-scoped memories, or "This project ..." for Project-scoped memories
  (null for delete_all; for delete, include the memory being deleted when useful)
- memory_type: fact / preference / constraint / project / workflow / relationship / instruction
- title: short optional title, or null
- confidence: number from 0.0 to 1.0
- importance: integer from 1 to 10
- expires_at: ISO datetime or null
- reason: short extraction reason
- sensitivity: normal / private / secret
- evidence_span: exact substring from the User message proving the memory
- scope_intent: user / project / docs_candidate / discard
- explicit_evidence: true only when the user explicitly stated the durable fact or scope
- semantic_key: for Project items, a short stable identity for the durable concept
  (reuse the same key when wording changes); null for User items

You may additionally emit at most three semantic Project Q&A items when (and only when)
the turn contains a standalone, durable question about the active project that a future
reader would reasonably need. Do not emit Q&A for complaints, instructions, brainstorming,
transient chat, or text that is merely question-shaped. A Q&A item must have:
- artifact_type: project_qa
- action: upsert
- question: a concise rewritten project question (not a copied transcript fragment)
- answer: a concise answer only when it is supported by the assistant response, otherwise null
- answer_supported: true only when the assistant response actually answers the question
- confidence: number from 0.0 to 1.0
- importance: integer from 1 to 10
- evidence_span: exact substring from the User message that establishes the question
- answer_evidence_span: exact substring from the Assistant response when answer_supported=true
When no standalone durable project question exists, emit no project_qa item.

Return [] when there is nothing worth storing.
"""


class DreamingMemoryExtractor:
    """Extract long-term memory candidates from one completed turn."""

    async def extract(
        self,
        user_input: str,
        assistant_response: str,
        existing_memories: List[Any],
        llm_client: Any = None,
        user_id: str | None = None,
        session_id: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        if not user_input.strip():
            return []

        existing_text = self._format_existing_memories(existing_memories)
        project_scope = (
            dict(project_metadata)
            if isinstance(project_metadata, Mapping)
            else {}
        )
        # Expose only the trusted presence bit to the provider.  The Project
        # UUID and any other metadata remain server-side and are enforced by
        # the deterministic router/job gate.
        active_project = (
            "yes"
            if str(project_scope.get("project_id") or "").strip()
            else "no"
        )
        prompt = EXTRACTION_PROMPT.format(
            existing=existing_text,
            active_project=active_project,
            user_input=user_input,
            assistant_response=assistant_response[:800],
        )

        output = await self._call_current_llm(
            llm_client,
            prompt,
            user_id=user_id,
            session_id=session_id,
            session_context=session_context,
            project_metadata=project_metadata,
        )
        if output is not None:
            parsed = self._parse_response(output)
            if parsed is not None:
                return parsed

        logger.debug("[DreamingMemoryExtractor] extraction failed")
        return []

    def _format_existing_memories(self, existing_memories: List[Any]) -> str:
        if not existing_memories:
            return "(none)"

        lines: list[str] = []
        for memory in existing_memories:
            if isinstance(memory, dict):
                memory_id = memory.get("id") or memory.get("memory_id") or "unknown"
                memory_type = memory.get("memory_type") or "memory"
                title = memory.get("title")
                content = str(memory.get("content") or "").strip()
                if not content:
                    continue
                title_part = f", title={title}" if title else ""
                lines.append(
                    f"- id={memory_id}, type={memory_type}{title_part}: {content}"
                )
            else:
                content = str(memory or "").strip()
                if content:
                    lines.append(f"- {content}")
        return "\n".join(lines) if lines else "(none)"

    def _privacy_gateway(
        self,
        llm_client: Any,
        *,
        user_id: str | None,
        session_id: str | None,
        session_context: Mapping[str, Any] | None,
        project_metadata: Mapping[str, Any] | None,
    ) -> OutboundPrivacyGateway:
        inherited = get_privacy_policy_context()
        resolved_session = (
            dict(session_context)
            if isinstance(session_context, Mapping)
            else dict(inherited.session_context or {})
        )
        resolved_project = (
            dict(project_metadata)
            if isinstance(project_metadata, Mapping)
            else dict(inherited.project_metadata or {})
        )
        return OutboundPrivacyGateway(
            getattr(llm_client, "config", None),
            user_id=str(user_id or ""),
            session_id=str(session_id or ""),
            session_context=resolved_session,
            project_metadata=resolved_project,
        )

    async def _call_current_llm(
        self,
        llm_client: Any,
        prompt: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Optional[str]:
        if llm_client is None:
            logger.debug("[DreamingMemoryExtractor] no active LLM client")
            return None

        if hasattr(llm_client, "generate_memory_extraction_async"):
            try:
                # Native provider clients already gate this extraction method
                # themselves.  For lightweight/fake integrations that expose
                # the method without a gateway, apply the same transport
                # boundary here so a background retry cannot upload raw text.
                # Native clients with a concrete gateway (including the
                # AgentTurnRunner-owned gateway used by AgentLLMClient) own
                # the complete provider/privacy boundary.  A client that only
                # exposes a placeholder ``_privacy_gateway = None`` and no
                # concrete runner gateway does not; fall through to the
                # per-call gateway below instead of sending raw text directly
                # through an unguarded adapter.
                native_gateway = getattr(llm_client, "_privacy_gateway", None)
                if not isinstance(native_gateway, OutboundPrivacyGateway):
                    # AgentLLMClient stores the native gateway on its nested
                    # AgentTurnRunner rather than the public client object.
                    # The scoped-memory job detaches that runner before this
                    # call; recognize the same concrete gateway shape here so
                    # extraction is not wrapped in a second alias transaction.
                    native_gateway = getattr(
                        getattr(llm_client, "_turn_runner", None),
                        "privacy_gateway",
                        None,
                    )
                if isinstance(native_gateway, OutboundPrivacyGateway):
                    output = await llm_client.generate_memory_extraction_async(
                        prompt,
                        system_prompt=EXTRACTION_SYSTEM_PROMPT,
                    )
                    return str(output)
                gateway = self._privacy_gateway(
                    llm_client,
                    user_id=user_id,
                    session_id=session_id,
                    session_context=session_context,
                    project_metadata=project_metadata,
                )
                provider_name = str(
                    getattr(llm_client, "provider_label", None)
                    or getattr(llm_client, "provider", None)
                    or "openai"
                ).strip().lower().replace(" ", "-") or "openai"

                async def send_extraction(final_payload: Any) -> Any:
                    if not isinstance(final_payload, Mapping):
                        raise PrivacyError("dreaming extraction payload is malformed")
                    result = llm_client.generate_memory_extraction_async(
                        str(final_payload.get("prompt") or ""),
                        system_prompt=str(
                            final_payload.get("system_context")
                            or EXTRACTION_SYSTEM_PROMPT
                        ),
                    )
                    return await result if inspect.isawaitable(result) else result

                output = await gateway.execute(
                    {
                        "prompt": prompt,
                        "system_context": EXTRACTION_SYSTEM_PROMPT,
                    },
                    provider=provider_name,
                    descriptor=EgressDescriptor(
                        action="memory.extract",
                        transport="llm.generate_memory_extraction_async",
                        destination=provider_name,
                        provider=provider_name,
                        tool="memory.dreaming",
                    ),
                    sender=send_extraction,
                    source_kind="dreaming_memory_generation",
                )
                return gateway.restore_aliases(str(output))
            except Exception as exc:
                logger.warning("[DreamingMemoryExtractor] extraction client failed: %s", exc)
                return None

        if self._has_cli_backend(llm_client):
            return await asyncio.to_thread(
                self._call_cli_backend,
                llm_client,
                prompt,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )

        if self._has_safe_chat(llm_client):
            return await asyncio.to_thread(
                self._call_chat_client,
                llm_client,
                prompt,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )

        logger.debug(
            "[DreamingMemoryExtractor] active LLM client has no side-effect-free extraction path: %s",
            type(llm_client).__name__,
        )
        return None

    def _has_cli_backend(self, llm_client: Any) -> bool:
        backend = getattr(llm_client, "cli_backend", None)
        return callable(getattr(backend, "execute_prompt", None))

    def _has_safe_chat(self, llm_client: Any) -> bool:
        chat = getattr(llm_client, "chat", None)
        if not callable(chat):
            return False
        # AgentLLMClient.chat delegates back to normal generation and mutates history.
        if type(llm_client).__module__ == "src.llm.manager":
            return False
        return True

    def _call_cli_backend(
        self,
        llm_client: Any,
        prompt: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Optional[str]:
        try:
            backend = llm_client.cli_backend
            provider_name = str(
                getattr(backend, "get_provider_name", lambda: "codex-cli")()
                or "codex-cli"
            ).strip().lower().replace(" ", "-")
            gateway = self._privacy_gateway(
                llm_client,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )
            def send_cli_extraction(final_payload: Any) -> Any:
                if not isinstance(final_payload, Mapping):
                    raise PrivacyError("dreaming CLI extraction payload is malformed")
                return llm_client.cli_backend.execute_prompt(
                    prompt=str(final_payload.get("prompt") or ""),
                    cwd=Path.cwd(),
                    system_context=str(
                        final_payload.get("system_context") or EXTRACTION_SYSTEM_PROMPT
                    ),
                )

            success, output = gateway.execute_sync(
                {
                    "prompt": prompt,
                    "system_context": EXTRACTION_SYSTEM_PROMPT,
                },
                provider=provider_name,
                descriptor=EgressDescriptor(
                    action="memory.extract",
                    transport="cli.backend.execute_prompt",
                    destination=provider_name,
                    provider=provider_name,
                    tool="memory.dreaming",
                ),
                sender=send_cli_extraction,
                source_kind="dreaming_memory_cli",
            )
            if not success:
                logger.warning("[DreamingMemoryExtractor] CLI extraction failed: %s", output)
                return None
            return gateway.restore_aliases(str(output or ""))
        except Exception as exc:
            logger.warning("[DreamingMemoryExtractor] CLI extraction error: %s", exc)
            return None

    def _call_chat_client(
        self,
        llm_client: Any,
        prompt: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Optional[str]:
        try:
            provider_name = str(
                getattr(llm_client, "provider_label", None)
                or getattr(llm_client, "provider", None)
                or "openai"
            ).strip().lower().replace(" ", "-")
            gateway = self._privacy_gateway(
                llm_client,
                user_id=user_id,
                session_id=session_id,
                session_context=session_context,
                project_metadata=project_metadata,
            )
            chat = llm_client.chat
            kwargs: dict[str, Any] = {"temperature": 0.0, "max_tokens": 1200}
            try:
                params = inspect.signature(chat).parameters
            except (TypeError, ValueError):
                params = {}
            if "tools_enabled" in params:
                kwargs["tools_enabled"] = False

            def send_chat_extraction(final_payload: Any) -> Any:
                if not isinstance(final_payload, Mapping):
                    raise PrivacyError("dreaming chat extraction payload is malformed")
                messages = [
                    {
                        "role": "system",
                        "content": str(
                            final_payload.get("system_context")
                            or EXTRACTION_SYSTEM_PROMPT
                        ),
                    },
                    {"role": "user", "content": str(final_payload.get("prompt") or "")},
                ]
                return chat(messages, **kwargs)

            result = gateway.execute_sync(
                {
                    "prompt": prompt,
                    "system_context": EXTRACTION_SYSTEM_PROMPT,
                },
                provider=provider_name,
                descriptor=EgressDescriptor(
                    action="memory.extract",
                    transport="llm.chat",
                    destination=provider_name,
                    provider=provider_name,
                    tool="memory.dreaming",
                ),
                sender=send_chat_extraction,
                source_kind="dreaming_memory_chat",
            )
            if inspect.isgenerator(result):
                return gateway.restore_aliases("".join(str(part) for part in result))
            return gateway.restore_aliases(str(result or ""))
        except Exception as exc:
            logger.warning("[DreamingMemoryExtractor] chat extraction error: %s", exc)
            return None

    def _parse_response(self, output: str) -> Optional[List[Dict[str, Any]]]:
        if not output:
            return None

        json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", output, re.DOTALL)
        if json_match:
            json_text = json_match.group(1)
        else:
            json_match = re.search(r"\[.*\]", output, re.DOTALL)
            if not json_match:
                return None
            json_text = json_match.group(0)

        try:
            parsed = json.loads(json_text)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(parsed, list):
            return None

        memories: list[dict[str, Any]] = []
        project_qa_count = 0
        for item in parsed:
            if not isinstance(item, dict):
                continue
            artifact_markers = (
                item.get("artifact_type"),
                item.get("candidate_type"),
                item.get("kind"),
                item.get("type"),
                item.get("scope_intent"),
            )
            artifact_type = next(
                (
                    str(marker).strip().lower().replace("_", "-")
                    for marker in artifact_markers
                    if str(marker or "").strip().lower().replace("_", "-")
                    in {"project-qa", "project-q-a", "qa", "question-answer"}
                ),
                "",
            )
            if artifact_type in {"project-qa", "project-q-a", "qa", "question-answer"}:
                # Project Q&A is intentionally parsed as a distinct artifact;
                # the scoped-memory router must never mistake it for a user
                # memory or derive one from question-shaped raw text.
                if project_qa_count >= 3:
                    continue
                question = item.get("question") or item.get("title")
                evidence_span = item.get("evidence_span") or item.get(
                    "question_evidence_span"
                )
                if not isinstance(question, str) or not question.strip():
                    continue
                if not isinstance(evidence_span, str) or not evidence_span.strip():
                    continue
                normalized = {
                    "artifact_type": "project_qa",
                    "action": "upsert",
                    "question": question.strip(),
                    "answer": (
                        item.get("answer").strip()
                        if isinstance(item.get("answer"), str)
                        and item.get("answer").strip()
                        else None
                    ),
                    "answer_supported": item.get("answer_supported") is True,
                    "confidence": item.get("confidence", 0.0),
                    "importance": item.get("importance", 1),
                    "evidence_span": evidence_span.strip(),
                }
                answer_evidence_span = item.get("answer_evidence_span") or item.get(
                    "assistant_evidence_span"
                )
                if isinstance(answer_evidence_span, str) and answer_evidence_span.strip():
                    normalized["answer_evidence_span"] = answer_evidence_span.strip()
                memories.append(normalized)
                project_qa_count += 1
                continue
            action = str(item.get("action") or "upsert").strip().lower()
            evidence_span = str(item.get("evidence_span") or "").strip()
            if action not in {"upsert", "update", "delete", "delete_all"}:
                continue
            if not evidence_span:
                continue
            content = str(item.get("content") or "").strip()
            memory_type = str(item.get("memory_type") or "").strip()
            if action in {"upsert", "update"} and (
                not content or not memory_type
            ):
                continue
            if action == "delete" and not (
                str(item.get("memory_id") or "").strip() or content
            ):
                continue
            normalized = dict(item)
            normalized["action"] = action
            if content:
                normalized["content"] = content
            if memory_type:
                normalized["memory_type"] = memory_type
            normalized["evidence_span"] = evidence_span
            # Keep old provider payloads byte-compatible: omitted routing hints
            # are resolved by the router as user/false.  When a provider emits
            # either field, normalize it to the closed, safe vocabulary.
            if "scope_intent" in item:
                scope_intent = str(item.get("scope_intent") or "user").strip().lower()
                if scope_intent not in {"user", "project", "docs_candidate", "discard"}:
                    scope_intent = "user"
                normalized["scope_intent"] = scope_intent
            if "explicit_evidence" in item:
                # Only an actual boolean true is accepted as explicit.
                normalized["explicit_evidence"] = item.get("explicit_evidence") is True
            if "semantic_key" in item:
                semantic_key = item.get("semantic_key")
                if isinstance(semantic_key, str) and semantic_key.strip():
                    normalized["semantic_key"] = semantic_key.strip()[:200]
            memories.append(normalized)
        return memories
