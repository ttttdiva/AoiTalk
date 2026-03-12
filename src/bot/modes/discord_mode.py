"""
Discord mode for AoiTalk bot
"""

import asyncio
import logging
import os
from typing import Optional, Dict, Any, List
import aiohttp
import base64
import io
from PIL import Image
import google.generativeai as genai

from ...assistant.base import BaseAssistant
from ...config import Config
from ...memory.history import HistoryManager

logger = logging.getLogger(__name__)


class DiscordMode(BaseAssistant):
    """Discord-specific assistant mode"""
    
    def __init__(self, config: Config, character: str = None):
        """Initialize Discord mode
        
        Args:
            config: Configuration manager
            character: Character name to use
        """
        super().__init__(config, mode='discord')
        
        # Override character if specified
        if character:
            self.character_name = character
            self.character_config = config.get_character_config(character)
            
        # Discord-specific state
        self.guild_contexts: Dict[int, Dict[str, Any]] = {}  # Guild ID -> context
        self.user_contexts: Dict[int, Dict[str, Any]] = {}   # User ID -> context
        self._memory_prefill_attempts: Dict[str, bool] = {}

    async def _initialize_mode_specific(self) -> bool:
        """Initialize Discord-specific components"""
        try:
            # Discord modeはTTSやSTTを直接使用しない（必要に応じて初期化）
            logger.info("Discord mode initialized")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Discord mode: {e}")
            return False
    
    async def run(self):
        """Run Discord mode (called when needed)"""
        # Discord modeは event-driven なので特別な実行ループは不要
        self.running = True
        logger.info("Discord mode is running")
    
    async def _cleanup_mode_specific(self):
        """Cleanup Discord-specific resources"""
        self.running = False
        self.guild_contexts.clear()
        self.user_contexts.clear()
        logger.info("Discord mode cleaned up")
    
    async def process_text(self, text: str, user_id: int = None, guild_id: int = None) -> str:
        """Process text message and generate response
        
        Args:
            text: Input text from user
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
            
        Returns:
            Generated response text
        """
        try:
            # セッションコンテキストとメモリ関連情報を設定
            self._set_llm_session_context(user_id, guild_id)

            # コンテキストの取得または作成
            context = self._get_or_create_context(user_id, guild_id)
            
            # メッセージ履歴に追加
            if 'history_manager' not in context:
                 context['history_manager'] = HistoryManager(
                     max_history_length=self.config.get('discord.max_history_length', 20)
                 )
            
            context['history_manager'].add_message('user', text)
            
            # LLMで応答生成
            response = await self._generate_response_with_context(text, context)
            
            # 応答を履歴に追加
            if response:
                context['history_manager'].add_message('assistant', response)
                
                # Check for background summarization
                if hasattr(self.llm_client, 'check_and_summarize_history'):
                     self.llm_client.check_and_summarize_history(context['history_manager'])
            
            return response or "申し訳ありません。応答の生成に失敗しました。"
            
        except Exception as e:
            logger.error(f"Error processing text: {e}", exc_info=True)
            return "エラーが発生しました。もう一度お試しください。"
    
    async def process_text_with_images(self, text: str, image_urls: List[str], user_id: int = None, guild_id: int = None) -> str:
        """Process text message with images and generate response
        
        Args:
            text: Input text from user
            image_urls: List of image URLs
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
            
        Returns:
            Generated response text
        """
        try:
            # セッションコンテキストとメモリ関連情報を設定
            self._set_llm_session_context(user_id, guild_id)

            # コンテキストの取得または作成
            context = self._get_or_create_context(user_id, guild_id)
            
            # 画像をダウンロード
            images_data = []
            async with aiohttp.ClientSession() as session:
                for url in image_urls[:4]:  # 最大4枚まで
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                image_bytes = await resp.read()
                                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                                images_data.append({
                                    'data': image_bytes,
                                    'mime_type': content_type,
                                    'url': url
                                })
                    except Exception as e:
                        logger.error(f"Error downloading image {url}: {e}")
            
            # メッセージを構築
            if not text:
                text = "この画像について説明してください。"
            
            # LLMで応答生成（画像対応）
            response = await self._generate_response_with_images(text, images_data, context)
            
            # 応答を履歴に追加（簡略化）
            if response:
                if 'history_manager' not in context:
                     context['history_manager'] = HistoryManager(
                         max_history_length=self.config.get('discord.max_history_length', 20)
                     )
                
                context['history_manager'].add_message('user', f"{text} [画像{len(images_data)}枚]")
                context['history_manager'].add_message('assistant', response)
                
                # Check for background summarization
                if hasattr(self.llm_client, 'check_and_summarize_history'):
                     self.llm_client.check_and_summarize_history(context['history_manager'])
            
            return response or "申し訳ありません。画像の処理に失敗しました。"
            
        except Exception as e:
            logger.error(f"Error processing text with images: {e}", exc_info=True)
            return "エラーが発生しました。画像の処理中に問題が発生しました。"
    
    async def process_voice(self, text: str, user_id: int = None, guild_id: int = None) -> Optional[str]:
        """Process voice input text and generate response"""
        return await self.process_text(text, user_id, guild_id)

    async def prefill_context_from_memory(
        self,
        user_id: Optional[int],
        guild_id: Optional[int],
        max_messages: Optional[int] = None
    ) -> bool:
        """Load recent conversation history from persistent memory for context."""
        memory_manager = getattr(self.llm_client, 'memory_manager', None)
        if not memory_manager or user_id is None:
            return False

        memory_user_id = self._build_memory_user_id(user_id, guild_id)
        if self._memory_prefill_attempts.get(memory_user_id):
            return False

        message_limit = max_messages or self.config.get('discord.memory_prefill_message_count', 10)
        message_limit = max(2, min(message_limit, self.config.get('discord.max_history_length', 20)))

        try:
            messages = await memory_manager.get_recent_messages(
                memory_user_id,
                self.character_name,
                count=message_limit
            )
        except Exception as exc:
            logger.error(f"Failed to prefill memory context: {exc}")
            self._memory_prefill_attempts[memory_user_id] = True
            return False

        if not messages:
            self._memory_prefill_attempts[memory_user_id] = True
            return False

        context = self._get_or_create_context(user_id, guild_id)
        if 'history_manager' not in context:
             context['history_manager'] = HistoryManager(
                 max_history_length=self.config.get('discord.max_history_length', 20)
             )
        
        context['history_manager'].clear()
        
        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content')
            if content:
                context['history_manager'].add_message(role, content)

        self._memory_prefill_attempts[memory_user_id] = True
        return True

    def _build_memory_user_id(self, user_id: Optional[int], guild_id: Optional[int]) -> str:
        parts = ['discord']
        if guild_id is not None:
            parts.append(str(guild_id))
        if user_id is not None:
            parts.append(str(user_id))
        return ':'.join(parts)

    def _set_llm_session_context(self, user_id: Optional[int], guild_id: Optional[int]) -> None:
        if not hasattr(self.llm_client, 'set_session_context'):
            return

        memory_user_id = self._build_memory_user_id(user_id, guild_id)
        metadata = {
            'platform': 'discord',
            'guild_id': str(guild_id) if guild_id is not None else None,
            'mode': self.mode
        }

        try:
            self.llm_client.set_session_context(
                user_id=memory_user_id,
                metadata=metadata
            )
        except Exception as exc:
            logger.debug(f"Failed to set session context: {exc}")

    async def _call_llm_generate_response(self, text: str) -> Optional[str]:
        """Call LLM generate_response without blocking the event loop"""
        try:
            if hasattr(self.llm_client, 'generate_response_async'):
                return await self.llm_client.generate_response_async(text)

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.llm_client.generate_response(text, stream=False)
            )
        except Exception as exc:
            logger.error(f"Failed to invoke LLM response: {exc}")
            return None
    
    def _get_or_create_context(self, user_id: Optional[int] = None, guild_id: Optional[int] = None) -> Dict[str, Any]:
        """Get or create context for user/guild
        
        Args:
            user_id: Discord user ID
            guild_id: Discord guild ID
            
        Returns:
            Context dictionary
        """
        # ユーザーコンテキストを優先
        if user_id:
            if user_id not in self.user_contexts:
                self.user_contexts[user_id] = {
                    'history_manager': HistoryManager(
                        max_history_length=self.config.get('discord.max_history_length', 20)
                    ),
                    'guild_id': guild_id,
                    'character': self.character_name
                }
            return self.user_contexts[user_id]
        
        # ギルドコンテキスト
        if guild_id:
            if guild_id not in self.guild_contexts:
                self.guild_contexts[guild_id] = {
                    'history_manager': HistoryManager(
                         max_history_length=self.config.get('discord.max_history_length', 20)
                    ),
                    'character': self.character_name
                }
            return self.guild_contexts[guild_id]
        
        # デフォルトコンテキスト
        return {
            'history_manager': HistoryManager(
                max_history_length=self.config.get('discord.max_history_length', 20)
            ), 
            'character': self.character_name
        }
    
    async def _generate_response_with_context(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        """Generate response with context
        
        Args:
            text: Input text
            context: Context dictionary
            
        Returns:
            Generated response or None
        """
        try:
            # キャラクター設定の確認
            character = context.get('character', self.character_name)
            if character != self.character_name:
                # キャラクターが変更された場合は再初期化
                self.character_name = character
                self.character_config = self.config.get_character_config(character)
                self._init_common_components()
            
            # Function callingを使用するかチェック
            use_tools = self.config.get('use_tools', True)
            
            if use_tools and hasattr(self.llm_client, 'generate_response'):
                # 既存のLLMマネージャーを使用（ツール対応）
                # 会話履歴を設定
                if hasattr(self.llm_client, 'conversation_history'):
                    # 最近の会話履歴を設定
                    self.llm_client.conversation_history = []
                    
                    history_manager = context.get('history_manager')
                    messages = history_manager.get_context(10) if history_manager else []
                    
                    for msg in messages:
                        if msg['role'] == 'user':
                            self.llm_client.conversation_history.append({
                                'role': 'user',
                                'content': msg['content']
                            })
                        elif msg['role'] == 'assistant':
                            self.llm_client.conversation_history.append({
                                'role': 'assistant', 
                                'content': msg['content']
                            })
                
                # ツール付きで応答生成（非同期呼び出しでイベントループをブロックしない）
                response = await self._call_llm_generate_response(text)
            else:
                # 通常の応答生成
                response = await self._generate_with_interrupt_check(
                    text=text,
                    task_id=f"discord-{context.get('guild_id', 'dm')}"
                )
            
            return response
            
        except Exception as e:
            logger.error(f"Error generating response: {e}", exc_info=True)
            return None
    
    async def _generate_response_with_images(self, text: str, images_data: List[Dict], context: Dict[str, Any]) -> Optional[str]:
        """Generate response with images
        
        Args:
            text: Input text
            images_data: List of image data dictionaries ({'data': bytes, 'mime_type': str})
            context: Context dictionary
            
        Returns:
            Generated response or None
        """
        try:
            # キャラクター設定の確認
            character = context.get('character', self.character_name)
            if character != self.character_name:
                # キャラクターが変更された場合は再初期化
                self.character_name = character
                self.character_config = self.config.get_character_config(character)
                self._init_common_components()
            
            vision_model = self.config.get('discord.vision_model', 'gemini-3-flash-preview')
            logger.info(f"Using vision model: {vision_model}")
            
            if 'gemini' in vision_model.lower():
                # Gemini APIを使用
                api_key = self.config.get('gemini_api_key') or os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
                if not api_key:
                    logger.error("API Key for Gemini is not set (checked gemini_api_key, GOOGLE_API_KEY, GEMINI_API_KEY)")
                    return "申し訳ありません。APIキー設定のエラーです。"
                
                genai.configure(api_key=api_key)
                
                # モデル設定
                model = genai.GenerativeModel(
                    model_name=vision_model,
                    system_instruction=self.character_config.get('personality', {}).get('details', 'あなたは親切なAIアシスタントです。')
                )
                
                # コンテンツ構築
                content_parts = []
                
                # テキスト追加
                content_parts.append(text)
                
                # 画像追加
                for img_data in images_data:
                    try:
                        # バイト列からPIL Imageを作成
                        image = Image.open(io.BytesIO(img_data['data']))
                        content_parts.append(image)
                    except Exception as e:
                        logger.error(f"Error processing image for Gemini: {e}")
                
                # 会話履歴を考慮（簡易的）
                history_text = ""
                history_manager = context.get('history_manager')
                if history_manager:
                    # 最新5件を取得
                    for msg in history_manager.get_context(5):
                        role = "ユーザー" if msg['role'] == 'user' else "あなた"
                        content = msg['content']
                        # 画像プレースホルダーを除去
                        content = content.split('[画像')[0].strip()
                        history_text += f"{role}: {content}\n"
                
                if history_text:
                    prompt = f"これまでの会話:\n{history_text}\n\nユーザーの入力: {text}"
                    content_parts[0] = prompt
                
                # 生成実行
                response = await asyncio.to_thread(
                    model.generate_content,
                    content_parts
                )
                
                return response.text
                
            else:
                # OpenAI APIを使用 (GPT-4oなど)
                import openai
                client = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
                
                # メッセージを構築
                messages = [
                    {
                        "role": "system",
                        "content": self.character_config.get('personality', {}).get('details', 'あなたは親切なAIアシスタントです。')
                    }
                ]
                
                # 会話履歴を追加（テキストのみ）
                history_manager = context.get('history_manager')
                if history_manager:
                    for msg in history_manager.get_context(10):  # 最近の10件
                        if msg['role'] in ['user', 'assistant']:
                            messages.append({
                                "role": msg['role'],
                                "content": msg['content']
                            })
                
                # 画像データをOpenAI形式に変換
                openai_images = []
                for img_data in images_data:
                    base64_image = base64.b64encode(img_data['data']).decode('utf-8')
                    openai_images.append({
                        'type': 'image_url',
                        'image_url': {
                            'url': f"data:{img_data['mime_type']};base64,{base64_image}"
                        }
                    })
                
                # 現在のメッセージを追加（画像付き）
                user_message = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text}
                    ]
                }
                user_message["content"].extend(openai_images)
                messages.append(user_message)
                
                # GPT-4oで応答生成
                response = client.chat.completions.create(
                    model=vision_model, # configから取得したモデル名を使用
                    messages=messages,
                    max_tokens=1000
                )
                
                return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"Error generating response with images: {e}", exc_info=True)
            return None
    
    def set_character(self, character: str, user_id: Optional[int] = None, guild_id: Optional[int] = None):
        """Set character for context
        
        Args:
            character: Character name
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
        """
        context = self._get_or_create_context(user_id, guild_id)
        context['character'] = character
        
        # 現在のコンテキストのキャラクターを変更
        if (user_id and self.user_contexts.get(user_id) == context) or \
           (guild_id and self.guild_contexts.get(guild_id) == context):
            self.character_name = character
            self.character_config = self.config.get_character_config(character)
            self._init_common_components()
    
    def clear_context(self, user_id: Optional[int] = None, guild_id: Optional[int] = None):
        """Clear conversation context
        
        Args:
            user_id: Discord user ID (optional)
            guild_id: Discord guild ID (optional)
        """
        if user_id and user_id in self.user_contexts:
            if 'history_manager' in self.user_contexts[user_id]:
                self.user_contexts[user_id]['history_manager'].clear()
        elif guild_id and guild_id in self.guild_contexts:
            if 'history_manager' in self.guild_contexts[guild_id]:
                self.guild_contexts[guild_id]['history_manager'].clear()
