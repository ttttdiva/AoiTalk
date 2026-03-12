"""
Base assistant class for AoiTalk Voice Assistant Framework
"""

import asyncio
import time
import platform
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from pathlib import Path
from src.tools.keyword.character_manager import get_character_manager

# WSL2環境の自動設定
if platform.system() == 'Linux':
    try:
        with open('/proc/version', 'r') as f:
            if 'microsoft' in f.read().lower():
                from dotenv import load_dotenv
                load_dotenv()
                
                pulse_runtime_path = os.getenv('PULSE_RUNTIME_PATH', '/mnt/wslg/runtime-dir/pulse')
                if os.path.exists(pulse_runtime_path):
                    os.environ['PULSE_RUNTIME_PATH'] = pulse_runtime_path
                
                os.environ['SDL_AUDIODRIVER'] = 'pulse'
    except:
        pass


class BaseAssistant(ABC):
    """Base class for all assistant modes"""
    
    def __init__(self, config, mode: str):
        """Initialize base assistant
        
        Args:
            config: Configuration object
            mode: Assistant mode ('terminal', 'voice_chat', etc.)
        """
        self.config = config
        self.mode = mode
        self.running = False
        self.web_interface = None
        
        # Load character configuration
        self.character_name = self.config.default_character
        self.character_config = self.config.get_character_config(self.character_name)
        
        # Register character switch callback
        self._register_character_switch_callback()
        
        # Common initialization
        self._init_common_components()
        
    def _init_common_components(self):
        """Initialize components common to all modes"""
        print("[BaseAssistant] _init_common_components開始")
        
        # LLM client initialization
        from src.llm.manager import create_llm_client
        
        use_tools = self.config.get('use_tools', True)
        if use_tools:
            print("[ツールモード] Function calling・MCP対応")
        else:
            print("[標準モード] 基本的なLLMクライアントを使用します")
        
        print("[BaseAssistant] LLMクライアント作成開始")
        self.llm_client = create_llm_client(self.config, use_agent=use_tools)
        print("[BaseAssistant] LLMクライアント作成完了")
        
        # Set LLM system prompt
        personality = self.character_config.get('personality', {})
        system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
        self.llm_client.set_system_prompt(system_prompt)
        print("[BaseAssistant] _init_common_components完了")
        
    async def initialize(self) -> bool:
        """Initialize assistant components
        
        Returns:
            bool: True if initialization succeeded
        """
        print(f"初期化中... (キャラクター: {self.character_name})")
        print("[BaseAssistant] initializeメソッド開始")
        
        # Initialize memory manager in background to avoid blocking startup
        memory_init_task = None
        if hasattr(self.llm_client, 'memory_manager') and self.llm_client.memory_manager:
            if hasattr(self.llm_client.memory_manager, 'initialize'):
                async def init_memory_background():
                    try:
                        print("[BaseAssistant] メモリシステムをバックグラウンドで初期化中...")
                        await self.llm_client.memory_manager.initialize()
                        print("[BaseAssistant] メモリシステムの初期化完了")
                    except Exception as e:
                        print(f"[BaseAssistant] メモリシステムの初期化エラー: {e}")
                
                # Start memory initialization in background
                memory_init_task = asyncio.create_task(init_memory_background())
        
        # Mode-specific initialization (e.g., VOICEVOX) runs in parallel
        mode_init_result = await self._initialize_mode_specific()
        
        # Optionally wait for memory init with a short timeout
        if memory_init_task:
            try:
                await asyncio.wait_for(memory_init_task, timeout=2.0)
            except asyncio.TimeoutError:
                # Memory init continues in background
                print("[BaseAssistant] メモリシステムの初期化は継続中（バックグラウンド）")
        
        return mode_init_result

    def _get_web_interface_settings(self):
        """Resolve WebUI host/port/auto-open settings from config/env"""
        web_config = self.config.get('web_interface', {}) or {}

        host = web_config.get('host', '127.0.0.1') or '127.0.0.1'
        port = web_config.get('port', 3000) or 3000
        auto_open = web_config.get('auto_open_browser', True)

        host = os.getenv('AOITALK_WEB_HOST', host)

        env_port = os.getenv('AOITALK_WEB_PORT')
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                print(f"[WebUI] Invalid AOITALK_WEB_PORT '{env_port}', falling back to {port}")

        env_auto = os.getenv('AOITALK_WEB_AUTO_OPEN')
        if env_auto is not None:
            auto_open = env_auto.strip().lower() in ('1', 'true', 'yes', 'on')

        try:
            port = int(port)
        except (TypeError, ValueError):
            print(f"[WebUI] Invalid port setting '{port}', falling back to 3000")
            port = 3000

        return host, port, bool(auto_open)

    def _get_local_browser_url(self, host: str, port: int, server_url: str) -> str:
        """Translate wildcard hosts to a usable loopback URL for auto-open"""
        wildcard_hosts = {'0.0.0.0', '::', '[::]'}
        host_value = str(host).strip()
        
        # Determine protocol from server_url
        protocol = "https" if server_url.startswith("https://") else "http"

        if host_value in wildcard_hosts:
            return f"{protocol}://127.0.0.1:{port}"

        # Additional safeguard: uvicorn may report the bound URL as 0.0.0.0 even if
        # host string had extra formatting. Rewrite those cases as well.
        if '://0.0.0.0' in server_url:
            return f"{protocol}://127.0.0.1:{port}"

        return server_url

    def _get_ssl_settings(self):
        """Resolve SSL settings from environment variables
        
        Returns:
            tuple: (ssl_enabled, ssl_keyfile, ssl_certfile)
        """
        ssl_enabled = os.getenv('AOITALK_SSL_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
        ssl_keyfile = os.getenv('AOITALK_SSL_KEYFILE', '')
        ssl_certfile = os.getenv('AOITALK_SSL_CERTFILE', '')
        
        if ssl_enabled:
            # Resolve relative paths from project root
            project_root = Path(__file__).parent.parent.parent
            
            if ssl_keyfile and not Path(ssl_keyfile).is_absolute():
                ssl_keyfile = str(project_root / ssl_keyfile)
            if ssl_certfile and not Path(ssl_certfile).is_absolute():
                ssl_certfile = str(project_root / ssl_certfile)
            
            # Verify files exist
            if not Path(ssl_keyfile).exists():
                print(f"⚠️  SSL key file not found: {ssl_keyfile}")
                print("   Run: python scripts/generate_ssl_cert.py")
                return False, None, None
            if not Path(ssl_certfile).exists():
                print(f"⚠️  SSL cert file not found: {ssl_certfile}")
                print("   Run: python scripts/generate_ssl_cert.py")
                return False, None, None
        
        return ssl_enabled, ssl_keyfile if ssl_enabled else None, ssl_certfile if ssl_enabled else None

    def _start_web_interface(self, input_callback, host: str = '127.0.0.1', port: int = 3000,
                              auto_open_browser: bool = True) -> Optional[str]:
        """Start FastAPI-based web interface shared across modes"""
        try:
            from src.api.web_interface import create_web_interface
        except ImportError:
            print("❌ Webインターフェースの依存関係が不足しています")
            print("   pip install fastapi uvicorn[standard] websockets を実行してください")
            return None

        try:
            # Get SSL settings
            ssl_enabled, ssl_keyfile, ssl_certfile = self._get_ssl_settings()
            
            self.web_interface = create_web_interface(self.config, self.character_name)
            current_loop = asyncio.get_running_loop()
            self.web_interface.set_user_input_callback(input_callback, current_loop)
            
            # Set clear chat callback to start new session when user clicks "New Conversation"
            if hasattr(self.llm_client, 'clear_history'):
                self.web_interface.set_clear_chat_callback(self.llm_client.clear_history)
            
            server_url = self.web_interface.start_server(
                host=host, port=port,
                ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile
            )

            browser_url = self._get_local_browser_url(host, port, server_url)

            if auto_open_browser:
                self._open_browser_async(browser_url)

            return server_url
        except Exception as e:
            print(f"❌ Webインターフェースの開始に失敗しました: {e}")
            return None

    def _open_browser_async(self, server_url: str):
        """Open browser asynchronously to avoid blocking event loop"""
        def open_browser():
            import time as _time
            _time.sleep(1.5)
            try:
                # Special handling for WSL2
                if platform.system() == 'Linux' and 'microsoft' in platform.uname().release.lower():
                    import subprocess
                    subprocess.run(['cmd.exe', '/c', 'start', server_url], check=True)
                    print(f"🌐 ブラウザを自動で開きました: {server_url}")
                else:
                    import webbrowser
                    webbrowser.open(server_url)
                    print(f"🌐 ブラウザを自動で開きました: {server_url}")
            except Exception as e:
                print(f"⚠️  ブラウザの自動起動に失敗しました: {e}")
                print(f"📍 手動で以下のURLにアクセスしてください: {server_url}")

        browser_thread = threading.Thread(target=open_browser, daemon=True)
        browser_thread.start()

    @abstractmethod
    async def _initialize_mode_specific(self) -> bool:
        """Initialize mode-specific components
        
        Returns:
            bool: True if initialization succeeded
        """
        pass
    
    @abstractmethod
    async def run(self):
        """Run the assistant"""
        pass
        
    def _register_character_switch_callback(self):
        """Register callback for character switching"""
        manager = get_character_manager()
        manager.register_callback(self._on_character_switch)
        
    def _on_character_switch(self, character_name: str, yaml_filename: str):
        """Handle character switch event
        
        Args:
            character_name: New character name
            yaml_filename: YAML filename (without extension)
        """
        print(f"[BaseAssistant] キャラクター切り替え: {self.character_name} -> {character_name}")
        
        # Update character configuration
        self.character_name = character_name
        self.character_config = self.config.get_character_config(character_name)
        
        # Update LLM client with new character
        if self.llm_client:
            # Use update_character/set_character if available (for GeminiCLIBackend etc.)
            if hasattr(self.llm_client, 'update_character'):
                self.llm_client.update_character(yaml_filename)
                print(f"[BaseAssistant] LLMキャラクターを更新しました (update_character)")
            elif hasattr(self.llm_client, 'set_character'):
                self.llm_client.set_character(character_name)
                print(f"[BaseAssistant] LLMキャラクターを更新しました (set_character)")
            else:
                # Fallback: set_system_prompt for non-CLI backends
                personality = self.character_config.get('personality', {})
                system_prompt = personality.get('details', 'あなたは親切なAIアシスタントです。')
                self.llm_client.set_system_prompt(system_prompt)
                print(f"[BaseAssistant] LLMのシステムプロンプトを更新しました")
        
        # Update character manager's current state
        manager = get_character_manager()
        manager._current_character = character_name
        manager._current_yaml = yaml_filename
        
    async def cleanup(self):
        """Cleanup resources"""
        self.running = False
        
        # Get goodbye message
        personality = self.character_config.get('personality', {})
        goodbye = personality.get('goodbyeReply', 'さようなら！')
        
        await self._cleanup_mode_specific()
        
        # Cleanup LLM client (including MCP)
        if hasattr(self.llm_client, 'cleanup'):
            try:
                await self.llm_client.cleanup()
            except Exception as e:
                print(f"LLMクライアントのクリーンアップエラー: {e}")
        
        print(f"\n{self.character_name}: {goodbye}")
        
    @abstractmethod
    async def _cleanup_mode_specific(self):
        """Cleanup mode-specific resources"""
        pass
        
    async def _generate_with_interrupt_check(self, text: str, task_id: str = "unknown", parent_task = None) -> Optional[str]:
        """Generate response with task-specific cancellation checking
        
        Args:
            text: Input text
            task_id: Task identifier for logging
            parent_task: Parent asyncio task for cancellation check
            
        Returns:
            Generated response or None if cancelled
        """
        # Check if parent task was cancelled before starting
        if parent_task and parent_task.cancelled():
            print(f"[{task_id}] 親タスクキャンセル済み - 応答生成をスキップ")
            return None
            
        try:
            # Direct async call instead of executor to allow proper cancellation
            if hasattr(self.llm_client, 'generate_response_async'):
                # Use async version if available
                response = await self.llm_client.generate_response_async(text)
            else:
                # Fallback: create a task that can be cancelled
                generation_task = asyncio.create_task(
                    asyncio.to_thread(lambda: self.llm_client.generate_response(text, stream=False))
                )
                
                # Monitor for parent task cancellation during generation
                while not generation_task.done():
                    if parent_task and parent_task.cancelled():
                        print(f"[{task_id}] 応答生成中に親タスクキャンセル検出")
                        generation_task.cancel()
                        try:
                            await generation_task
                        except asyncio.CancelledError:
                            pass
                        return None
                    await asyncio.sleep(0.05)  # Check every 50ms
                
                response = await generation_task
                
            # Final cancellation check
            if parent_task and parent_task.cancelled():
                print(f"[{task_id}] 応答生成完了後に親タスクキャンセル検出")
                return None
                
            return response
            
        except asyncio.CancelledError:
            print(f"[{task_id}] 応答生成タスクがキャンセルされました")
            return None
        except Exception as e:
            print(f"[{task_id}] 応答生成エラー: {e}")
            return None
