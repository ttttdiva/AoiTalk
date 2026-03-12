"""
Terminal mode for AoiTalk Voice Assistant Framework
"""

import asyncio
from ..base import BaseAssistant
from ..response_handler import ResponseHandler


class TerminalMode(BaseAssistant):
    """Terminal mode assistant - text chat only"""
    
    def __init__(self, config):
        """Initialize terminal mode assistant
        
        Args:
            config: Configuration object
        """
        super().__init__(config, 'terminal')
        
        # Terminal mode doesn't use voice components
        self.response_handler = ResponseHandler(
            self.llm_client,
            character_name=self.character_name
        )
    
    def _setup_keyword_detection(self):
        """キーワード検出システムをセットアップ"""
        try:
            from ...tools.keyword.initializer import setup_keyword_detection
            setup_keyword_detection(self.config)
        except Exception as e:
            print(f"[TerminalMode] キーワード検出システムの初期化に失敗: {e}")
            # エラーが発生してもターミナルモードは動作を続行
        
    async def _initialize_mode_specific(self) -> bool:
        """Initialize terminal mode specific components"""
        print("[ターミナルモード] テキストチャットモードで開始")
        print("[ターミナルモード] TTS初期化をスキップ")
        
        # Initialize keyword detection system after LLM client is ready
        self._setup_keyword_detection()
        
        return True
        
    async def run(self):
        """Run terminal mode"""
        # Initialize
        if not await self.initialize():
            return
        
        # Get greeting
        personality = self.character_config.get('personality', {})
        greeting = personality.get('greeting', 'こんにちは！')
        
        print(f"\n💬 ターミナルモード開始")
        print(f"{self.character_name}: {greeting}")
        print("💡 テキストで対話してください")
        print("📝 'quit' または 'exit' で終了します\n")

        # Optionally start web UI for text chat convenience
        web_host, web_port, auto_open = self._get_web_interface_settings()
        server_url = self._start_web_interface(
            self._process_user_message_web,
            host=web_host,
            port=web_port,
            auto_open_browser=auto_open
        )
        if server_url:
            print("🌐 Webチャットインターフェースを開始しました (テキスト専用)")
            print(f"📍 ブラウザで以下のURLにアクセスしてください: {server_url}")
            if self.web_interface:
                self.web_interface.set_voice_recognition_ready(False)
                self.web_interface.set_recording_state(False)
                self.web_interface.update_rms(0.0)
                self.web_interface.add_system_message("🖥️ ターミナルモード: 音声なしでチャットできます")
                self.web_interface.add_assistant_message(greeting)
        else:
            print("⚠️ Webインターフェースは利用できません（ターミナルのみ）")

        await self._run_interactive_mode()

        # Cleanup
        await self.cleanup()
    
    
    async def _run_interactive_mode(self):
        """Run interactive mode with user input"""
        self.running = True
        
        try:
            while self.running:
                try:
                    raw = await asyncio.to_thread(input, "あなた: ")
                    message = raw.strip()
                    if message.lower() in ['quit', 'exit', '終了', 'やめる']:
                        break
                    if message:
                        await self._process_chat_message(message)
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print("\n\n終了します...")
                    break
        except Exception as e:
            print(f"ターミナルモードエラー: {e}")
    
    async def _process_chat_message(self, message: str, source: str = 'terminal', image_data: dict = None):
        """Process chat message

        Args:
            message: User message
            source: Message source ('terminal' or 'web')
            image_data: Optional image data for multimodal input {data: base64, mimeType: str, name: str}
        """
        try:
            if source != 'web' and self.web_interface:
                self.web_interface.add_user_message(message)
            # Check for keywords using universal keyword detection system
            try:
                from ...tools.keyword import process_keywords
                keyword_result = process_keywords(message)
                if keyword_result and keyword_result.detected:
                    # メッセージが辞書形式の場合（キャラクター切り替え）
                    if isinstance(keyword_result.message, dict):
                        msg_data = keyword_result.message
                        mode = msg_data.get('mode', '')

                        # 選択モードに入る時
                        if mode == 'selection_mode' and 'goodbye_reply' in msg_data:
                            # goodbyeReplyを表示
                            print(f"{self.character_name}: {msg_data['goodbye_reply']}")
                            print(f"\n{msg_data['message']}")
                            if self.web_interface:
                                self.web_interface.add_assistant_message(msg_data['goodbye_reply'])
                                self.web_interface.add_system_message(msg_data['message'])

                        # キャラクター切り替え完了時
                        elif mode == 'character_switched' and 'greeting' in msg_data:
                            print(f"\n{msg_data['message']}")
                            # キャラクター名を更新（コールバックが呼ばれるまでの一時的な対応）
                            from ...tools.keyword.character_manager import get_character_manager
                            manager = get_character_manager()
                            self.character_name = manager.get_current_character()
                            # greetingを表示
                            print(f"{self.character_name}: {msg_data['greeting']}")
                            if self.web_interface:
                                self.web_interface.add_system_message(msg_data['message'])
                                self.web_interface.add_assistant_message(msg_data['greeting'])

                        else:
                            print(f"{msg_data.get('message', '')}")
                            if self.web_interface and msg_data.get('message'):
                                self.web_interface.add_assistant_message(msg_data['message'])

                    # 通常のメッセージの場合
                    elif keyword_result.message:
                        print(f"{keyword_result.message}")
                        if self.web_interface:
                            self.web_interface.add_assistant_message(keyword_result.message)

                    # Skip normal processing if keyword was handled and LLM bypass is requested
                    if keyword_result.bypass_llm:
                        return
            except Exception as e:
                print(f"[キーワード検出] エラー: {e}")

            # Generate response
            response = await self.response_handler.handle_new_input(message, "chat", image_data=image_data)

            if response:
                print(f"{self.character_name}: {response}")
                if self.web_interface:
                    self.web_interface.add_assistant_message(response)
            else:
                print("応答の生成に失敗しました")
                    
        except Exception as e:
            print(f"チャットメッセージ処理エラー: {e}")

    async def _process_user_message_web(self, message: str, image_data=None, session_id=None, project_id=None):
        """Process user message sent from the WebUI
        
        Args:
            message: User message text
            image_data: Optional image data {data: base64, mimeType: str, name: str}
            session_id: Optional conversation session ID from frontend
            project_id: Optional project ID from frontend
        """
        # Set session ID in LLM client for session-specific message storage and history loading
        if session_id and hasattr(self, 'llm_client') and self.llm_client:
            self.llm_client.current_session_id = session_id
            print(f"[TerminalMode] Set session_id for message storage: {session_id}")
        
        # Set project ID in LLM client for project-specific session creation
        if project_id and hasattr(self, 'llm_client') and self.llm_client:
            self.llm_client.current_project_id = project_id
            print(f"[TerminalMode] Set project_id for session creation: {project_id}")
        
        # Pass image_data through to chat message processing
        await self._process_chat_message(message, source='web', image_data=image_data)
        
        # Clear session ID and project ID after processing
        if hasattr(self, 'llm_client') and self.llm_client:
            self.llm_client.current_session_id = None
            self.llm_client.current_project_id = None
    
    async def _cleanup_mode_specific(self):
        """Cleanup terminal mode specific resources"""
        # No specific cleanup needed for terminal mode
        pass
