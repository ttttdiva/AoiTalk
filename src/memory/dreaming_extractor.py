"""Extract Dreaming memory candidates from conversations."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You maintain AoiTalk Dreaming Memory.
Return only a JSON array. Do not include Markdown or commentary.
Use only the current user message as evidence. The assistant response is context only."""

EXTRACTION_PROMPT = """Extract durable user-state memory operations from the user message below.
Dreaming memories are canonical long-term notes for understanding the current user.

Save only:
- Stable user preferences, constraints, and response-style expectations
- Long-running projects, workflows, tools, or environment facts
- Explicit durable facts the user intentionally shared in the user message

Update/delete when:
- The user explicitly corrects a saved memory
- The user explicitly asks to forget a saved memory
- The user explicitly asks to clear all memories

Do not save:
- Temporary plans, appointments, moods, or one-off tasks
- Guesses or uncertain inferences
- Passwords, API keys, secrets, or highly sensitive personal data
- Content already covered by the existing memories
- Facts that only appear in the assistant response
- General knowledge, search results, or assistant suggestions

Existing Dreaming memories:
{existing}

Current turn:
User: {user_input}
Assistant response for context only, never as evidence:
{assistant_response}

Return only a JSON array. Do not wrap it in Markdown.
Each item must have:
- action: upsert / update / delete / delete_all
- memory_id: existing memory id for update/delete, or null
- content: concise single-sentence memory beginning with "The user ..."
  (null for delete_all; for delete, include the memory being deleted when useful)
- memory_type: fact / preference / constraint / project / workflow / relationship / instruction
- title: short optional title, or null
- confidence: number from 0.0 to 1.0
- importance: integer from 1 to 10
- expires_at: ISO datetime or null
- reason: short extraction reason
- sensitivity: normal / private / secret
- evidence_span: exact substring from the User message proving the memory

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
    ) -> List[Dict[str, Any]]:
        if not user_input.strip():
            return []

        existing_text = self._format_existing_memories(existing_memories)
        prompt = EXTRACTION_PROMPT.format(
            existing=existing_text,
            user_input=user_input,
            assistant_response=assistant_response[:800],
        )

        output = await self._call_current_llm(llm_client, prompt)
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

    async def _call_current_llm(self, llm_client: Any, prompt: str) -> Optional[str]:
        if llm_client is None:
            logger.debug("[DreamingMemoryExtractor] no active LLM client")
            return None

        if hasattr(llm_client, "generate_memory_extraction_async"):
            try:
                return str(
                    await llm_client.generate_memory_extraction_async(
                        prompt,
                        system_prompt=EXTRACTION_SYSTEM_PROMPT,
                    )
                )
            except Exception as exc:
                logger.warning("[DreamingMemoryExtractor] extraction client failed: %s", exc)
                return None

        if self._has_cli_backend(llm_client):
            return await asyncio.to_thread(self._call_cli_backend, llm_client, prompt)

        if self._has_safe_chat(llm_client):
            return await asyncio.to_thread(self._call_chat_client, llm_client, prompt)

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

    def _call_cli_backend(self, llm_client: Any, prompt: str) -> Optional[str]:
        try:
            success, output = llm_client.cli_backend.execute_prompt(
                prompt=prompt,
                cwd=Path.cwd(),
                system_context=EXTRACTION_SYSTEM_PROMPT,
            )
            if not success:
                logger.warning("[DreamingMemoryExtractor] CLI extraction failed: %s", output)
                return None
            return str(output or "")
        except Exception as exc:
            logger.warning("[DreamingMemoryExtractor] CLI extraction error: %s", exc)
            return None

    def _call_chat_client(self, llm_client: Any, prompt: str) -> Optional[str]:
        try:
            messages = [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            chat = llm_client.chat
            kwargs: dict[str, Any] = {"temperature": 0.0, "max_tokens": 1200}
            try:
                params = inspect.signature(chat).parameters
            except (TypeError, ValueError):
                params = {}
            if "tools_enabled" in params:
                kwargs["tools_enabled"] = False
            result = chat(messages, **kwargs)
            if inspect.isgenerator(result):
                return "".join(str(part) for part in result)
            return str(result or "")
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
        for item in parsed:
            if not isinstance(item, dict):
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
            memories.append(normalized)
        return memories
