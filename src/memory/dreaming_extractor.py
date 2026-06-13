"""Extract Dreaming memory candidates from conversations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_MESSAGE_LENGTH = 20

EXTRACTION_PROMPT = """Extract durable user-state facts from the conversation below.
Dreaming memories are canonical long-term notes for understanding the current user.

Save only:
- Stable user preferences, constraints, and response-style expectations
- Long-running projects, workflows, tools, or environment facts
- Explicit durable facts the user intentionally shared

Do not save:
- Temporary plans, appointments, moods, or one-off tasks
- Guesses or uncertain inferences
- Passwords, API keys, secrets, or highly sensitive personal data
- Content already covered by the existing memories

Existing Dreaming memories:
{existing}

Current turn:
User: {user_input}
Assistant: {assistant_response}

Return only a JSON array. Do not wrap it in Markdown.
Each item must have:
- content: concise single-sentence memory
- memory_type: fact / preference / constraint / project / workflow / relationship / instruction
- title: short optional title, or null
- confidence: number from 0.0 to 1.0
- importance: integer from 1 to 10
- expires_at: ISO datetime or null
- reason: short extraction reason
- sensitivity: normal / private / secret

Return [] when there is nothing worth storing.
"""


class DreamingMemoryExtractor:
    """Extract long-term memory candidates from one completed turn."""

    async def extract(
        self,
        user_input: str,
        assistant_response: str,
        existing_memories: List[str],
    ) -> List[Dict[str, Any]]:
        if len(user_input.strip()) < MIN_MESSAGE_LENGTH:
            return []

        existing_text = (
            "\n".join(f"- {memory}" for memory in existing_memories)
            if existing_memories
            else "(none)"
        )
        prompt = EXTRACTION_PROMPT.format(
            existing=existing_text,
            user_input=user_input,
            assistant_response=assistant_response[:800],
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._call_gemini, prompt)
        if result is not None:
            return result

        logger.debug("[DreamingMemoryExtractor] extraction failed")
        return []

    def _call_gemini(self, prompt: str) -> Optional[List[Dict[str, Any]]]:
        try:
            import google.generativeai as genai

            api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            if not api_key:
                logger.debug("[DreamingMemoryExtractor] Gemini API key is not configured")
                return None

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(prompt)
            return self._parse_response(response.text)
        except Exception as exc:
            logger.warning("[DreamingMemoryExtractor] Gemini API error: %s", exc)
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
            if isinstance(item, str) and item.strip():
                memories.append({"content": item.strip(), "memory_type": "fact"})
            elif isinstance(item, dict):
                content = str(item.get("content") or item.get("value") or "").strip()
                if content:
                    normalized = dict(item)
                    normalized["content"] = content
                    memories.append(normalized)
        return memories
