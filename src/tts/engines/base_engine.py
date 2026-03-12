"""TTSエンジンの基底クラス

全てのTTSエンジンが継承すべき共通インターフェースと処理を定義
"""
import asyncio
import platform
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Union, Callable
from contextlib import asynccontextmanager
import time

from ...utils.exceptions import TTSError, ConnectionError as AoiTalkConnectionError
from ...utils.logger import get_logger


class BaseTTSEngine(ABC):
    """TTSエンジンの基底クラス
    
    全てのTTSエンジンに共通する機能を提供：
    - 初期化と終了処理
    - パラメータ検証
    - エラーハンドリング
    - リトライロジック
    - ログ出力
    """
    
    # デフォルトのパラメータ範囲
    DEFAULT_SPEED_RANGE = (0.5, 4.0)
    DEFAULT_PITCH_RANGE = (0.5, 2.0)
    DEFAULT_VOLUME_RANGE = (0.0, 2.0)
    DEFAULT_INTONATION_RANGE = (0.0, 2.0)
    
    # リトライ設定
    DEFAULT_RETRY_COUNT = 3
    DEFAULT_RETRY_DELAY = 1.0  # 秒
    
    def __init__(self, engine_name: str, **config):
        """基底クラスの初期化
        
        Args:
            engine_name: エンジン名（ログ出力用）
            **config: エンジン固有の設定
        """
        self.engine_name = engine_name
        self.config = config
        self.logger = get_logger(f"tts.{engine_name}")
        
        # 初期化状態
        self._initialized = False
        
        # 利用可能な音声リスト
        self._voices: List[Union[str, Dict[str, Any]]] = []
        
        # 現在の音声設定
        self._current_voice = None
        
        # パラメータ範囲（サブクラスでカスタマイズ可能）
        self.speed_range = self.DEFAULT_SPEED_RANGE
        self.pitch_range = self.DEFAULT_PITCH_RANGE
        self.volume_range = self.DEFAULT_VOLUME_RANGE
        self.intonation_range = self.DEFAULT_INTONATION_RANGE
        
        # プラットフォームチェック
        self._platform = platform.system()
        
    @abstractmethod
    async def _initialize_impl(self) -> bool:
        """エンジン固有の初期化処理（サブクラスで実装）
        
        Returns:
            初期化が成功したかどうか
        """
        pass
        
    @abstractmethod
    async def _synthesize_impl(self, text: str, **params) -> Optional[bytes]:
        """エンジン固有の音声合成処理（サブクラスで実装）
        
        Args:
            text: 合成するテキスト
            **params: エンジン固有のパラメータ
            
        Returns:
            音声データ（WAV形式）またはNone
        """
        pass
        
    @abstractmethod
    async def _cleanup_impl(self) -> None:
        """エンジン固有のクリーンアップ処理（サブクラスで実装）"""
        pass
        
    async def initialize(self) -> bool:
        """TTSエンジンを初期化
        
        Returns:
            初期化が成功したかどうか
        """
        if self._initialized:
            self.logger.debug(f"{self.engine_name} is already initialized")
            return True
            
        try:
            self.logger.info(f"Initializing {self.engine_name}...")
            
            # プラットフォームチェック
            if not self._check_platform():
                return False
                
            # エンジン固有の初期化
            success = await self._initialize_impl()
            
            if success:
                self._initialized = True
                self.logger.info(f"{self.engine_name} initialized successfully")
                
                # 利用可能な音声リストを取得
                await self._update_voices()
            else:
                self.logger.error(f"Failed to initialize {self.engine_name}")
                
            return success
            
        except Exception as e:
            self.logger.error(f"Error initializing {self.engine_name}: {e}")
            raise TTSError(f"Failed to initialize {self.engine_name}", 
                          engine=self.engine_name) from e
            
    async def synthesize(self, text: str, **params) -> Optional[bytes]:
        """テキストを音声に変換
        
        Args:
            text: 合成するテキスト
            **params: 音声合成パラメータ
            
        Returns:
            音声データ（WAV形式）またはNone
        """
        if not self._initialized:
            self.logger.error(f"{self.engine_name} is not initialized")
            return None
            
        if not text or not text.strip():
            self.logger.warning("Empty text provided for synthesis")
            return None
            
        try:
            # パラメータの検証と正規化
            validated_params = self._validate_parameters(params)
            
            # リトライロジック付きで音声合成
            retry_count = self.config.get('retry_count', self.DEFAULT_RETRY_COUNT)
            retry_delay = self.config.get('retry_delay', self.DEFAULT_RETRY_DELAY)
            
            for attempt in range(retry_count):
                try:
                    self.logger.debug(f"Synthesizing text (attempt {attempt + 1}/{retry_count}): {text[:50]}...")
                    
                    result = await self._synthesize_impl(text, **validated_params)
                    
                    if result:
                        self.logger.debug(f"Successfully synthesized {len(result)} bytes of audio")
                        return result
                    else:
                        self.logger.warning(f"Synthesis returned no data (attempt {attempt + 1})")
                        
                except Exception as e:
                    if attempt < retry_count - 1:
                        self.logger.warning(
                            f"Synthesis attempt {attempt + 1} failed: {e}. "
                            f"Retrying in {retry_delay}s..."
                        )
                        await asyncio.sleep(retry_delay)
                    else:
                        raise
                        
            return None
            
        except Exception as e:
            self.logger.error(f"Error synthesizing text: {e}")
            raise TTSError(
                f"Failed to synthesize text with {self.engine_name}",
                engine=self.engine_name,
                character=params.get('voice_name', self._current_voice)
            ) from e
            
    async def cleanup(self) -> None:
        """リソースをクリーンアップ"""
        if not self._initialized:
            return
            
        try:
            self.logger.info(f"Cleaning up {self.engine_name}...")
            await self._cleanup_impl()
            self._initialized = False
            self._voices = []
            self._current_voice = None
            self.logger.info(f"{self.engine_name} cleaned up successfully")
            
        except Exception as e:
            self.logger.error(f"Error cleaning up {self.engine_name}: {e}")
            # クリーンアップエラーは握りつぶす（リソースリークを避けるため）
            
    def get_voices(self) -> List[Union[str, Dict[str, Any]]]:
        """利用可能な音声のリストを取得
        
        Returns:
            音声のリスト（文字列または辞書）
        """
        return self._voices.copy()
        
    def set_voice(self, voice: Union[str, int, Dict[str, Any]]) -> bool:
        """音声を設定
        
        Args:
            voice: 音声名、インデックス、または音声情報の辞書
            
        Returns:
            設定が成功したかどうか
        """
        try:
            if isinstance(voice, int):
                # インデックスで指定
                if 0 <= voice < len(self._voices):
                    self._current_voice = self._voices[voice]
                    return True
            elif isinstance(voice, str):
                # 名前で指定
                for v in self._voices:
                    if isinstance(v, str) and v == voice:
                        self._current_voice = v
                        return True
                    elif isinstance(v, dict) and v.get('name') == voice:
                        self._current_voice = v
                        return True
            elif isinstance(voice, dict):
                # 辞書で指定
                self._current_voice = voice
                return True
                
            self.logger.warning(f"Voice not found: {voice}")
            return False
            
        except Exception as e:
            self.logger.error(f"Error setting voice: {e}")
            return False
            
    def _check_platform(self) -> bool:
        """プラットフォームの互換性をチェック
        
        Returns:
            このプラットフォームでエンジンが使用可能か
        """
        # デフォルトでは全プラットフォーム対応
        # サブクラスでオーバーライド可能
        return True
        
    def _validate_parameters(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """パラメータを検証して正規化
        
        Args:
            params: 入力パラメータ
            
        Returns:
            検証済みパラメータ
        """
        validated = params.copy()
        
        # 速度の検証
        if 'speed' in validated:
            validated['speed'] = self._clamp_value(
                validated['speed'], *self.speed_range, 'speed'
            )
            
        # ピッチの検証
        if 'pitch' in validated:
            validated['pitch'] = self._clamp_value(
                validated['pitch'], *self.pitch_range, 'pitch'
            )
            
        # 音量の検証
        if 'volume' in validated:
            validated['volume'] = self._clamp_value(
                validated['volume'], *self.volume_range, 'volume'
            )
            
        # 抑揚の検証
        if 'intonation' in validated:
            validated['intonation'] = self._clamp_value(
                validated['intonation'], *self.intonation_range, 'intonation'
            )
            
        return validated
        
    def _clamp_value(self, value: float, min_val: float, max_val: float, 
                     param_name: str) -> float:
        """値を指定範囲内にクランプ
        
        Args:
            value: 入力値
            min_val: 最小値
            max_val: 最大値
            param_name: パラメータ名（ログ用）
            
        Returns:
            クランプされた値
        """
        original = value
        clamped = max(min_val, min(max_val, value))
        
        if clamped != original:
            self.logger.debug(
                f"{param_name} clamped from {original} to {clamped} "
                f"(range: {min_val}-{max_val})"
            )
            
        return clamped
        
    async def _update_voices(self) -> None:
        """利用可能な音声リストを更新（サブクラスでオーバーライド可能）"""
        # デフォルトでは何もしない
        # サブクラスで音声リストの取得処理を実装
        pass
        
    # コンテキストマネージャーサポート
    async def __aenter__(self):
        """非同期コンテキストマネージャーの開始"""
        await self.initialize()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """非同期コンテキストマネージャーの終了"""
        await self.cleanup()
        
    def __enter__(self):
        """同期コンテキストマネージャーの開始"""
        asyncio.run(self.initialize())
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """同期コンテキストマネージャーの終了"""
        asyncio.run(self.cleanup())


class BaseServerTTSEngine(BaseTTSEngine):
    """サーバー型TTSエンジンの基底クラス
    
    HTTPサーバーとして動作するTTSエンジン用の共通機能を提供
    """
    
    def __init__(self, engine_name: str, default_port: int, **config):
        """サーバー型エンジンの初期化
        
        Args:
            engine_name: エンジン名
            default_port: デフォルトポート番号
            **config: エンジン固有の設定
        """
        super().__init__(engine_name, **config)
        
        self.host = config.get('host', 'localhost')
        self.port = config.get('port', default_port)
        self.process = None
        self.base_url = f"http://{self.host}:{self.port}"
        
    @abstractmethod
    async def _start_server(self) -> bool:
        """サーバープロセスを起動（サブクラスで実装）
        
        Returns:
            起動が成功したかどうか
        """
        pass
        
    @abstractmethod
    async def _stop_server(self) -> None:
        """サーバープロセスを停止（サブクラスで実装）"""
        pass
        
    def _is_port_in_use(self, port: int) -> bool:
        """ポートが使用中かチェック
        
        Args:
            port: チェックするポート番号
            
        Returns:
            使用中かどうか
        """
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))
                return False
            except:
                return True
                
    def _kill_process_using_port(self, port: int) -> bool:
        """指定ポートを使用しているプロセスを終了
        
        Args:
            port: ポート番号
            
        Returns:
            終了が成功したかどうか
        """
        try:
            if self._platform == "Windows":
                import subprocess
                # netstatでポートを使用しているプロセスを検索
                result = subprocess.run(
                    ['netstat', '-ano'], 
                    capture_output=True, 
                    text=True
                )
                for line in result.stdout.split('\n'):
                    if f':{port}' in line and 'LISTENING' in line:
                        # PIDを抽出
                        parts = line.split()
                        if parts:
                            pid = parts[-1]
                            # プロセスを終了
                            subprocess.run(['taskkill', '/F', '/PID', pid])
                            self.logger.info(f"Killed process {pid} using port {port}")
                            return True
            else:
                # Linux/Mac
                import subprocess
                result = subprocess.run(
                    ['lsof', '-i', f':{port}'], 
                    capture_output=True, 
                    text=True
                )
                for line in result.stdout.split('\n')[1:]:  # ヘッダーをスキップ
                    if line:
                        parts = line.split()
                        if len(parts) > 1:
                            pid = parts[1]
                            subprocess.run(['kill', '-9', pid])
                            self.logger.info(f"Killed process {pid} using port {port}")
                            return True
                            
        except Exception as e:
            self.logger.error(f"Error killing process using port {port}: {e}")
            
        return False
        
    async def _wait_for_server(self, timeout: float = 30.0) -> bool:
        """サーバーの起動を待機
        
        Args:
            timeout: タイムアウト時間（秒）
            
        Returns:
            サーバーが起動したかどうか
        """
        import aiohttp
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    # ヘルスチェックエンドポイントにアクセス
                    async with session.get(
                        f"{self.base_url}/",
                        timeout=aiohttp.ClientTimeout(total=2.0)
                    ) as response:
                        if response.status < 500:  # サーバーエラー以外
                            return True
                            
            except:
                # 接続エラーは無視（まだ起動していない）
                pass
                
            await asyncio.sleep(0.5)
            
        return False