"""
Google Gemini speech recognition implementation
"""
import io
import time
import wave
from typing import Optional, Generator, Tuple, Dict, Any
from collections import deque

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("[GeminiRecognizer] google-generativeai not available")

from ..base import SpeechRecognizerInterface


class GeminiSpeechRecognizer(SpeechRecognizerInterface):
    """Google Gemini speech recognition implementation"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize Gemini speech recognizer
        
        Args:
            config: Configuration dictionary
        """
        self.config = config or {}
        self.api_key = self.config.get('api_key')
        self.model_name = self.config.get('model', 'gemini-3-flash-preview')
        self.language = self.config.get('language', 'ja')
        self.chunk_length = self.config.get('chunk_length', 3.0)
        self.sample_rate = 16000
        
        # Initialize Gemini
        self.model = None
        if GEMINI_AVAILABLE and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
                print(f"[GeminiRecognizer] Initialized with model '{self.model_name}'")
            except Exception as e:
                print(f"[GeminiRecognizer] Failed to initialize: {type(e).__name__}: {e}")
        else:
            if not GEMINI_AVAILABLE:
                print("[GeminiRecognizer] google-generativeai is not installed")
            if not self.api_key:
                print("[GeminiRecognizer] API key not provided")
                
        # Buffer for streaming mode
        self.audio_buffer = deque()
        self.buffer_duration = 0.0
        
    def configure(self, config: Dict[str, Any]) -> None:
        """Configure the recognition engine
        
        Args:
            config: Configuration dictionary
        """
        self.config.update(config)
        
        # Update API key if changed
        new_api_key = self.config.get('api_key')
        if new_api_key != self.api_key:
            self.api_key = new_api_key
            if GEMINI_AVAILABLE and self.api_key:
                try:
                    genai.configure(api_key=self.api_key)
                    print("[GeminiRecognizer] Updated API key")
                except Exception as e:
                    print(f"[GeminiRecognizer] Failed to update API key: {e}")
        
        # Update other configuration
        self.model_name = self.config.get('model', self.model_name)
        self.language = self.config.get('language', self.language)
        self.chunk_length = self.config.get('chunk_length', self.chunk_length)
        
    def get_engine_info(self) -> Dict[str, Any]:
        """Get information about the recognition engine
        
        Returns:
            Dictionary with engine information
        """
        return {
            'engine': 'gemini',
            'model': self.model_name,
            'language': self.language,
            'chunk_length': self.chunk_length,
            'gemini_available': GEMINI_AVAILABLE,
            'api_key_configured': bool(self.api_key),
            'model_initialized': self.model is not None
        }
        
    def start_stream(self):
        """Start a new streaming session"""
        self.audio_buffer.clear()
        self.buffer_duration = 0.0
        print("[GeminiRecognizer] Started new streaming session")
        
    def process_audio_chunk(self, 
                           audio_data: bytes,
                           sample_rate: int = 16000,
                           channels: int = 1,
                           sample_width: int = 2) -> Generator[Tuple[bool, Optional[str]], None, None]:
        """Process audio chunk and yield transcription results
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            
        Yields:
            Tuple of (is_final, text) where is_final indicates if the segment is complete
        """
        if not self.model:
            return
            
        # Add to buffer
        self.audio_buffer.append(audio_data)
        chunk_duration = len(audio_data) / (sample_rate * channels * sample_width)
        self.buffer_duration += chunk_duration
        
        # Process if we have enough audio
        if self.buffer_duration >= self.chunk_length:
            # Combine all chunks in buffer
            combined_audio = b''.join(self.audio_buffer)
            
            # Clear buffer
            self.audio_buffer.clear()
            self.buffer_duration = 0.0
            
            # Transcribe the chunk
            result = self.recognize(combined_audio, sample_rate, channels, sample_width)
            
            if result:
                yield (True, result)
                
    def finish_stream(self) -> Optional[str]:
        """Finish streaming and process remaining audio
        
        Returns:
            Final transcription text or None
        """
        if not self.model or len(self.audio_buffer) == 0:
            return None
            
        # Process remaining audio in buffer
        combined_audio = b''.join(self.audio_buffer)
        
        # Clear buffer
        self.audio_buffer.clear()
        self.buffer_duration = 0.0
        
        return self.recognize(combined_audio)
    
    def recognize(self, 
                  audio_data: bytes, 
                  sample_rate: int = 16000,
                  channels: int = 1,
                  sample_width: int = 2,
                  language: str = None,
                  prompt: Optional[str] = None) -> Optional[str]:
        """Recognize speech from audio data
        
        Args:
            audio_data: Raw audio data bytes
            sample_rate: Sample rate of audio
            channels: Number of audio channels
            sample_width: Sample width in bytes
            language: Language code for recognition
            prompt: Optional prompt to guide recognition
            
        Returns:
            Recognized text or None if failed
        """
        if not self.model:
            print("[GeminiRecognizer] Gemini model not initialized")
            return None
            
        try:
            # Create WAV file from audio data
            wav_data = self._create_wav_data(audio_data, sample_rate, channels, sample_width)
            
            # Create a file-like object
            audio_file = io.BytesIO(wav_data)
            audio_file.name = "audio.wav"
            
            # Prepare the prompt with strict instructions to reduce hallucinations
            lang = language or self.language
            base_prompt = f"""Please transcribe this audio to text in {lang}. 

IMPORTANT INSTRUCTIONS:
- Only transcribe actual speech that you can clearly hear
- Do NOT add any sound effects, beeping sounds, or noise descriptions
- Do NOT transcribe repetitive patterns like "ピッピッピッ" or "beep beep" 
- If the audio contains only noise or unclear sounds, respond with "[UNCLEAR]"
- If there is some speech followed by noise, only transcribe the clear speech part
- Focus on real human conversation, ignore background noise"""
            
            if prompt:
                base_prompt += f" Context: {prompt}"
            
            # Upload and transcribe
            uploaded_file = genai.upload_file(audio_file, mime_type="audio/wav")
            
            try:
                response = self.model.generate_content([
                    base_prompt,
                    uploaded_file
                ])
                
                if response.text:
                    result = response.text.strip()
                    print(f"[GeminiRecognizer] Transcription result: '{result}'")
                    
                    # Handle [UNCLEAR] responses
                    if "[UNCLEAR]" in result:
                        print(f"[GeminiRecognizer] Geminiが不明瞭と判定: '{result}'")
                        return None
                    
                    # Apply Gemini-specific hallucination filtering and cleaning
                    cleaned_result = self._clean_gemini_hallucinations(result)
                    if not cleaned_result:
                        print(f"[GeminiRecognizer] Gemini幻聴を検出してフィルタ: '{result}'")
                        return None
                    
                    if cleaned_result != result:
                        print(f"[GeminiRecognizer] 幻聴部分を除去: '{result}' → '{cleaned_result}'")
                    
                    return cleaned_result
                else:
                    print("[GeminiRecognizer] No transcription result")
                    return None
                    
            finally:
                # Clean up uploaded file
                try:
                    uploaded_file.delete()
                except:
                    pass
                    
        except Exception as e:
            print(f"[GeminiRecognizer] Recognition error: {type(e).__name__}: {e}")
            return None
            
    def _create_wav_data(self, 
                        audio_data: bytes, 
                        sample_rate: int, 
                        channels: int, 
                        sample_width: int) -> bytes:
        """Create WAV format data from raw audio
        
        Args:
            audio_data: Raw audio data
            sample_rate: Sample rate
            channels: Number of channels
            sample_width: Sample width in bytes
            
        Returns:
            WAV format audio data
        """
        # Create WAV file in memory
        wav_buffer = io.BytesIO()
        
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)
            
        wav_buffer.seek(0)
        return wav_buffer.read()
    
    def _is_gemini_hallucination(self, text: str) -> bool:
        """Check if text contains Gemini-specific hallucination patterns
        
        Args:
            text: Transcribed text to check
            
        Returns:
            True if likely a Gemini hallucination
        """
        if not text:
            return True
            
        # Remove whitespace and newlines for analysis
        clean_text = ''.join(text.split())
        
        # Check for repetitive "ピッ" patterns (common Gemini hallucination)
        pip_count = clean_text.count('ピッ')
        if pip_count >= 5:  # Require more repetitions (was 3, now 5)
            # Check if it dominates the text
            pip_ratio = (pip_count * 2) / len(clean_text) if len(clean_text) > 0 else 0
            if pip_ratio > 0.5:  # Require higher dominance (was 30%, now 50%)
                return True
                
        # Check for other Gemini-specific patterns
        gemini_hallucination_patterns = [
            'ピッピッピッピッピッピッ',  # 6+ consecutive
            'プップップップップップップ',
            'ブーブーブーブーブーブー',
            'ビープビープビープビープ',
            'ピーピーピーピーピーピー',
        ]
        
        for pattern in gemini_hallucination_patterns:
            if pattern in clean_text:
                return True
                
        # Check for mixed real speech with trailing noise patterns
        lines = text.strip().split('\n')
        if len(lines) >= 2:
            last_line = lines[-1].strip()
            # If last line is pure repetitive noise after real speech
            if len(last_line) >= 6 and ('ピッ' in last_line or 'ピー' in last_line):
                # Check if last line is mostly noise
                noise_chars = last_line.count('ピ') + last_line.count('プ') + last_line.count('ブ')
                if noise_chars / len(last_line) > 0.7:  # 70% noise characters
                    # Remove just the noisy last line, keep the real speech
                    remaining_text = '\n'.join(lines[:-1]).strip()
                    if len(remaining_text) > 10:  # If there's substantial real content
                        # Update the original text reference (this is a limitation)
                        # For now, we'll flag as hallucination to be safe
                        return True
                        
        # Check for pure noise without real content (more lenient)
        real_japanese_chars = sum(1 for c in text if c in 'あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん')
        noise_chars = text.count('ピ') + text.count('プ') + text.count('ブ')
        
        # Only flag if noise clearly dominates and there's substantial noise
        if len(text) > 20 and noise_chars > real_japanese_chars * 2 and noise_chars > 10:
            return True
            
        return False
    
    def _clean_gemini_hallucinations(self, text: str) -> str:
        """Clean Gemini hallucinations while preserving real speech
        
        Args:
            text: Original transcribed text
            
        Returns:
            Cleaned text with hallucinations removed, or empty string if pure hallucination
        """
        if not text:
            return ""
            
        # First check if it's pure hallucination (but be more lenient)
        # Only reject if it's clearly pure noise
        lines = text.strip().split('\n')
        total_chars = len(''.join(lines))
        noise_chars = sum(line.count('ピ') + line.count('プ') + line.count('ブ') for line in lines)
        
        if total_chars > 0 and noise_chars / total_chars > 0.8:  # 80% noise
            return ""
            
        # Try to clean by removing trailing noise patterns
        lines = text.strip().split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Check if this line is mostly noise
            noise_chars = line.count('ピ') + line.count('プ') + line.count('ブ')
            if len(line) > 6 and noise_chars / len(line) > 0.7:
                # Skip this noisy line
                print(f"[GeminiRecognizer] ノイズ行をスキップ: '{line}'")
                continue
                
            # Remove trailing repetitive patterns from the line
            cleaned_line = self._remove_trailing_noise(line)
            if cleaned_line:
                cleaned_lines.append(cleaned_line)
        
        result = '\n'.join(cleaned_lines).strip()
        
        # Final check: if result is too short or still mostly noise, reject
        if len(result) < 3:
            return ""
            
        return result
    
    def _remove_trailing_noise(self, line: str) -> str:
        """Remove trailing noise patterns from a line
        
        Args:
            line: Input line
            
        Returns:
            Line with trailing noise removed
        """
        # Define noise patterns to remove from the end
        noise_patterns = ['ピッ', 'ピー', 'プッ', 'ブー', 'ビープ']
        
        # Keep removing noise patterns from the end
        while True:
            original_line = line
            for pattern in noise_patterns:
                if line.endswith(pattern):
                    line = line[:-len(pattern)].strip()
                    break
                    
            # Also remove if there's repetitive noise at the end
            for pattern in noise_patterns:
                if pattern in line[-10:]:  # Check last 10 characters
                    # Count consecutive patterns at the end
                    count = 0
                    temp_line = line
                    while temp_line.endswith(pattern):
                        temp_line = temp_line[:-len(pattern)]
                        count += 1
                    
                    if count >= 2:  # 2 or more consecutive patterns
                        line = temp_line.strip()
                        break
            
            # If no changes made, break
            if line == original_line:
                break
                
        return line.strip()
