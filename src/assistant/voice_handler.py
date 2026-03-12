"""
Voice handling for AoiTalk Voice Assistant Framework
"""

import asyncio
import queue
import threading
import time
import numpy as np
from collections import deque
from typing import Optional, Callable, Any


class VoiceHandler:
    """Handles all voice-related operations including recording, interrupt detection, and echo cancellation"""
    
    def __init__(self, config, recognizer, player):
        """Initialize voice handler
        
        Args:
            config: Configuration object
            recognizer: Speech recognition manager
            player: Audio player
        """
        self.config = config
        self.recognizer = recognizer
        self.player = player
        
        # Audio queue for continuous recording
        self.audio_queue = queue.Queue()
        
        # Recording state
        self.recording_thread = None
        self.is_speaking = False
        self._recording_active = False
        self.interrupt_flag = False
        self.pre_interrupt_flag = False
        self.pre_interrupt_start_time = None
        self.early_verification_done = False
        self.mid_verification_done = False
        
        # Audio buffer for continuous recording
        self.audio_buffer = deque(maxlen=int(16000 * 30))  # 30 seconds max
        self.voice_segments = []
        
        # Speech recognition for interrupt validation (reuse main recognizer)
        self.quick_recognizer = self.recognizer
        
        # Acoustic Echo Cancellation (AEC) System
        current_time = time.time()
        self.playback_started_time = current_time - 100  # Set to past time to avoid initial trigger
        self.playback_finished_time = current_time - 100  # Set to past time to avoid initial trigger
        self.min_voice_duration = 0.4  # Reduced minimum duration for better responsiveness
        
        # Acoustic echo cancellation buffer
        self.playback_audio_buffer = deque(maxlen=int(44100 * 3))  # 3 seconds of playback audio at 44.1kHz
        self.playback_timestamps = deque(maxlen=int(44100 * 3 / 2048))  # Timestamps for audio chunks
        self.echo_correlation_threshold = 0.7  # Correlation threshold for echo detection
        self.echo_attenuation_factor = 0.1  # How much to attenuate detected echo (90% reduction)
        self.aec_enabled = True  # Enable/disable AEC system
        
        # Simple fallback for immediate post-playback period
        self.immediate_echo_grace_period = 0.2  # Very short grace period for immediate echo only
        
        # Debug counter for initial echo prevention checks
        self.echo_debug_counter = 0
        
        # Event callback for processed audio
        self.audio_callback: Optional[Callable] = None
        
        # Callback for immediate transcription display
        self.transcription_callback: Optional[Callable] = None
        
        # Initialize keyword detection system
        self._setup_keyword_detection()
    
    def _setup_keyword_detection(self):
        """キーワード検出システムをセットアップ"""
        try:
            from ..tools.keyword.initializer import setup_keyword_detection
            setup_keyword_detection(self.config)
        except Exception as e:
            print(f"[VoiceHandler] キーワード検出システムの初期化に失敗: {e}")
            # エラーが発生してもvoice_handlerは動作を続行
        
    def set_audio_callback(self, callback: Callable):
        """Set callback for processed audio segments
        
        Args:
            callback: Function to call with (text, audio_type) when audio is processed
        """
        self.audio_callback = callback
        
    def set_transcription_callback(self, callback: Callable):
        """Set callback for immediate transcription display
        
        Args:
            callback: Function to call with transcribed text immediately after recognition
        """
        self.transcription_callback = callback
        
    def start_recording(self, rms_callback: Optional[Callable[[float], None]] = None):
        """Start continuous recording thread
        
        Args:
            rms_callback: Optional callback function to receive RMS values
        """
        if self.recording_thread and self.recording_thread.is_alive():
            return
            
        self.rms_callback = rms_callback
        self._recording_active = True
        self.recording_thread = threading.Thread(target=self.continuous_recording_thread)
        self.recording_thread.daemon = True
        self.recording_thread.start()
        
    def stop_recording(self):
        """Stop recording"""
        self._recording_active = False
        
    def _quick_verify_interrupt_audio(self, audio_bytes, min_duration=0.3):
        """Ultra-fast verification for early interrupt detection"""
        try:
            # Convert bytes to numpy array
            audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
            
            # Very basic checks only - prioritize speed
            duration = len(audio_data) / 16000
            if duration < min_duration:
                return False
                
            # Calculate RMS energy quickly
            rms_energy = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            if rms_energy < 0.002:  # Slightly higher threshold for early detection
                return False
            
            # Super fast speech recognition with maximum speed settings
            try:
                # Backup original settings
                original_min_duration = self.quick_recognizer.min_audio_duration
                original_energy_threshold = self.quick_recognizer.energy_threshold 
                original_hallucination_detection = self.quick_recognizer.hallucination_detection
                
                # Set ultra-fast validation settings
                self.quick_recognizer.min_audio_duration = 0.05  # Very short minimum
                self.quick_recognizer.energy_threshold = 0.001   # Low threshold
                self.quick_recognizer.hallucination_detection = True  # Still check for hallucinations
                
                # Try recognition with timeout for speed
                text = self.quick_recognizer.recognize(audio_data, sample_rate=16000)
                
                # Restore settings immediately
                self.quick_recognizer.min_audio_duration = original_min_duration
                self.quick_recognizer.energy_threshold = original_energy_threshold
                self.quick_recognizer.hallucination_detection = original_hallucination_detection
                
                if text and len(text.strip()) > 0:
                    # Quick hallucination check
                    if self.quick_recognizer.hallucination_filter.is_hallucination(
                        text, engine=self.quick_recognizer.engine_name
                    ):
                        print(f"[早期検証] ハルシネーション検出: '{text}'")
                        return False
                    
                    print(f"[早期検証] 有効な音声を早期検出: '{text[:20]}...'")
                    return True
                else:
                    return False
                    
            except Exception as e:
                print(f"[早期検証] 音声認識エラー: {e}")
                return False
                
        except Exception as e:
            print(f"[早期検証] 検証エラー: {e}")
            return False
    
    def _verify_interrupt_audio(self, audio_bytes):
        """Quickly verify if interrupt audio contains valid speech"""
        try:
            # Convert bytes to numpy array
            audio_data = np.frombuffer(audio_bytes, dtype=np.int16)
            
            # Basic audio quality checks
            if len(audio_data) < 4800:  # Less than 0.3 seconds at 16kHz
                return False
                
            # Calculate RMS energy
            rms_energy = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            if rms_energy < 0.001:  # Very low energy threshold for interrupts
                return False
            
            # Quick speech recognition with minimal settings
            try:
                # Use very permissive settings for interrupt validation
                original_min_duration = self.quick_recognizer.min_audio_duration
                original_energy_threshold = self.quick_recognizer.energy_threshold 
                original_hallucination_detection = self.quick_recognizer.hallucination_detection
                
                # Set lenient validation for quick check
                self.quick_recognizer.min_audio_duration = 0.1
                self.quick_recognizer.energy_threshold = 0.0001
                self.quick_recognizer.hallucination_detection = True  # Keep hallucination detection
                
                text = self.quick_recognizer.recognize(audio_data, sample_rate=16000)
                
                # Restore original settings
                self.quick_recognizer.min_audio_duration = original_min_duration
                self.quick_recognizer.energy_threshold = original_energy_threshold
                self.quick_recognizer.hallucination_detection = original_hallucination_detection
                
                if text and len(text.strip()) > 0:
                    # Check for hallucination
                    if self.quick_recognizer.hallucination_filter.is_hallucination(
                        text, engine=self.quick_recognizer.engine_name
                    ):
                        print(f"[割り込み検証] ハルシネーション検出: '{text}'")
                        return False
                    
                    print(f"[割り込み検証] 有効な音声を検出: '{text}'")
                    return True
                else:
                    print(f"[割り込み検証] 音声認識結果なし")
                    return False
                    
            except Exception as e:
                print(f"[割り込み検証] 音声認識エラー: {e}")
                return False
                
        except Exception as e:
            print(f"[割り込み検証] 検証エラー: {e}")
            return False
    
    def continuous_recording_thread(self):
        """Thread for continuous audio recording"""
        import pyaudio
        
        # Use 16kHz directly to avoid resampling overhead
        CHUNK = 1024   # Optimized chunk size for 16kHz
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000   # Direct 16kHz recording for speech recognition
        
        p = pyaudio.PyAudio()
        
        # Try to open stream with error handling
        stream = None
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries:
            try:
                # Try to activate audio source first
                try:
                    import subprocess
                    subprocess.run(['pactl', 'set-source-mute', 'RDPSource', '0'], check=False)
                    subprocess.run(['pactl', 'set-source-volume', 'RDPSource', '100%'], check=False)
                    print("[録音スレッド] PulseAudio設定を初期化")
                except:
                    pass
                
                # Configure stream with PulseAudio optimizations
                device_index = getattr(self.config, 'device_index', None)
                stream = p.open(
                    format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    input_device_index=device_index if device_index is not None else 0,
                    frames_per_buffer=CHUNK,
                    stream_callback=None,
                    start=False  # Don't start immediately to prevent initial noise
                )
                # Start stream after configuration
                stream.start_stream()
                print("[録音スレッド] 音声入力を開始しました")
                
                # Let stream settle to avoid initial noise burst
                time.sleep(0.1)
                break
            except Exception as e:
                retry_count += 1
                print(f"[録音スレッド] ストリーム開始エラー (試行 {retry_count}/{max_retries}): {e}")
                time.sleep(1)
        
        if not stream:
            print("[録音スレッド] 音声入力の開始に失敗しました")
            print("[録音スレッド] WSL2環境では音声入力デバイスが利用できない可能性があります")
            print("[録音スレッド] チャットモードのみで動作します")
            return
        
        # Main recording loop with adaptive thresholds
        self._run_recording_loop(stream, CHUNK, RATE, p)
        
    def _run_recording_loop(self, stream, CHUNK, RATE, p):
        """Main recording loop"""
        # Continuous recording loop - Adjusted thresholds for high ambient noise environment
        # Allow configuration from config file or use defaults that work with RMS 20-60 ambient noise
        speech_config = getattr(self.config, 'speech_recognition', {})
        silence_threshold = speech_config.get('silence_threshold', 70.0)  # Above 60 ambient peak
        voice_threshold = speech_config.get('voice_threshold', 120.0)     # Well above 60 ambient noise
        silence_duration = speech_config.get('silence_duration', 1.5)     # Shorter for responsiveness
        
        print(f"[録音スレッド] 音声閾値: {voice_threshold}, 無音閾値: {silence_threshold}, 無音時間: {silence_duration}秒")
        print(f"[録音スレッド] 現在のノイズレベル観察から、音声入力時のRMSが{voice_threshold}を超えることを期待します")
        
        # Adaptive threshold calibration
        if speech_config.get('auto_calibrate', True):
            import time
            print("[録音スレッド] ノイズレベル測定中（3秒間）...")
            calibration_samples = []
            calibration_start = time.time()
            while time.time() - calibration_start < 3.0:
                try:
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    audio_data = np.frombuffer(data, dtype=np.int16)
                    rms = np.sqrt(np.mean(audio_data.astype(np.float64)**2))
                    calibration_samples.append(rms)
                except:
                    pass
            
            if calibration_samples:
                avg_noise = np.mean(calibration_samples)
                max_noise = np.max(calibration_samples)
                print(f"[録音スレッド] ノイズレベル測定完了: 平均={avg_noise:.2f}, 最大={max_noise:.2f}")
                
                # Auto-adjust thresholds based on measured noise
                if avg_noise > 10:  # High ambient noise detected
                    voice_threshold = max(voice_threshold, max_noise * 1.8)  # 80% above max noise
                    silence_threshold = max(silence_threshold, avg_noise * 1.3)  # 30% above avg noise
                    print(f"[録音スレッド] 自動調整: 音声閾値={voice_threshold:.1f}, 無音閾値={silence_threshold:.1f}")
                else:
                    print("[録音スレッド] 低ノイズ環境を検出、設定値を使用")
        
        current_segment = []
        voice_detected = False
        silence_start = None
        pre_voice_buffer = []  # Buffer to store audio before voice detection
        voice_start_time = None
        
        try:
            # Use a global running flag that can be checked
            while getattr(self, '_recording_active', True):
                try:
                    # Read audio chunk
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    
                    # Convert to numpy for RMS calculation and apply noise filtering
                    audio_data = np.frombuffer(data, dtype=np.int16)
                    
                    # Apply simple noise gate (adjusted for WSL)
                    noise_gate_threshold = 50  # Much lower threshold for WSL
                    audio_data = np.where(np.abs(audio_data) < noise_gate_threshold, 
                                        audio_data * 0.1, audio_data)  # Reduce low-level noise by 90%
                    
                    # Calculate RMS after noise filtering
                    rms = np.sqrt(np.mean(audio_data.astype(np.float64)**2))
                    
                    # Call RMS callback if provided
                    if hasattr(self, 'rms_callback') and self.rms_callback:
                        try:
                            self.rms_callback(rms)
                        except Exception as e:
                            print(f"[RMS Callback] Error: {e}")
                    
                    # Process audio chunk and update variables
                    voice_detected, silence_start, voice_start_time = self._process_audio_chunk(
                        audio_data, rms, current_segment, voice_detected, 
                        silence_start, pre_voice_buffer, voice_start_time,
                        voice_threshold, silence_threshold, silence_duration, CHUNK, RATE)
                    
                    # Visual feedback for all states
                    level = int(rms)  # Direct RMS for low values
                    bar = "=" * min(level, 30)
                    if self.is_speaking:
                        if voice_detected:
                            status = "[再生中:音声検出]"
                        else:
                            status = "[再生中]"
                    else:
                        if voice_detected:
                            status = f"[録音中] TH={voice_threshold}"
                        else:
                            status = f"[待機中] TH={voice_threshold}"
                    print(f"\r{status} 音声レベル: [{bar:<30}] RMS={rms:6.2f}", end="", flush=True)
                    
                    # Check for stop condition (this needs to be implemented based on parent context)
                    # For now, we'll use a simple approach
                    
                except Exception as e:
                    if "Input overflowed" in str(e):
                        continue  # Common error, ignore
                    else:
                        print(f"\n[録音スレッド] エラー: {type(e).__name__}: {e}")
                        time.sleep(0.1)
                        
        except Exception as e:
            print(f"\n[録音スレッド] 致命的エラー: {type(e).__name__}: {e}")
        finally:
            if stream:
                stream.stop_stream()
                stream.close()
            p.terminate()
            print("\n[録音スレッド] 終了しました")
    
    def _process_audio_chunk(self, audio_data, rms, current_segment, voice_detected, 
                           silence_start, pre_voice_buffer, voice_start_time,
                           voice_threshold, silence_threshold, silence_duration, CHUNK, RATE):
        """Process a single audio chunk"""
        # Use filtered audio data
        filtered_data = audio_data.astype(np.int16).tobytes()
        
        # Always add to pre-voice buffer (for leading audio capture)
        pre_voice_buffer.append(filtered_data)
        if len(pre_voice_buffer) > 15:  # Keep ~1 second of pre-audio
            pre_voice_buffer.pop(0)
        
        # Voice activity detection
        current_time = time.time()
        
        # Check for voice start
        if rms > voice_threshold and not self.is_speaking:
            if not voice_detected:
                voice_start_time = current_time
                voice_detected = True
                
                # Add pre-voice buffer to current segment for better context
                current_segment.extend(pre_voice_buffer)
                pre_voice_buffer.clear()
                
                print(f"\n[音声検出] 音声開始 RMS={rms:.2f}")
                
                # If we're speaking, interrupt playback
                if self.is_speaking or self.player.is_playing():
                    print(f"[音声検出] 再生中断")
                    self.interrupt_flag = True
                    self.player.stop()
                    self.is_speaking = False
            
            # Add current chunk to segment
            current_segment.append(filtered_data)
            silence_start = None
        
        # Check for voice continuation
        elif voice_detected and rms > voice_threshold:
            current_segment.append(filtered_data)
            silence_start = None
        
        # Check for silence (potential end of utterance)
        elif voice_detected and rms <= silence_threshold:
            current_segment.append(filtered_data)  # Include silence for natural endings
            
            if silence_start is None:
                silence_start = current_time
            elif current_time - silence_start > silence_duration:
                # End of utterance detected
                if len(current_segment) > 5:  # Minimum segments
                    voice_duration = current_time - voice_start_time if voice_start_time else 0
                    
                    if voice_duration > 0.2:  # Shorter minimum duration for responsiveness
                        # Convert to bytes and add to queue
                        audio_bytes = b''.join(current_segment)
                        print(f"\n[録音] キューに追加前: queue size={self.audio_queue.qsize()}")
                        self.audio_queue.put(('normal', audio_bytes, current_time))
                        print(f"[録音] 音声セグメント完了 ({len(audio_bytes)} bytes, {voice_duration:.1f}秒)")
                        print(f"[録音] キューに追加後: queue size={self.audio_queue.qsize()}")
                    else:
                        print(f"\n[録音] 短すぎる音声を除外 ({voice_duration:.1f}秒)")
                
                # Reset for next segment
                current_segment.clear()
                voice_detected = False
                silence_start = None
                voice_start_time = None
        
        # Return updated state variables
        return voice_detected, silence_start, voice_start_time
    
    async def process_audio_queue(self, running_flag):
        """Process audio segments from queue"""
        print("[VoiceHandler] process_audio_queue開始")
        while running_flag[0]:  # Use list reference for mutable flag
            try:
                # Wait for audio with timeout
                audio_item = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=1.0)
                )
                
                if audio_item:
                    # Unpack audio type, data, and timestamp
                    if isinstance(audio_item, tuple):
                        if len(audio_item) == 3:
                            audio_type, audio_data, timestamp = audio_item
                        else:
                            # Backward compatibility
                            audio_type, audio_data = audio_item
                            timestamp = time.time()
                    else:
                        # Backward compatibility for old format
                        audio_type, audio_data = 'normal', audio_item
                        timestamp = time.time()
                    
                    # Skip if this audio is too old (indicates queue backup)
                    current_time = time.time()
                    if current_time - timestamp > 5.0:  # 5秒以上古い音声は破棄
                        print(f"[キュー処理] 古い音声データを破棄 ({current_time - timestamp:.1f}秒前)")
                        continue
                    
                    # Always recognize speech first
                    print(f"\n音声認識中... (タイプ: {audio_type}, データサイズ: {len(audio_data)} bytes)")
                    
                    # Audio is already at 16kHz, no downsampling needed
                    try:
                        text = self.recognizer.recognize(audio_data, sample_rate=16000)
                        print(f"[VoiceHandler] 音声認識完了: '{text}' (None={text is None})")
                    except Exception as e:
                        print(f"[VoiceHandler] 音声認識エラー: {type(e).__name__}: {e}")
                        import traceback
                        traceback.print_exc()
                        text = None
                    
                    # デバッグ: 選択モード中の認識結果を確認
                    from ..tools.keyword import get_keyword_manager
                    manager = get_keyword_manager()
                    char_detector = manager.get_detector('character_switch')
                    if char_detector and char_detector.is_in_selection_mode():
                        print(f"[VoiceHandler DEBUG] 選択モード中の認識結果: '{text}' (結果あり: {text is not None and len(text) > 0})")
                    
                    if text:
                        print(f"認識結果: {text}")
                        
                        # Call transcription callback immediately for UI display
                        if self.transcription_callback:
                            try:
                                await self.transcription_callback(text)
                            except Exception as e:
                                print(f"[VoiceHandler] Transcription callback error: {e}")
                        
                        # Check for hallucination using unified filter
                        if self.recognizer.hallucination_filter.is_hallucination(text, engine=self.recognizer.engine_name):
                            print(f"[幻聴検出] ハルシネーションを検出したため無視します: '{text}'")
                            continue
                        
                        # Check for keywords using universal keyword detection system
                        try:
                            from ..tools.keyword import process_keywords, get_keyword_manager
                            
                            # 選択モードの状態を事前確認（デバッグ用）
                            manager = get_keyword_manager()
                            char_detector = manager.get_detector('character_switch')
                            if char_detector and char_detector.is_in_selection_mode():
                                print(f"[VoiceHandler] キーワード処理前: 選択モード中です (テキスト: '{text}')")
                            
                            keyword_result = process_keywords(text)
                            if keyword_result and keyword_result.detected:
                                # メッセージが辞書形式の場合（キャラクター切り替え）
                                if isinstance(keyword_result.message, dict):
                                    msg_data = keyword_result.message
                                    mode = msg_data.get('mode', '')
                                    
                                    # 選択モードに入る時
                                    if mode == 'selection_mode' and 'goodbye_reply' in msg_data:
                                        # CharacterSwitchDetectorの状態を確認
                                        manager = get_keyword_manager()
                                        char_detector = manager.get_detector('character_switch')
                                        if char_detector:
                                            print(f"[VoiceHandler] 選択モードに入ります。検出器の選択モード状態: {char_detector.is_in_selection_mode()}")
                                        
                                        # goodbyeReplyを音声で読み上げ（ユーザー入力テキストも含めて渡す）
                                        if self.audio_callback:
                                            await self.audio_callback({
                                                'user_text': text,
                                                'assistant_text': msg_data['goodbye_reply'],
                                                'type': 'keyword_response'
                                            }, 'voice_synthesis')
                                        print(f"[キーワード検出] {msg_data['message']}")
                                        continue  # LLM処理をスキップ
                                    
                                    # キャラクター切り替え完了時
                                    elif mode == 'character_switched' and 'greeting' in msg_data:
                                        # CharacterSwitchDetectorの状態を確認
                                        manager = get_keyword_manager()
                                        char_detector = manager.get_detector('character_switch')
                                        if char_detector:
                                            print(f"[VoiceHandler] キャラクター切り替え完了。検出器の選択モード状態: {char_detector.is_in_selection_mode()}")
                                        
                                        print(f"[キーワード検出] {msg_data['message']}")
                                        # greetingを音声で読み上げ（ユーザー入力テキストも含めて渡す）
                                        if self.audio_callback:
                                            await self.audio_callback({
                                                'user_text': text,
                                                'assistant_text': msg_data['greeting'],
                                                'type': 'keyword_response'
                                            }, 'voice_synthesis')
                                        continue  # LLM処理をスキップ
                                    
                                    else:
                                        print(f"[キーワード検出] {msg_data.get('message', '')}")
                                        # 選択モード以外の辞書形式メッセージもLLM処理をスキップ
                                        if keyword_result.bypass_llm:
                                            continue
                                
                                # 通常のメッセージの場合
                                elif keyword_result.message:
                                    print(f"[キーワード検出] {keyword_result.message}")
                                
                                # Skip normal processing if keyword was handled and LLM bypass is requested
                                if keyword_result.bypass_llm:
                                    continue
                        except Exception as e:
                            print(f"[キーワード検出] エラー: {e}")
                            # エラーが発生してもキーワードが検出されていたらスキップ
                            if keyword_result and keyword_result.detected and keyword_result.bypass_llm:
                                continue
                        
                        # Call audio callback if set
                        if self.audio_callback:
                            print(f"[VoiceHandler] audio_callback呼び出し: text='{text}', type={audio_type}")
                            await self.audio_callback(text, audio_type)
                        else:
                            print("[VoiceHandler] audio_callbackが設定されていません")
                    else:
                        print("音声認識に失敗しました")
                        
            except queue.Empty:
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"\n処理エラー: {type(e).__name__}: {e}")
                await asyncio.sleep(0.1)
    
    def _apply_acoustic_echo_cancellation(self, audio_data: np.ndarray) -> np.ndarray:
        """Apply acoustic echo cancellation to microphone input"""
        if not self.aec_enabled or len(self.playback_audio_buffer) == 0:
            return audio_data
            
        try:
            # Convert input audio to float for processing
            input_audio = audio_data.astype(np.float32)
            
            # Get recent playback audio for comparison
            delay_samples = int(0.05 * 44100)  # Assume ~50ms delay (speaker to mic)
            
            # Get playback reference signal
            if len(self.playback_audio_buffer) >= len(input_audio) + delay_samples:
                playback_end = len(self.playback_audio_buffer) - delay_samples
                playback_start = playback_end - len(input_audio)
                
                if playback_start >= 0:
                    playback_ref = np.array(list(self.playback_audio_buffer))[playback_start:playback_end]
                    
                    # Calculate normalized cross-correlation
                    correlation = self._calculate_normalized_correlation(input_audio, playback_ref)
                    
                    if correlation > self.echo_correlation_threshold:
                        # High correlation detected - likely echo
                        print(f"[AEC] エコー検出 (相関: {correlation:.3f}) - 90%減衰適用")
                        
                        # Apply adaptive filtering to remove echo
                        echo_cancelled = input_audio * self.echo_attenuation_factor
                        
                        # Preserve some of the original signal to avoid over-suppression
                        echo_cancelled += input_audio * (1 - self.echo_attenuation_factor) * (1 - correlation)
                        
                        return echo_cancelled.astype(np.int16)
                    else:
                        print(f"[AEC] 低相関 ({correlation:.3f}) - エコーなし")
            
            return audio_data
            
        except Exception as e:
            print(f"[AEC] エラー: {e}")
            return audio_data
    
    def _calculate_normalized_correlation(self, signal1: np.ndarray, signal2: np.ndarray) -> float:
        """Calculate normalized cross-correlation between two signals"""
        try:
            if len(signal1) != len(signal2):
                min_len = min(len(signal1), len(signal2))
                signal1 = signal1[:min_len]
                signal2 = signal2[:min_len]
            
            if len(signal1) == 0:
                return 0.0
                
            # Calculate normalized correlation coefficient
            correlation = np.corrcoef(signal1, signal2)[0, 1]
            
            # Handle NaN (can occur with silent audio)
            if np.isnan(correlation):
                return 0.0
                
            # Return absolute correlation (we care about similarity, not phase)
            return abs(correlation)
            
        except Exception:
            return 0.0
    
    def _store_playback_audio(self, audio_data: bytes):
        """Store playback audio for echo cancellation reference"""
        if not self.aec_enabled:
            return
            
        try:
            # Convert to numpy array and store
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            
            # Add to circular buffer
            self.playback_audio_buffer.extend(audio_array)
            
            # Store timestamp
            current_time = time.time()
            chunk_timestamps = [current_time] * (len(audio_array) // 2048 + 1)
            self.playback_timestamps.extend(chunk_timestamps)
            
        except Exception as e:
            print(f"[AEC] 再生音声保存エラー: {e}")