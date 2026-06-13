"""
Discord voice channel handler with voice recognition and TTS support
"""

import asyncio
import logging
import io
import time
import wave
from typing import Dict, Optional, Set
from collections import defaultdict
import numpy as np

import discord
from discord.ext import voice_recv

from ...runtime_features import runtime_feature_manager
from .custom_sink import UserAudioSink
from .speech_detector import SpeechDetector

from ...config import Config
from ...audio.manager import SpeechRecognitionManager as AudioManager
from ...tts.manager import TTSManager

logger = logging.getLogger(__name__)


# Note: We don't need a custom VoiceConnection class anymore
# discord-ext-voice-recv extends the default VoiceClient


class VoiceHandler:
    """Handle Discord voice channel operations"""
    
    def __init__(self, config: Config):
        self.config = config
        self.voice_connections: Dict[int, discord.VoiceClient] = {}  # guild_id -> VoiceClient
        self._connection_locks: Dict[int, asyncio.Lock] = {}
        self.audio_sinks: Dict[int, UserAudioSink] = {}  # guild_id -> active audio sink
        self.user_audio_buffers: Dict[int, Dict[int, bytearray]] = {}  # guild_id -> {user_id: audio_buffer}
        self.user_speaking_states: Dict[int, Dict[int, bool]] = {}  # guild_id -> {user_id: is_speaking}
        self.active_sessions: Dict[int, Set[int]] = {}  # guild_id -> set of user_ids
        self.guild_empty_since: Dict[int, float] = {}  # guild_id -> monotonic timestamp when VC became empty
        self.user_last_speech_time: Dict[int, Dict[int, float]] = {}  # guild_id -> {user_id: timestamp}
        self.user_processing_lock: Dict[int, Dict[int, asyncio.Lock]] = {}  # guild_id -> {user_id: lock}
        self.user_processing_queue: Dict[int, Dict[int, asyncio.Queue]] = {}  # guild_id -> {user_id: queue}
        self.processing_tasks: Dict[int, Dict[int, asyncio.Task]] = {}  # guild_id -> {user_id: task}
        self._main_loop = None  # Store the main event loop
        self._bot_instance = None  # Store bot instance reference
        
        # Initialize audio components lazily. Loading Whisper here delays Discord login.
        speech_config = config.get('speech_recognition', {})
        self._speech_config = speech_config
        self._current_speech_engine = speech_config.get('current_engine', 'whisper')
        self.audio_manager: Optional[AudioManager] = None
        self._audio_manager_lock = asyncio.Lock()
        self.tts_manager = TTSManager(config.config)  # Pass the underlying config dict
        self._tts_initialized = False
        
        # Voice detection parameters
        self.sample_rate = int(config.get('discord.voice.sample_rate', 48000))
        self.channels = int(config.get('discord.voice.channels', 2))
        if self.channels not in (1, 2):
            logger.warning("Invalid discord.voice.channels '%s'; falling back to stereo", self.channels)
            self.channels = 2
        self.auto_disconnect_timeout = int(config.get('discord.voice.auto_disconnect_timeout', 300))
        self.silence_threshold = config.get('speech_recognition.silence_threshold', 15.0)
        self.silence_duration = config.get('speech_recognition.silence_duration', 1.5)
        
        # Initialize speech detector
        self.speech_detector = SpeechDetector(
            sample_rate=self.sample_rate,
            silence_threshold=self.silence_threshold,
            speech_threshold=30.0,
            silence_duration=0.5,  # Use shorter silence duration from detector
            min_speech_duration=0.5,
            max_speech_duration=30.0
        )
        
        # Timeout check task
        self._timeout_check_task = None
        
    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        """Handle voice state updates
        
        Args:
            member: Discord member
            before: Previous voice state
            after: New voice state
        """
        # ユーザーがボイスチャンネルから退出した場合
        if before.channel and not after.channel:
            logger.info(f"User {member.name} left voice channel {before.channel.name}")
            guild_id = before.channel.guild.id
            
            # Clean up speech detector state
            self.speech_detector.cleanup_user(member.id)
            
            # Remove from active sessions
            if guild_id in self.active_sessions:
                self.active_sessions[guild_id].discard(member.id)
                if not self.active_sessions[guild_id]:
                    self.guild_empty_since[guild_id] = time.monotonic()
            
        # ユーザーがボイスチャンネルに参加した場合
        elif not before.channel and after.channel:
            logger.info(f"User {member.name} joined voice channel {after.channel.name}")
            guild_id = after.channel.guild.id
            
            # Add to active sessions
            if guild_id not in self.active_sessions:
                self.active_sessions[guild_id] = set()
            self.active_sessions[guild_id].add(member.id)
            self.guild_empty_since.pop(guild_id, None)
            
        # ユーザーがミュート/アンミュートした場合
        elif before.self_mute != after.self_mute:
            if after.self_mute:
                logger.debug(f"User {member.name} muted themselves")
            else:
                logger.debug(f"User {member.name} unmuted themselves")

    def _get_connection_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._connection_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[guild_id] = lock
        return lock

    def _store_voice_connection(self, channel: discord.VoiceChannel, voice_client: discord.VoiceClient) -> None:
        guild_id = channel.guild.id
        self.voice_connections[guild_id] = voice_client
        self.active_sessions[guild_id] = {
            member.id for member in channel.members if not member.bot
        }
        if self.active_sessions[guild_id]:
            self.guild_empty_since.pop(guild_id, None)
        else:
            self.guild_empty_since[guild_id] = time.monotonic()

    async def connect_voice_channel(self, channel: discord.VoiceChannel) -> Optional[discord.VoiceClient]:
        """Connect to a voice channel with receive support
        
        Args:
            channel: Discord voice channel
            
        Returns:
            VoiceClient or None
        """
        async with self._get_connection_lock(channel.guild.id):
            return await self._connect_voice_channel_locked(channel)

    async def _connect_voice_channel_locked(self, channel: discord.VoiceChannel) -> Optional[discord.VoiceClient]:
        try:
            # Store the main event loop
            self._main_loop = asyncio.get_running_loop()
            logger.info(f"Attempting to connect to voice channel: {channel.name}")

            existing = self.voice_connections.get(channel.guild.id)
            if existing and existing.is_connected():
                if getattr(existing, "channel", None) != channel:
                    logger.info("Moving existing voice connection to channel: %s", channel.name)
                    await existing.move_to(channel)
                self._store_voice_connection(channel, existing)
                if not await self.ensure_listening(channel.guild.id):
                    logger.warning(
                        "Voice connection is active but receive listening did not start for guild %s",
                        channel.guild.id,
                    )
                return existing

            guild_voice_client = channel.guild.voice_client
            if guild_voice_client and guild_voice_client.is_connected():
                if not hasattr(guild_voice_client, "listen"):
                    logger.warning(
                        "Existing guild voice client lacks receive support; reconnecting with VoiceRecvClient"
                    )
                    await guild_voice_client.disconnect(force=True)
                else:
                    if getattr(guild_voice_client, "channel", None) != channel:
                        logger.info("Moving guild voice client to channel: %s", channel.name)
                        await guild_voice_client.move_to(channel)
                    self._store_voice_connection(channel, guild_voice_client)
                    if not await self.ensure_listening(channel.guild.id):
                        logger.warning(
                            "Voice connection is active but receive listening did not start for guild %s",
                            channel.guild.id,
                        )
                    return guild_voice_client

            # Connect to voice channel with voice receive support
            # Reverting self_deaf=True because we need to receive audio
            voice_client = await channel.connect(
                cls=voice_recv.VoiceRecvClient,
                timeout=20.0,
                reconnect=False,
            )
            logger.info(f"Voice client type: {type(voice_client)}")

            # Store connection
            self._store_voice_connection(channel, voice_client)

            # Add a small delay to ensure connection is stable before listening
            await asyncio.sleep(1.0)

            # Check connection status
            if not voice_client.is_connected():
                logger.warning("Voice client reports not connected after connect()")
                # Try waiting a bit more
                await asyncio.sleep(2.0)
                if not voice_client.is_connected():
                    raise Exception("Failed to establish voice connection (is_connected() returned False)")

            # Set up voice receive callback
            if not await self.ensure_listening(channel.guild.id):
                logger.warning(
                    "Voice connection is active but receive listening did not start for guild %s",
                    channel.guild.id,
                )

            logger.info(f"Connected to voice channel: {channel.name} with voice receive support")
            return voice_client

        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {e}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            
            # Ensure we clean up if connection failed
            if channel.guild.id in self.voice_connections:
                del self.voice_connections[channel.guild.id]
            
            # Try to disconnect if voice_client exists locally
            try:
                if 'voice_client' in locals() and voice_client:
                    if voice_client.is_connected():
                        await voice_client.disconnect()
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup: {cleanup_error}")
                
            return None
    
    async def disconnect_voice_channel(self, guild_id: int):
        """Disconnect from voice channel
        
        Args:
            guild_id: Discord guild ID
        """
        if guild_id in self.voice_connections:
            try:
                voice_client = self.voice_connections[guild_id]
                # Stop listening before disconnect
                self.stop_listening(guild_id)
                await voice_client.disconnect()
                del self.voice_connections[guild_id]
                
                # Clean up buffers
                if guild_id in self.user_audio_buffers:
                    del self.user_audio_buffers[guild_id]
                if guild_id in self.active_sessions:
                    del self.active_sessions[guild_id]
                self.guild_empty_since.pop(guild_id, None)
                self.audio_sinks.pop(guild_id, None)
                    
                logger.info(f"Disconnected from voice channel in guild {guild_id}")
            except Exception as e:
                logger.error(f"Error disconnecting from voice channel: {e}")

    def is_listening(self, guild_id: int) -> bool:
        """Return whether the Discord voice client is currently receiving audio."""
        voice_client = self.voice_connections.get(guild_id)
        if not voice_client or not voice_client.is_connected():
            return False

        if hasattr(voice_client, 'is_listening'):
            try:
                return bool(voice_client.is_listening())
            except Exception as exc:
                logger.warning("Failed to query Discord listening state: %s", exc)

        return guild_id in self.audio_sinks

    async def ensure_listening(self, guild_id: int) -> bool:
        """Ensure voice receive is active for an existing voice connection."""
        voice_client = self.voice_connections.get(guild_id)
        if not voice_client or not voice_client.is_connected():
            logger.warning("Cannot start listening: no active voice connection for guild %s", guild_id)
            return False

        if self.is_listening(guild_id):
            return True

        if not hasattr(voice_client, 'listen'):
            logger.error("Voice client does not have a listen method. Type=%s", type(voice_client))
            return False

        try:
            audio_sink = UserAudioSink(self)
            voice_client.listen(audio_sink)
            self.audio_sinks[guild_id] = audio_sink
            logger.info("Discord voice receive listening started for guild %s", guild_id)
            return True
        except Exception as exc:
            logger.error("Failed to start Discord voice receive listening: %s", exc, exc_info=True)
            self.audio_sinks.pop(guild_id, None)
            return False

    def stop_listening(self, guild_id: int) -> bool:
        """Stop voice receive for a guild without disconnecting the bot."""
        voice_client = self.voice_connections.get(guild_id)
        if not voice_client or not voice_client.is_connected():
            self.audio_sinks.pop(guild_id, None)
            return False

        stopped = False
        if hasattr(voice_client, 'stop_listening'):
            try:
                if not hasattr(voice_client, 'is_listening') or voice_client.is_listening():
                    voice_client.stop_listening()
                    stopped = True
            except Exception as exc:
                logger.warning("Failed to stop Discord voice receive listening: %s", exc)
        elif guild_id in self.audio_sinks:
            logger.warning("Voice client cannot stop listening explicitly. Type=%s", type(voice_client))

        self.audio_sinks.pop(guild_id, None)
        return stopped

    def get_connection_status(self, guild_id: int) -> dict:
        """Return a compact voice/TTS status snapshot for Discord commands."""
        voice_client = self.voice_connections.get(guild_id)
        connected = bool(voice_client and voice_client.is_connected())
        channel_name = None
        if connected and getattr(voice_client, 'channel', None):
            channel_name = voice_client.channel.name

        return {
            "connected": connected,
            "channel_name": channel_name,
            "listening": self.is_listening(guild_id),
            "active_users": len(self.active_sessions.get(guild_id, set())),
            "tts_engine": self.tts_manager.current_engine,
            "available_tts_engines": list(self.tts_manager.engines.keys()),
        }
    
    def _on_voice_receive(self, data: bytes, user):
        """Callback for voice data reception
        
        Args:
            data: Raw PCM audio data
            user: Discord user who sent the audio
        """
        logger.debug(f"_on_voice_receive called for user: {user.name if user else 'None'}, data size: {len(data)} bytes")
        
        if not user or user.bot:
            return
            
        # Get guild_id from member object
        guild_id = None
        if hasattr(user, 'guild') and user.guild:
            guild_id = user.guild.id
        else:
            # Try to find from voice connections
            for gid, vc in self.voice_connections.items():
                if vc.is_connected():
                    # Get member from guild
                    try:
                        guild = vc.guild
                        member = guild.get_member(user.id)
                        if member:
                            guild_id = gid
                            break
                    except:
                        pass
                        
        if not guild_id:
            logger.error(f"Cannot determine guild_id for user {user.name}")
            return
            
        # Use speech detector to process audio
        complete_speech = self.speech_detector.process_audio(user.id, data)
        
        if complete_speech:
            logger.info(f"🎯 Complete speech detected for user {user.id}, size: {len(complete_speech)} bytes")
            
            # Initialize queue for user if needed
            if guild_id not in self.user_processing_queue:
                self.user_processing_queue[guild_id] = {}
            if user.id not in self.user_processing_queue[guild_id]:
                self.user_processing_queue[guild_id][user.id] = asyncio.Queue()
            
            # Add to processing queue
            if self._main_loop:
                asyncio.run_coroutine_threadsafe(
                    self.user_processing_queue[guild_id][user.id].put((guild_id, user.id, complete_speech)),
                    self._main_loop
                )
                
                # Start processing task if not already running
                if guild_id not in self.processing_tasks:
                    self.processing_tasks[guild_id] = {}
                if user.id not in self.processing_tasks[guild_id] or self.processing_tasks[guild_id][user.id].done():
                    task = asyncio.run_coroutine_threadsafe(
                        self._process_user_queue(guild_id, user.id),
                        self._main_loop
                    )
                    self.processing_tasks[guild_id][user.id] = task
            else:
                logger.error("Main event loop not available for async processing")
    
    async def _process_user_speech_data(self, guild_id: int, user_id: int, audio_data: bytes):
        """Process speech data from a user
        
        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            audio_data: Audio data bytes
        """
        # Initialize locks for guild if needed
        if guild_id not in self.user_processing_lock:
            self.user_processing_lock[guild_id] = {}
        if user_id not in self.user_processing_lock[guild_id]:
            self.user_processing_lock[guild_id][user_id] = asyncio.Lock()
        
        # Acquire lock to ensure sequential processing
        async with self.user_processing_lock[guild_id][user_id]:
            logger.info(f"🔒 Acquired processing lock for user {user_id}")
            try:
                if len(audio_data) == 0:
                    return
                
                # Convert to numpy array
                audio_array = np.frombuffer(audio_data, dtype=np.int16)
                
                # Check for silence
                rms = np.sqrt(np.mean(audio_array.astype(float) ** 2))
                
                if rms < self.silence_threshold:
                    return
                
                # Convert to mono if stereo
                if self.channels == 2:
                    audio_array = audio_array.reshape(-1, 2).mean(axis=1).astype(np.int16)
                
                # Resample from 48kHz to 16kHz for better Whisper recognition
                from scipy import signal
                target_rate = 16000
                audio_array = signal.resample(audio_array, int(len(audio_array) * target_rate / self.sample_rate)).astype(np.int16)
                
                # Create WAV file in memory
                wav_buffer = io.BytesIO()
                with wave.open(wav_buffer, 'wb') as wav_file:
                    wav_file.setnchannels(1)  # Mono
                    wav_file.setsampwidth(2)  # 16-bit
                    wav_file.setframerate(target_rate)  # Use 16kHz for Whisper
                    wav_file.writeframes(audio_array.tobytes())
                
                wav_buffer.seek(0)
                
                # デバッグ用: WAVファイルを保存（デバッグモード時のみ）
                import os
                if os.getenv('AOITALK_DEBUG', '').lower() in ('true', '1', 'yes'):
                    from datetime import datetime
                    debug_dir = "debug_audio"
                    if not os.path.exists(debug_dir):
                        os.makedirs(debug_dir)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    debug_path = f"{debug_dir}/user_{user_id}_{timestamp}.wav"
                    with open(debug_path, 'wb') as f:
                        f.write(wav_buffer.getvalue())
                    logger.info(f"Debug: Saved audio to {debug_path} (length: {len(audio_array)/target_rate:.2f}s)")
                
                # Perform speech recognition
                text = await self._recognize_speech(wav_buffer.read())
                
                if text:
                    logger.info(f"🗣️ User {user_id} said: '{text}'")
                    
                    # Double-check for common hallucinations
                    hallucination_phrases = [
                        "ご視聴ありがとうございました",
                        "お疲れ様でした", 
                        "ありがとうございました",
                        "また見てね",
                        "チャンネル登録"
                    ]
                    
                    if any(phrase in text for phrase in hallucination_phrases):
                        logger.warning(f"Detected hallucination, ignoring: {text}")
                        return
                    
                    # Use stored bot instance
                    if self._bot_instance:
                        # Get session
                        session = await self._bot_instance.session_handler.get_or_create_session(
                            guild_id=guild_id,
                            user_id=user_id
                        )
                        
                        # Ensure session is in voice mode and has assistant
                        if session.mode != 'voice':
                            session.mode = 'voice'
                            logger.info(f"Changed session mode to 'voice' for user {user_id}")
                        
                        # Initialize assistant if needed
                        if session.assistant is None:
                            from ..modes.discord_mode import DiscordMode
                            session.assistant = DiscordMode(
                                config=self.config,
                                character=session.character or self.config.get('default_character', 'ずんだもん')
                            )
                            logger.info(f"Initialized assistant for user {user_id}")
                        
                        # Process voice command
                        await self._process_voice_command(self._bot_instance, session, text, guild_id, user_id)
            
            except Exception as e:
                logger.error(f"Error processing user speech: {e}", exc_info=True)
            finally:
                logger.info(f"🔓 Released processing lock for user {user_id}")
    
    async def _recognize_speech(self, audio_data: bytes) -> Optional[str]:
        """Recognize speech from audio data
        
        Args:
            audio_data: WAV audio data
            
        Returns:
            Recognized text or None
        """
        try:
            # Extract raw PCM data from WAV
            wav_buffer = io.BytesIO(audio_data)
            with wave.open(wav_buffer, 'rb') as wav_file:
                # Get audio parameters
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                framerate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())
            
            audio_manager = await self._ensure_audio_manager()

            # Use existing speech recognition system
            result = await asyncio.to_thread(
                audio_manager.recognize,
                frames,  # raw PCM data
                sample_rate=framerate,
                channels=channels,
                sample_width=sample_width
            )
            
            if result:
                # Apply hallucination filter
                if audio_manager.hallucination_filter.is_hallucination(result):
                    logger.debug(f"Filtered hallucination: {result}")
                    return None
                return result

        except Exception as e:
            logger.error(f"Speech recognition error: {e}")

        return None

    async def _ensure_audio_manager(self) -> AudioManager:
        """Initialize the ASR manager on first real audio input."""
        if self.audio_manager is not None:
            return self.audio_manager

        async with self._audio_manager_lock:
            if self.audio_manager is None:
                logger.info("Initializing Discord ASR engine: %s", self._current_speech_engine)
                self.audio_manager = await asyncio.to_thread(
                    AudioManager,
                    self._current_speech_engine,
                    self._speech_config,
                )
        return self.audio_manager
    
    async def _process_voice_command(self, bot, session, text: str, guild_id: int, user_id: int):
        """Process voice command and generate response
        
        Args:
            bot: Discord bot instance
            session: User session
            text: Recognized text
            guild_id: Guild ID
            user_id: User ID
        """
        try:
            # Generate response
            if session.assistant:
                if not getattr(session, 'memory_prefilled', False):
                    try:
                        await session.assistant.prefill_context_from_memory(
                            user_id=user_id,
                            guild_id=guild_id
                        )
                    finally:
                        session.memory_prefilled = True

                response = await session.assistant.process_text(
                    text,
                    user_id=user_id,
                    guild_id=guild_id
                )
                
                if response:
                    # Send text response to the text channel
                    if bot and guild_id in self.voice_connections:
                        voice_client = self.voice_connections[guild_id]
                        if voice_client.channel:
                            # Find the text channel associated with the voice channel
                            text_channel = None
                            guild = voice_client.guild
                            
                            # Try to find a text channel with the same name or in the same category
                            if voice_client.channel.category:
                                for channel in voice_client.channel.category.text_channels:
                                    text_channel = channel
                                    break
                            
                            # If no category channel, use the first available text channel
                            if not text_channel:
                                for channel in guild.text_channels:
                                    if channel.permissions_for(guild.me).send_messages:
                                        text_channel = channel
                                        break
                            
                            # Send the text response
                            if text_channel:
                                user = guild.get_member(user_id)
                                if user:
                                    # Send response mentioning the user
                                    if len(response) <= 2000:
                                        await text_channel.send(f"{user.mention} {response}")
                                    else:
                                        # Split long responses
                                        chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
                                        for i, chunk in enumerate(chunks):
                                            if i == 0:
                                                await text_channel.send(f"{user.mention} {chunk}")
                                            else:
                                                await text_channel.send(chunk)
                                    logger.info(f"Sent text response to {text_channel.name}")
                            else:
                                logger.warning("No suitable text channel found for response")
                    
                    # Generate and play TTS audio when Discord VC output is enabled.
                    if runtime_feature_manager.feature_enabled("discord_vc_output"):
                        character = session.character or self.config.get('default_character', 'ずんだもん')
                        audio_data = await self._generate_tts(response, character)

                        if audio_data:
                            await self.play_audio(guild_id, audio_data)
                        
        except Exception as e:
            logger.error(f"Error processing voice command: {e}")
    
    async def _generate_tts(self, text: str, character: str) -> Optional[bytes]:
        """Generate TTS audio
        
        Args:
            text: Text to synthesize
            character: Character name
            
        Returns:
            Audio data or None
        """
        try:
            if not self._tts_initialized:
                await self._initialize_tts()

            logger.info(f"Generating TTS for character: {character}")
            logger.info(f"TTS Manager state - Current engine: {self.tts_manager.current_engine}, Available engines: {list(self.tts_manager.engines.keys())}")
            
            # Get character config
            character_config = self.config.get_character_config(character)
            logger.info(f"Character config found: {character_config is not None}")
            
            # Generate TTS
            result = await self.tts_manager.synthesize(
                text,
                character_name=character
            )
            
            if result:
                logger.info(f"TTS generated successfully, size: {len(result)} bytes")
                return result
            else:
                logger.warning("TTS generation returned None")
                
        except Exception as e:
            logger.error(f"TTS generation error: {e}", exc_info=True)
            
        return None
    
    async def play_audio(self, guild_id: int, audio_data: bytes):
        """Play audio in voice channel
        
        Args:
            guild_id: Guild ID
            audio_data: Audio data to play
        """
        try:
            voice_client = self.voice_connections.get(guild_id)
            if not voice_client or not voice_client.is_connected():
                logger.warning("Not connected to voice channel")
                return
            
            # Save audio to temporary file for FFmpeg
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_file:
                tmp_file.write(audio_data)
                tmp_file_path = tmp_file.name
            
            try:
                # Use FFmpeg to convert WAV to Discord-compatible format
                audio_source = discord.FFmpegPCMAudio(
                    tmp_file_path,
                    options='-f wav'  # Specify input format as WAV
                )
                
                # Stop current playback if any
                if voice_client.is_playing():
                    voice_client.stop()
                
                # Play audio
                voice_client.play(
                    audio_source,
                    after=lambda e: logger.error(f"Player error: {e}") if e else None
                )
                
                # Wait for playback to finish
                while voice_client.is_playing():
                    await asyncio.sleep(0.1)
                    
            finally:
                # Clean up temporary file after playback
                import os
                await asyncio.sleep(1)  # Give FFmpeg time to release the file
                if os.path.exists(tmp_file_path):
                    try:
                        os.unlink(tmp_file_path)
                    except:
                        pass  # Ignore cleanup errors
                
        except Exception as e:
            logger.error(f"Error playing audio: {e}")
    
    def is_connected(self, guild_id: int) -> bool:
        """Check if bot is connected to voice channel in guild
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            True if connected
        """
        return guild_id in self.voice_connections and self.voice_connections[guild_id].is_connected()
    
    def get_voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        """Get voice client for guild
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            VoiceClient or None
        """
        return self.voice_connections.get(guild_id)
    
    async def _initialize_tts(self):
        """Initialize TTS engine based on configuration"""
        try:
            # Register character configurations
            characters = self.config.get('characters', {})
            logger.info(f"Registering {len(characters)} character(s)")
            for char_name, char_config in characters.items():
                self.tts_manager.register_character(char_name, char_config)
                logger.info(f"Registered character: {char_name}")

            preferred_engine = self._get_preferred_tts_engine()
            logger.info("Initializing Discord TTS with preferred engine: %s", preferred_engine)
            engine_initialized = await self._initialize_tts_engine(preferred_engine)

            if not engine_initialized and preferred_engine != 'voicevox':
                logger.warning("Preferred TTS engine '%s' failed; trying VOICEVOX fallback", preferred_engine)
                engine_initialized = await self._initialize_tts_engine('voicevox')

            if not engine_initialized:
                logger.warning("Discord TTS was not initialized. Voice recognition still works, but replies will be text-only.")

            self._tts_initialized = True
            logger.info("TTS initialization completed")

        except Exception as e:
            logger.error(f"Error initializing TTS: {e}", exc_info=True)
            self._tts_initialized = True

    def _get_preferred_tts_engine(self) -> str:
        """Resolve the TTS engine Discord voice replies should use."""
        configured = (
            self.config.get('discord.voice.tts_engine')
            or self.config.get('tts.engine')
            or self.config.get('tts.default_engine')
        )
        if configured:
            return str(configured)

        default_character = self.config.get('default_character')
        if default_character:
            character_config = self.config.get_character_config(default_character)
            voice_config = character_config.get('voice', {}) if character_config else {}
            if voice_config.get('engine'):
                return str(voice_config['engine'])

        return 'voicevox'

    async def _initialize_tts_engine(self, engine_name: str) -> bool:
        """Initialize one TTS engine by name."""
        try:
            engine = None
            if engine_name == 'voicevox':
                engine_path = self.config.voicevox_path
                logger.info("VOICEVOX path: %s", engine_path)
                engine = await self.tts_manager.create_voicevox_engine(engine_path)
            elif engine_name == 'aivisspeech':
                engine_path = (
                    self.config.get('aivisspeech_engine_path')
                    or self.config.get('tts_settings.aivisspeech.engine_path')
                    or self.config.get('tts_settings.aivisspeech.path')
                )
                if not engine_path:
                    logger.warning("AivisSpeech engine path is not configured")
                    return False
                engine = await self.tts_manager.create_aivisspeech_engine(engine_path)
            elif engine_name == 'irodori_tts':
                engine = await self.tts_manager.create_irodori_tts_engine()
            elif engine_name == 'voiceroid':
                character_config = self.config.get_character_config(self.config.get('default_character'))
                engine = await self.tts_manager.create_voiceroid_engine(character_config)
            elif engine_name == 'aivoice':
                engine_path = self.config.get('aivoice_engine_path') or self.config.get('tts_settings.aivoice.engine_path')
                engine = await self.tts_manager.create_aivoice_engine(engine_path)
            elif engine_name == 'cevio':
                engine = await self.tts_manager.create_cevio_engine()
            elif engine_name == 'nijivoice':
                engine = await self.tts_manager.create_nijivoice_engine(self.config.get('nijivoice_api_key'))
            else:
                logger.warning("Unsupported Discord TTS engine: %s", engine_name)
                return False

            if not engine:
                logger.warning("Failed to initialize %s TTS engine", engine_name)
                return False

            self.tts_manager.register_engine(engine_name, engine)
            self.tts_manager.set_engine(engine_name)
            logger.info("Initialized %s TTS engine successfully", engine_name)
            return True
        except Exception as exc:
            logger.error("Error initializing %s TTS engine: %s", engine_name, exc, exc_info=True)
            return False
    
    async def _process_user_queue(self, guild_id: int, user_id: int):
        """Process queued speech data for a user"""
        logger.info(f"🏭 Started queue processor for user {user_id}")
        queue = self.user_processing_queue[guild_id][user_id]
        
        while True:
            try:
                # Wait for speech data with timeout
                gid, uid, audio_data = await asyncio.wait_for(queue.get(), timeout=30.0)
                logger.info(f"📦 Processing queued speech for user {uid}")
                await self._process_user_speech_data(gid, uid, audio_data)
            except asyncio.TimeoutError:
                # No new data for 30 seconds, exit task
                logger.info(f"🚪 Queue processor for user {user_id} timed out, exiting")
                break
            except Exception as e:
                logger.error(f"Error in queue processor: {e}", exc_info=True)
    
    async def cleanup(self):
        """Cleanup all voice connections"""
        # Cancel timeout check task
        if self._timeout_check_task and not self._timeout_check_task.done():
            self._timeout_check_task.cancel()
            
        # Cancel all processing tasks
        for guild_tasks in self.processing_tasks.values():
            for task in guild_tasks.values():
                if not task.done():
                    task.cancel()
        
        for guild_id in list(self.voice_connections.keys()):
            await self.disconnect_voice_channel(guild_id)
        
        self.user_audio_buffers.clear()
        self.user_speaking_states.clear()
        self.active_sessions.clear()
        self.guild_empty_since.clear()
        self.user_processing_queue.clear()
        self.processing_tasks.clear()
        self.audio_sinks.clear()
        logger.info("Voice handler cleanup complete")
    
    async def _periodic_timeout_check(self):
        """Periodically check for speech timeouts"""
        logger.info("🕐 Starting periodic timeout check task")
        while True:
            try:
                await asyncio.sleep(0.2)  # Check every 200ms
                
                # Check all active users for timeouts
                for user_id in list(self.speech_detector.user_states.keys()):
                    timeout_audio = self.speech_detector.check_timeout(user_id)
                    
                    if timeout_audio:
                        logger.info(f"🕒 Timeout detected for user {user_id}")
                        
                        # Find guild_id for this user
                        guild_id = None
                        for gid, vc in self.voice_connections.items():
                            if vc.is_connected():
                                try:
                                    guild = vc.guild
                                    member = guild.get_member(user_id)
                                    if member:
                                        guild_id = gid
                                        break
                                except:
                                    pass
                        
                        if guild_id:
                            # Initialize queue if needed
                            if guild_id not in self.user_processing_queue:
                                self.user_processing_queue[guild_id] = {}
                            if user_id not in self.user_processing_queue[guild_id]:
                                self.user_processing_queue[guild_id][user_id] = asyncio.Queue()
                            
                            # Add to processing queue
                            await self.user_processing_queue[guild_id][user_id].put((guild_id, user_id, timeout_audio))
                            
                            # Start processing task if needed
                            if guild_id not in self.processing_tasks:
                                self.processing_tasks[guild_id] = {}
                            if user_id not in self.processing_tasks[guild_id] or self.processing_tasks[guild_id][user_id].done():
                                self.processing_tasks[guild_id][user_id] = asyncio.create_task(
                                    self._process_user_queue(guild_id, user_id)
                                )

                await self._disconnect_empty_voice_channels()

            except Exception as e:
                logger.error(f"Error in timeout check: {e}")
                await asyncio.sleep(1)  # Wait longer on error

    async def _disconnect_empty_voice_channels(self):
        """Disconnect from empty voice channels after the configured timeout."""
        if self.auto_disconnect_timeout <= 0:
            return

        now = time.monotonic()
        for guild_id, voice_client in list(self.voice_connections.items()):
            if not voice_client or not voice_client.is_connected():
                continue

            channel = getattr(voice_client, 'channel', None)
            members = {
                member.id for member in getattr(channel, 'members', [])
                if not getattr(member, 'bot', False)
            }
            self.active_sessions[guild_id] = members
            if members:
                self.guild_empty_since.pop(guild_id, None)
                continue

            empty_since = self.guild_empty_since.setdefault(guild_id, now)
            if now - empty_since >= self.auto_disconnect_timeout:
                logger.info(
                    "Auto-disconnecting from empty Discord voice channel in guild %s",
                    guild_id
                )
                await self.disconnect_voice_channel(guild_id)
