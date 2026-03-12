from typing import Optional, Union, AsyncGenerator
import asyncio
import numpy as np
from ...tts.manager import TTSManager
from ..interfaces.output import AudioOutputInterface


class TTSPipeline:
    """統一TTS処理パイプライン（ストリーミング対応）"""
    
    def __init__(self, tts_manager: TTSManager, output_adapter: AudioOutputInterface):
        """TTSパイプラインを初期化
        
        Args:
            tts_manager: TTSマネージャー
            output_adapter: 音声出力アダプター
        """
        self.tts_manager = tts_manager
        self.output_adapter = output_adapter
        
        # ストリーミング用の状態
        self._is_streaming = False
        self._stream_task: Optional[asyncio.Task] = None
        
    async def process_and_play(self, 
                             text: str, 
                             character: str,
                             speed: float = 1.0,
                             pitch: float = 0.0,
                             intonation: float = 1.0,
                             volume: float = 1.0) -> bool:
        """テキストをTTS処理して再生
        
        Args:
            text: 読み上げるテキスト
            character: キャラクター名
            speed: 話速（0.5-2.0）
            pitch: ピッチ（-300～300）
            intonation: 抑揚（0.0-2.0）
            volume: 音量（0.0-1.0）
            
        Returns:
            成功したかどうか
        """
        try:
            # TTS生成（共通処理）
            audio_data = await self.tts_manager.synthesize(
                text=text,
                character_name=character,
                speed=speed,
                pitch=pitch,
                intonation=intonation,
                volume=volume
            )
            
            if not audio_data:
                print(f"[TTSPipeline] Failed to synthesize audio for: {text}")
                return False
                
            # 音量設定
            self.output_adapter.set_volume(volume)
            
            # 再生（アダプター経由）
            await self.output_adapter.play(audio_data, format="wav")
            
            return True
            
        except Exception as e:
            print(f"[TTSPipeline] Error processing TTS: {e}")
            return False
            
    async def stop(self) -> None:
        """再生を停止"""
        await self.output_adapter.stop()
        
    def is_playing(self) -> bool:
        """再生中かどうか"""
        return self.output_adapter.is_playing()
        
    async def set_character(self, character: str) -> bool:
        """デフォルトキャラクターを設定
        
        Args:
            character: キャラクター名
            
        Returns:
            設定が成功したかどうか
        """
        # TTSマネージャーでキャラクターが設定されているか確認
        if hasattr(self.tts_manager, 'character_configs'):
            if character in self.tts_manager.character_configs:
                return True
            else:
                print(f"[TTSPipeline] Character '{character}' not found in configuration")
                return False
        else:
            # キャラクター設定がない場合はTrueを返す（デフォルト動作）
            return True
            
    async def process_and_play_stream(self, 
                                    text: str, 
                                    character: str,
                                    speed: float = 1.0,
                                    pitch: float = 0.0,
                                    intonation: float = 1.0,
                                    volume: float = 1.0,
                                    chunk_size: int = 1024) -> bool:
        """テキストをTTS処理してストリーミング再生
        
        Args:
            text: 読み上げるテキスト
            character: キャラクター名
            speed: 話速（0.5-2.0）
            pitch: ピッチ（-300～300）
            intonation: 抑揚（0.0-2.0）
            volume: 音量（0.0-1.0）
            chunk_size: ストリーミングチャンクサイズ
            
        Returns:
            成功したかどうか
        """
        try:
            # 既存のストリーミングを停止
            await self.stop_stream()
            
            self._is_streaming = True
            
            # 音量設定
            self.output_adapter.set_volume(volume)
            
            # ストリーミングTTSがサポートされているか確認
            if hasattr(self.tts_manager, 'synthesize_stream'):
                # ストリーミング生成と再生を並行実行
                self._stream_task = asyncio.create_task(
                    self._stream_worker(
                        text, character, speed, pitch, 
                        intonation, volume, chunk_size
                    )
                )
                return True
            else:
                # ストリーミングがサポートされていない場合は通常の再生
                self._is_streaming = False
                return await self.process_and_play(
                    text, character, speed, pitch, intonation, volume
                )
                
        except Exception as e:
            print(f"[TTSPipeline] Error in streaming TTS: {e}")
            self._is_streaming = False
            return False
            
    async def _stream_worker(self, text: str, character: str,
                           speed: float, pitch: float,
                           intonation: float, volume: float,
                           chunk_size: int) -> None:
        """ストリーミングワーカー"""
        try:
            # バッファキュー
            audio_queue = asyncio.Queue(maxsize=5)
            
            # TTS生成タスク
            async def generate_audio():
                async for chunk in self.tts_manager.synthesize_stream(
                    text=text,
                    character_name=character,
                    speed=speed,
                    pitch=pitch,
                    intonation=intonation,
                    volume=volume,
                    chunk_size=chunk_size
                ):
                    if not self._is_streaming:
                        break
                    await audio_queue.put(chunk)
                await audio_queue.put(None)  # 終了シグナル
                
            # 再生タスク
            async def play_audio():
                while self._is_streaming:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        break
                        
                    # チャンクを再生
                    if hasattr(self.output_adapter, 'play_chunk'):
                        await self.output_adapter.play_chunk(chunk)
                    else:
                        # play_chunkがない場合は通常のplayを使用
                        await self.output_adapter.play(chunk, format="wav")
                        # 次のチャンクまで少し待機
                        await asyncio.sleep(0.1)
                        
            # 生成と再生を並行実行
            await asyncio.gather(
                generate_audio(),
                play_audio()
            )
            
        except Exception as e:
            print(f"[TTSPipeline] Error in stream worker: {e}")
        finally:
            self._is_streaming = False
            
    async def stop_stream(self) -> None:
        """ストリーミングを停止"""
        self._is_streaming = False
        
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
                
        await self.output_adapter.stop()
        
    def is_streaming(self) -> bool:
        """ストリーミング中かどうか"""
        return self._is_streaming
