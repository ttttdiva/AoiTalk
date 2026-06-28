"""Media specialist agent."""

from __future__ import annotations

from ..llm.native_runtime import AgentDefinition as Agent, NativeModelSettings as ModelSettings

from ..tools.core import ensure_tool_definitions
from ..tools.entertainment.video_streaming.video_streaming_tools import (
    get_video_playback_status,
    play_niconico_audio,
    play_youtube_audio,
    search_and_play_niconico,
    search_and_play_youtube,
    stop_video_audio,
)
from ..tools.image_generation import generate_image
from ..tools.comfyui_image_generation import generate_comfyui_image, list_comfyui_workflows
from ..tools.entertainment.music_tools import play_bgm, stop_bgm
from .base import BaseAgent


class MediaAgent(BaseAgent):
    """Specialized agent for image and streaming media tasks."""

    def _create_agent(self) -> Agent:
        tools = ensure_tool_definitions(
            [
                generate_image,
                generate_comfyui_image,
                list_comfyui_workflows,
                search_and_play_youtube,
                play_youtube_audio,
                search_and_play_niconico,
                play_niconico_audio,
                stop_video_audio,
                get_video_playback_status,
                play_bgm,
                stop_bgm,
            ]
        )

        instructions = """
You are a media specialist.

Handle image generation, streaming audio playback for YouTube and NicoNico, and BGM control.
Use the available media tools directly to carry out the user's request.

1. **BGM Control**: Use `play_bgm` to set the background music or atmosphere. This is useful for setting a specific mood without playing a full YouTube video.
   - When playing BGM, you can use descriptive IDs like "peaceful", "tense", "mysterious", or provide a YouTube URL.
   - Use `stop_bgm` to silence the background music.

2. **Image Generation**: You have two tools:
   - generate_image: Uses Gemini's built-in image generation. Good for general requests.
   - generate_comfyui_image: Uses a local ComfyUI server. Better for high-quality, character-specific, or stylized images.
     - Use `list_comfyui_workflows` to see available workflows.
     - You can specify `workflow_name` and override parameters like `width`, `height`, `steps`, `cfg`, `sampler`, `scheduler`, `lora_strength`, and `seed`.
     - If the user mentions a specific character, try to use their slug in `character_slug` to apply their appearance tags.
     - Use Danbooru-style tags (comma separated) for the prompt if possible.

3. **Streaming**: Use YouTube or NicoNico tools to find and play specific music or videos requested by the user.
""".strip()

        return Agent(
            name="MediaAssistant",
            model=self.model,
            instructions=instructions,
            model_settings=ModelSettings(tool_choice="required"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "media_assistant"

    def get_tool_description(self) -> str:
        return (
            "Media assistant - generate images and control YouTube or NicoNico "
            "audio playback"
        )
