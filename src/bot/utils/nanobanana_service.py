"""Utility helpers for Nanobanana Pro slash command"""
from __future__ import annotations

import base64
import logging
import os
import re
import textwrap
from typing import List, Optional, Tuple

from openai import OpenAI

from ...tools.basic.web_search import web_search_impl

logger = logging.getLogger(__name__)


class NanobananaProService:
    """Search and image generation helper"""

    SEARCH_QUERY = "Nanobanana Pro 最新情報 仕様 機能 2025"

    def __init__(self) -> None:
        self._client: Optional[OpenAI] = None

    def fetch_summary(self) -> str:
        """Fetch summary text via web_search tool"""
        logger.info("Fetching Nanobanana Pro summary via web_search tool")
        result = web_search_impl(self.SEARCH_QUERY)
        if not isinstance(result, str) or not result.strip():
            return "Nanobanana Proの最新情報を取得できませんでした。"
        return result.strip()

    def build_embed_description(self, summary: str, max_items: int = 3, max_len: int = 900) -> str:
        """Build a compact bullet list for Discord embeds"""
        highlights = self._extract_highlights(summary, limit=max_items)
        if not highlights:
            return "Nanobanana Proに関する追加情報を取得できませんでした。"
        joined = "\n".join(f"・{item}" for item in highlights)
        return textwrap.shorten(joined, width=max_len, placeholder="...")

    def generate_image(self, summary: str) -> Tuple[Optional[bytes], str]:
        """Generate promotional image bytes and prompt"""
        prompt = self._build_image_prompt(summary)
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            logger.warning("OPENAI_API_KEY is missing. Skipping image generation.")
            return None, prompt

        if self._client is None:
            self._client = OpenAI(api_key=api_key)

        try:
            logger.info("Generating Nanobanana Pro hero image via OpenAI Images API")
            response = self._client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                quality="high",
                response_format="b64_json"
            )
            image_data = response.data[0].b64_json
            return base64.b64decode(image_data), prompt
        except Exception as exc:  # pragma: no cover - network failure path
            logger.error("Image generation failed: %s", exc)
            return None, prompt

    def _extract_highlights(self, text: str, limit: int = 3) -> List[str]:
        sanitized = text.replace('\r', '\n')
        parts = re.split(r"[\n\r]+|(?<=[。．.!?])\s+", sanitized)
        cleaned: List[str] = []
        for part in parts:
            item = part.strip(" -*•\u3000")
            if len(item) < 20:
                continue
            cleaned.append(item)
            if len(cleaned) >= limit:
                break
        if not cleaned and text:
            cleaned.append(textwrap.shorten(text, width=150, placeholder="..."))
        return cleaned

    def _build_image_prompt(self, summary: str) -> str:
        keywords = self._extract_keywords(summary)
        keyword_text = ", ".join(keywords) if keywords else "creative AI studio"
        prompt = (
            "Create a cinematic 4K marketing render for Nanobanana Pro, a reasoning-first AI "
            "image and video suite. Highlight {features} with glossy banana-yellow accents, "
            "floating multitouch canvases, holographic UI, and pro-grade lighting. Include subtle "
            "SynthID watermark indicators and futuristic studio vibes.".format(features=keyword_text)
        )
        return prompt

    def _extract_keywords(self, text: str) -> List[str]:
        candidates = {
            "4K": "4K fidelity dashboards",
            "8K": "8K-ready canvas",
            "lossless": "lossless diffusion",
            "reasoning": "reasoning copilots",
            "video": "video + image hybrid workflows",
            "texture": "photoreal textures",
            "SynthID": "SynthID-visible security",
            "AR": "mixed-reality overlays"
        }
        found: List[str] = []
        lowered = text.lower()
        for token, phrase in candidates.items():
            if token.lower() in lowered:
                found.append(phrase)
        return found[:4]
