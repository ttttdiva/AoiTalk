"""
Qwen3-TTS Voice Cloning engine implementation

This engine provides voice cloning capabilities using Qwen3-TTS models.
It can clone voices from 3-10 second audio samples and generate speech
with the cloned voice.
"""
import asyncio
import os
import json
import pickle
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
from datetime import datetime
import torch
import soundfile as sf
import numpy as np

try:
    from qwen_tts import Qwen3TTSModel
    QWEN_TTS_AVAILABLE = True
except ImportError:
    Qwen3TTSModel = None
    QWEN_TTS_AVAILABLE = False

try:
    from src.utils.audio_transcription import AudioTranscriber
    TRANSCRIPTION_AVAILABLE = True
except ImportError:
    AudioTranscriber = None
    TRANSCRIPTION_AVAILABLE = False


class Qwen3TTSEngine:
    """Qwen3-TTS Voice Cloning engine
    
    Features:
    - Clone voices from short audio samples (3-10 seconds)
    - Save and reuse voice embeddings
    - Generate speech with cloned voices
    - Support for multiple languages
    - Auto-scan config/cloning/ directory for voice files
    """
    
    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        cache_dir: Optional[str] = None,
        voices_dir: Optional[str] = None,
        cloning_dir: Optional[str] = None,
        use_gpu: bool = True,
        dtype: str = "bfloat16",
        config: Optional[Any] = None,
    ):
        """Initialize Qwen3-TTS engine
        
        Args:
            model_name: HuggingFace model name or local path
            cache_dir: Directory to cache the model
            voices_dir: Directory to store voice embeddings
            cloning_dir: Directory to scan for voice files (default: config/cloning)
            use_gpu: Whether to use GPU acceleration
            dtype: Model dtype (bfloat16, float16, float32)
            config: Config object for accessing existing Gemini API settings
        """
        if not QWEN_TTS_AVAILABLE:
            raise ImportError(
                "qwen-tts library is not installed. "
                "Install it with: pip install qwen-tts"
            )
        
        self.model_name = model_name
        self.cache_dir = cache_dir or "cache/qwen3_models"
        self.voices_dir = voices_dir or "cache/qwen3_voices"
        
        # Resolve cloning_dir to absolute path based on repository root
        if cloning_dir:
            self.cloning_dir = Path(cloning_dir)
        else:
            # Get repository root (where this file is located: src/tts/engines/)
            repo_root = Path(__file__).resolve().parent.parent.parent.parent
            self.cloning_dir = repo_root / "config" / "cloning"
        
        self.use_gpu = use_gpu and torch.cuda.is_available()
        self.dtype = dtype
        self.config = config  # Store Config object for accessing existing Gemini API settings
        
        # Create directories
        os.makedirs(self.voices_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # Model and state
        self.model: Optional[Qwen3TTSModel] = None
        self.voices: Dict[str, Dict[str, Any]] = {}
        self.current_voice: Optional[str] = None
        self.current_voice_prompt: Optional[Any] = None
        
        # Sample rate (Qwen3-TTS outputs at 12000 Hz, but we'll convert to 24000 for compatibility)
        self.target_sample_rate = 24000
        
    async def initialize(self) -> bool:
        """Initialize the Qwen3-TTS model
        
        Returns:
            True if initialization successful
        """
        try:
            print(f"[Qwen3-TTS] Loading model: {self.model_name}")
            
            # Determine device
            device = "cuda:0" if self.use_gpu else "cpu"
            
            # Determine dtype
            if self.dtype == "bfloat16":
                dtype = torch.bfloat16
            elif self.dtype == "float16":
                dtype = torch.float16
            else:
                dtype = torch.float32
            
            # Load model
            model_kwargs = {
                "device_map": device,
                "dtype": dtype,
            }
            
            # Use flash attention if available and on GPU
            use_flash_attn = False
            if self.use_gpu:
                model_kwargs["attn_implementation"] = "flash_attention_2"
                use_flash_attn = True
            
            # Try to load model with FlashAttention2 if enabled, fallback if not available
            try:
                self.model = Qwen3TTSModel.from_pretrained(
                    self.model_name,
                    **model_kwargs
                )
            except ImportError as e:
                # FlashAttention2 not available, fallback to default attention
                if use_flash_attn and "flash_attn" in str(e):
                    print("[Qwen3-TTS] flash_attn not installed, falling back to default attention")
                    # Remove FlashAttention2 from kwargs
                    model_kwargs.pop("attn_implementation", None)
                    # Retry without FlashAttention2
                    self.model = Qwen3TTSModel.from_pretrained(
                        self.model_name,
                        **model_kwargs
                    )
                else:
                    # Other ImportError, re-raise
                    raise
            
            print(f"[Qwen3-TTS] Model loaded successfully on {device}")
            
            # Load existing voices
            self._load_voices_index()
            
            # Auto-scan and register voices from cloning directory
            await self._auto_scan_cloning_directory()
            
            # Log and select a voice if available
            if self.voices:
                print(f"[Qwen3-TTS] Loaded {len(self.voices)} saved voices: {list(self.voices.keys())}")
                # Set first voice as current if available and no voice is selected
                if not self.current_voice:
                    first_voice_name = list(self.voices.keys())[0]
                    self.set_voice_by_name(first_voice_name)
                    print(f"[Qwen3-TTS] ✓ Auto-selected first available voice: {first_voice_name}")
                else:
                    print(f"[Qwen3-TTS] Current voice already set: {self.current_voice}")
            else:
                print("[Qwen3-TTS] ❌ No voices available. Please add voice files to config/cloning/")
            
            return True
            
        except Exception as e:
            print(f"[Qwen3-TTS] Initialization error: {e}")
            return False
    
    def _load_voices_index(self):
        """Load the voices index from disk"""
        index_path = os.path.join(self.voices_dir, "voices_index.json")
        
        if os.path.exists(index_path):
            try:
                with open(index_path, 'r', encoding='utf-8') as f:
                    self.voices = json.load(f)
            except Exception as e:
                print(f"[Qwen3-TTS] Error loading voices index: {e}")
                self.voices = {}
        else:
            self.voices = {}
    
    def _save_voices_index(self):
        """Save the voices index to disk"""
        index_path = os.path.join(self.voices_dir, "voices_index.json")
        
        try:
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump(self.voices, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Qwen3-TTS] Error saving voices index: {e}")
    
    async def _auto_scan_cloning_directory(self):
        """Auto-scan config/cloning/ directory and register voices
        
        This method scans the cloning directory for audio files and:
        1. Checks if voice is already registered
        2. Looks for corresponding .txt file for transcription
        3. If no .txt, uses Gemini to transcribe
        4. Creates voice embeddings
        """
        # Check if cloning directory exists
        if not self.cloning_dir.exists():
            return
        
        print(f"[Qwen3-TTS] Scanning for voices in {self.cloning_dir.name}")
        
        # Supported audio extensions
        audio_extensions = {'.wav', '.mp3', '.flac', '.ogg', '.m4a'}
        
        # Find all audio files
        audio_files = []
        for ext in audio_extensions:
            audio_files.extend(self.cloning_dir.glob(f"*{ext}"))
        
        if not audio_files:
            print(f"[Qwen3-TTS] ❌ No audio files found in {self.cloning_dir}")
            return
        
        # Process each audio file
        for audio_path in audio_files:
            voice_name = audio_path.stem  # Filename without extension
            
            # Check if voice is already registered
            if self._is_voice_registered(voice_name):
                continue
            
            # Look for corresponding .txt file
            txt_path = audio_path.with_suffix('.txt')
            ref_text = None
            
            if txt_path.exists():
                # Read existing transcription
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        ref_text = f.read().strip()
                except Exception as e:
                    print(f"[Qwen3-TTS] Error reading {txt_path.name}: {e}")
            
            # If no .txt file, try to transcribe with Gemini
            if not ref_text:
                ref_text = await self._transcribe_audio(audio_path, txt_path)
                
                if not ref_text:
                    print(f"[Qwen3-TTS] Skipping '{voice_name}' (no transcription available)")
                    continue
            
            # Create voice embeddings
            try:
                success = await self.save_voice(
                    audio_path=str(audio_path),
                    voice_name=voice_name,
                    ref_text=ref_text,
                    language="Auto",
                    description=f"Auto-registered from {audio_path.name}",
                    x_vector_only=False,
                )
                
                if success:
                    print(f"[Qwen3-TTS] Registered voice: {voice_name}")
                else:
                    print(f"[Qwen3-TTS] Failed to register voice: {voice_name}")
                    
            except Exception as e:
                print(f"[Qwen3-TTS] Error registering '{voice_name}': {e}")
    
    def _is_voice_registered(self, voice_name: str) -> bool:
        """Check if voice is already registered with valid embeddings
        
        Args:
            voice_name: Name of the voice to check
            
        Returns:
            True if voice is registered and embeddings file exists
        """
        if voice_name not in self.voices:
            return False
        
        # Check if embeddings file exists
        voice_file = self.voices[voice_name].get("file")
        if not voice_file or not os.path.exists(voice_file):
            return False
        
        return True
    
    async def _transcribe_audio(self, audio_path: Path, txt_path: Path) -> Optional[str]:
        """Transcribe audio using Gemini API
        
        Args:
            audio_path: Path to audio file
            txt_path: Path where transcription will be saved
            
        Returns:
            Transcribed text or None on error
        """
        if not TRANSCRIPTION_AVAILABLE:
            return None
        
        try:
            # Initialize transcriber using existing Config (dict or Config object)
            transcriber = AudioTranscriber(config=self.config)
            
            # Transcribe audio
            transcription = transcriber.transcribe_audio(audio_path)
            
            if transcription:
                # Save transcription to .txt file
                try:
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(transcription)
                except Exception as e:
                    print(f"[Qwen3-TTS] Could not save transcription file: {e}")
                
                return transcription
            else:
                return None
                
        except Exception as e:
            print(f"[Qwen3-TTS] Transcription error: {e}")
            return None
    
    async def save_voice(
        self,
        audio_path: str,
        voice_name: str,
        ref_text: str,
        language: str = "Auto",
        description: str = "",
        x_vector_only: bool = False,
    ) -> bool:
        """Save a voice embedding from an audio sample
        
        Args:
            audio_path: Path to the reference audio file
            voice_name: Name for this voice
            ref_text: Transcript of the reference audio
            language: Language of the audio (Auto, Chinese, English, etc.)
            description: Optional description of the voice
            x_vector_only: If True, only use speaker embedding (lower quality but faster)
            
        Returns:
            True if voice was saved successfully
        """
        if not self.model:
            print("[Qwen3-TTS] Model not initialized")
            return False
        
        try:
            print(f"[Qwen3-TTS] Creating voice embedding for: {voice_name}")
            
            # Create voice clone prompt (this extracts and saves the embeddings)
            prompt_items = self.model.create_voice_clone_prompt(
                ref_audio=audio_path,
                ref_text=ref_text if not x_vector_only else None,
                x_vector_only_mode=x_vector_only,
            )
            
            # Save prompt items to disk
            voice_file = os.path.join(self.voices_dir, f"{voice_name}.pkl")
            with open(voice_file, 'wb') as f:
                pickle.dump(prompt_items, f)
            
            # Update voices index
            self.voices[voice_name] = {
                "name": voice_name,
                "file": voice_file,
                "language": language,
                "description": description,
                "ref_text": ref_text,
                "x_vector_only": x_vector_only,
                "created_at": datetime.now().isoformat(),
            }
            
            self._save_voices_index()
            
            print(f"[Qwen3-TTS] Voice '{voice_name}' saved successfully")
            return True
            
        except Exception as e:
            print(f"[Qwen3-TTS] Error saving voice: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_voices(self) -> List[Dict[str, Any]]:
        """Get list of available voices
        
        Returns:
            List of voice metadata dictionaries
        """
        return list(self.voices.values())
    
    def set_voice_by_name(self, voice_name: str) -> bool:
        """Set current voice by name
        
        Args:
            voice_name: Name of the voice to use
            
        Returns:
            True if voice was set successfully
        """
        if voice_name not in self.voices:
            print(f"[Qwen3-TTS] Voice '{voice_name}' not found")
            return False
        
        try:
            # Load voice prompt from file
            voice_file = self.voices[voice_name]["file"]
            with open(voice_file, 'rb') as f:
                self.current_voice_prompt = pickle.load(f)
            
            self.current_voice = voice_name
            print(f"[Qwen3-TTS] Voice set to: {voice_name}")
            return True
            
        except Exception as e:
            print(f"[Qwen3-TTS] Error loading voice: {e}")
            return False
    
    async def synthesize(
        self,
        text: str,
        voice_name: Optional[str] = None,
        language: str = "Auto",
        character_name: Optional[str] = None,
        **kwargs
    ) -> Optional[bytes]:
        """Synthesize speech from text using the current or specified voice
        
        Args:
            text: Text to synthesize
            voice_name: Optional voice name to use (overrides current voice)
            language: Language for synthesis
            character_name: Optional character name (used to auto-select voice if voice_name not specified)
            **kwargs: Additional generation parameters
            
        Returns:
            WAV audio data as bytes or None on error
        """
        if not self.model:
            print("[Qwen3-TTS] Model not initialized")
            return None
        
        # Handle voice selection
        # Priority: voice_name > character_name > current_voice
        selected_voice = voice_name
        
        if not selected_voice and character_name:
            # Try to find voice matching character name
            if character_name in self.voices:
                selected_voice = character_name
        
        if selected_voice and selected_voice != self.current_voice:
            if not self.set_voice_by_name(selected_voice):
                return None
        
        if not self.current_voice_prompt:
            print("[Qwen3-TTS] No voice selected")
            if self.voices:
                print(f"[Qwen3-TTS] Available voices: {list(self.voices.keys())}")
                print(f"[Qwen3-TTS] Hint: Add 'voice_name: <name>' to character YAML or ensure voice file matches character name")
            else:
                print("[Qwen3-TTS] No voices registered. Add audio files to config/cloning/")
            return None
        
        try:
            # Generation parameters
            gen_kwargs = {
                "max_new_tokens": kwargs.get("max_new_tokens", 2048),
                "do_sample": kwargs.get("do_sample", True),
                "top_k": kwargs.get("top_k", 50),
                "top_p": kwargs.get("top_p", 1.0),
                "temperature": kwargs.get("temperature", 0.9),
                "repetition_penalty": kwargs.get("repetition_penalty", 1.05),
            }
            
            # Generate speech
            wavs, sr = self.model.generate_voice_clone(
                text=text,
                language=language,
                voice_clone_prompt=self.current_voice_prompt,
                **gen_kwargs
            )
            
            # Convert to numpy array if needed
            if isinstance(wavs, list):
                wav_data = wavs[0]
            else:
                wav_data = wavs
            
            # Resample to target sample rate if needed
            if sr != self.target_sample_rate:
                try:
                    import librosa
                    wav_data = librosa.resample(
                        wav_data,
                        orig_sr=sr,
                        target_sr=self.target_sample_rate
                    )
                    sr = self.target_sample_rate
                except ImportError:
                    print("[Qwen3-TTS] librosa not available, using original sample rate")
            
            # Convert to WAV bytes
            import io
            wav_buffer = io.BytesIO()
            sf.write(wav_buffer, wav_data, sr, format='WAV', subtype='PCM_16')
            wav_buffer.seek(0)
            wav_bytes = wav_buffer.read()
            
            print(f"[Qwen3-TTS] Generated {len(wav_bytes)} bytes of audio at {sr}Hz")
            return wav_bytes
            
        except Exception as e:
            print(f"[Qwen3-TTS] Synthesis error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def cleanup(self):
        """Cleanup resources"""
        try:
            if self.model:
                # Clean up CUDA cache if using GPU
                if self.use_gpu and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                
                self.model = None
                print("[Qwen3-TTS] Cleanup complete")
        except Exception as e:
            print(f"[Qwen3-TTS] Cleanup error: {e}")
