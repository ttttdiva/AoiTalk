"""
Enhanced voice chat mode using unified audio interfaces
"""

import asyncio
import numpy as np
from typing import Optional
from ..base_v2 import EnhancedBaseAssistant
from ...audio.pipeline import VoiceActivityDetector
from ...audio.interfaces import AudioInputInterface, AudioOutputInterface
from ...session.base import BaseSession


class EnhancedVoiceChatMode(EnhancedBaseAssistant):
    """Enhanced voice chat mode with unified audio interfaces"""
    
    def __init__(self,
                 config,
                 audio_input: AudioInputInterface,
                 audio_output: AudioOutputInterface,
                 session: BaseSession):
        """Initialize enhanced voice chat mode
        
        Args:
            config: Configuration object
            audio_input: Audio input interface
            audio_output: Audio output interface
            session: Session object
        """
        super().__init__(
            config=config,
            mode='voice_chat',
            audio_input=audio_input,
            audio_output=audio_output,
            session=session
        )
        
        # Voice activity detector
        self.vad = VoiceActivityDetector(
            sample_rate=audio_input.get_sample_rate(),
            silence_threshold=config.get('speech_recognition.silence_threshold', 0.01),
            speech_threshold=config.get('speech_recognition.speech_threshold', 0.02),
            silence_duration=config.get('speech_recognition.silence_duration', 1.5)
        )
        
        # Audio buffer for continuous recording
        self.audio_buffer = []
        self.max_buffer_duration = 30.0  # 最大30秒
        
    async def _initialize_mode_specific(self) -> bool:
        """Initialize voice chat specific components"""
        try:
            # Test TTS
            print("音声合成エンジンのテスト中...")
            test_message = self.character_config.get('personality', {}).get(
                'welcomeReply', f"こんにちは！{self.character_name}です。"
            )
            
            success = await self.speak(test_message)
            if not success:
                print("⚠️ 音声合成エンジンのテストに失敗しました")
                return False
                
            print("✓ 音声合成エンジンの初期化完了")
            
            # Test speech recognition
            if self.speech_recognition:
                print("✓ 音声認識エンジンの初期化完了")
            else:
                print("❌ 音声認識エンジンの初期化に失敗")
                return False
                
            return True
            
        except Exception as e:
            print(f"Mode initialization error: {e}")
            return False
            
    async def run(self):
        """Run voice chat mode"""
        self.running = True
        print("\n音声チャットモードを開始しました。話しかけてください。")
        print("終了するには Ctrl+C を押してください。\n")
        
        # Set audio callback for continuous processing
        self.audio_input.set_callback(self._audio_callback)
        
        try:
            while self.running:
                # Process audio buffer periodically
                await self._process_audio_buffer()
                await asyncio.sleep(0.1)
                
        except KeyboardInterrupt:
            print("\n\n音声チャットを終了します...")
        finally:
            self.running = False
            
    def _audio_callback(self, audio_chunk: np.ndarray):
        """Callback for audio input
        
        Args:
            audio_chunk: Audio data chunk
        """
        if not self.running:
            return
            
        # Add to buffer
        self.audio_buffer.append(audio_chunk)
        
        # Limit buffer size
        total_samples = sum(len(chunk) for chunk in self.audio_buffer)
        max_samples = int(self.max_buffer_duration * self.audio_input.get_sample_rate())
        
        if total_samples > max_samples:
            # Remove old chunks
            while total_samples > max_samples and self.audio_buffer:
                removed = self.audio_buffer.pop(0)
                total_samples -= len(removed)
                
    async def _process_audio_buffer(self):
        """Process accumulated audio buffer"""
        if not self.audio_buffer:
            return
            
        # Concatenate audio chunks
        audio_data = np.concatenate(self.audio_buffer)
        
        # Process with VAD
        has_speech, start_idx, end_idx = self.vad.process_audio(audio_data)
        
        if has_speech and start_idx >= 0 and end_idx > start_idx:
            # Extract speech segment
            speech_segment = audio_data[start_idx:end_idx]
            
            # Clear processed audio from buffer
            samples_to_remove = end_idx
            self._remove_samples_from_buffer(samples_to_remove)
            
            # Reset VAD state
            self.vad.reset()
            
            # Process speech
            await self._process_speech(speech_segment)
            
    def _remove_samples_from_buffer(self, num_samples: int):
        """Remove specified number of samples from buffer
        
        Args:
            num_samples: Number of samples to remove
        """
        removed = 0
        while removed < num_samples and self.audio_buffer:
            chunk = self.audio_buffer[0]
            if len(chunk) <= num_samples - removed:
                # Remove entire chunk
                self.audio_buffer.pop(0)
                removed += len(chunk)
            else:
                # Remove partial chunk
                remaining = num_samples - removed
                self.audio_buffer[0] = chunk[remaining:]
                removed += remaining
                
    async def _process_speech(self, audio_data: np.ndarray):
        """Process detected speech
        
        Args:
            audio_data: Speech audio data
        """
        print("🎤 音声を検出しました...")
        
        # Recognize speech
        if self.speech_recognition:
            try:
                text = await self.speech_recognition.recognize_audio(audio_data)
                
                if text:
                    print(f"📝 認識結果: {text}")
                    
                    # Update session activity
                    self.session.update_activity()
                    
                    # Get response from LLM
                    print("🤔 応答を生成中...")
                    response = await self.llm_client.get_response(text)
                    
                    if response:
                        print(f"💬 {self.character_name}: {response}")
                        
                        # Speak response
                        await self.speak(response)
                    else:
                        print("⚠️ 応答の生成に失敗しました")
                else:
                    print("⚠️ 音声認識に失敗しました")
                    
            except Exception as e:
                print(f"❌ エラー: {e}")
                
    async def _cleanup_mode_specific(self):
        """Cleanup voice chat specific resources"""
        # Clear audio callback
        self.audio_input.set_callback(None)
        
        # Clear audio buffer
        self.audio_buffer.clear()