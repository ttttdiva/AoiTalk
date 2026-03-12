# MUST be at the very top of the file, before any other imports
import os
import sys
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

# WSL2環境の自動設定
if 'microsoft' in os.uname().release.lower():
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # PULSE_RUNTIME_PATH設定
    pulse_runtime_path = os.getenv('PULSE_RUNTIME_PATH', '/mnt/wslg/runtime-dir/pulse')
    if os.path.exists(pulse_runtime_path):
        os.environ['PULSE_RUNTIME_PATH'] = pulse_runtime_path
    
    # SDL audio driver設定
    os.environ['SDL_AUDIODRIVER'] = 'pulse'

# Redirect stdout to stderr for anything that bypasses our controls
real_stdout = sys.stdout
sys.stdout = sys.stderr

# Now safe to import other modules
import logging
from mcp.server.fastmcp import FastMCP, Context
import pygame.mixer
import json
from pathlib import Path
from typing import Optional
import asyncio
import subprocess
import tempfile

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("audio-player")

# Restore stdout for MCP protocol messages only
sys.stdout = real_stdout
sys.stdout.reconfigure(line_buffering=True)

# Initialize MCP server
mcp = FastMCP("audio-player")

# Update in player.py
AUDIO_DIR = Path(os.environ.get('AUDIO_PLAYER_DIR', os.path.expanduser('~/Music')))  # More universal default
logger.info(f"Using audio directory: {AUDIO_DIR}")

# Verify directory exists
if not AUDIO_DIR.exists():
    logger.warning(f"Audio directory does not exist: {AUDIO_DIR}")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Created audio directory: {AUDIO_DIR}")

# Simple state management
class AudioState:
    def __init__(self):
        self.volume = 5
        self.playing = None
        self.paused = False

state = AudioState()

def convert_to_mp3(input_file: Path) -> Path:
    """Convert audio file to MP3 format using ffmpeg"""
    try:
        # Create temporary MP3 file
        temp_dir = Path(tempfile.gettempdir()) / "aoi_audio_converted"
        temp_dir.mkdir(exist_ok=True)
        
        output_file = temp_dir / f"{input_file.stem}.mp3"
        
        # Skip conversion if already exists and is newer
        if output_file.exists() and output_file.stat().st_mtime >= input_file.stat().st_mtime:
            logger.info(f"Using cached converted file: {output_file}")
            return output_file
        
        # Convert using ffmpeg
        cmd = [
            'ffmpeg', '-y', '-i', str(input_file),
            '-acodec', 'mp3', '-ab', '192k',
            str(output_file)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"ffmpeg conversion failed: {result.stderr}")
        
        logger.info(f"Converted {input_file.name} to MP3")
        return output_file
        
    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        raise

def find_audio_files(directory: Path, base_path: Path = None, max_depth: int = 3, current_depth: int = 0) -> list:
    """Recursively find audio files in directory and subdirectories"""
    if base_path is None:
        base_path = directory
    
    if current_depth >= max_depth:
        return []
    
    audio_files = []
    try:
        for item in directory.iterdir():
            if item.is_file() and item.suffix.lower() in {'.mp3', '.wav', '.ogg', '.m4a', '.flac'}:
                # Get relative path from base directory
                relative_path = str(item.relative_to(base_path))
                # Get parent directory name for context
                parent_dir = item.parent.name if item.parent != base_path else ""
                audio_files.append({
                    "name": item.name,
                    "path": relative_path,
                    "directory": parent_dir,
                    "full_path": str(item)
                })
            elif item.is_dir() and not item.name.startswith('.'):
                # Recursively search subdirectories
                audio_files.extend(find_audio_files(item, base_path, max_depth, current_depth + 1))
    except PermissionError:
        logger.warning(f"Permission denied accessing: {directory}")
    except Exception as e:
        logger.error(f"Error scanning directory {directory}: {e}")
    
    return audio_files

@mcp.resource("audio://files")
def list_audio_files() -> str:
    """List available audio files"""
    try:
        files = find_audio_files(AUDIO_DIR)
        logger.info(f"Found {len(files)} audio files in {AUDIO_DIR} and subdirectories")
        return json.dumps({"files": files})
    except Exception as e:
        logger.error(f"Error listing audio files: {e}")
        raise


@mcp.tool()
async def list_audio_files(ctx: Context, search_query: Optional[str] = None, limit: int = 100) -> dict:
    """List all available audio files in the audio directory and subdirectories
    
    Args:
        search_query: Optional search term to filter files by name or directory
        limit: Maximum number of files to return (default: 100)
    """
    logger.info(f"Listing audio files via tool (search: {search_query}, limit: {limit})")
    try:
        all_files = find_audio_files(AUDIO_DIR)
        
        # Filter by search query if provided
        if search_query:
            search_lower = search_query.lower()
            files = [
                f for f in all_files
                if search_lower in f["name"].lower() or 
                   search_lower in f["directory"].lower() or
                   search_lower in f["path"].lower()
            ]
            logger.info(f"Found {len(files)} files matching '{search_query}'")
        else:
            files = all_files
        
        # Apply limit to prevent context overflow
        total_count = len(files)
        if len(files) > limit:
            files = files[:limit]
            truncated = True
        else:
            truncated = False
        
        # Group by directory for better organization
        by_directory = {}
        for file in files:
            dir_name = file["directory"] or "[root]"
            if dir_name not in by_directory:
                by_directory[dir_name] = []
            # Only include essential info to reduce response size
            by_directory[dir_name].append({
                "name": file["name"],
                "path": file["path"]
            })
        
        # Log the results
        logger.info(f"Returning {len(files)} of {total_count} audio files across {len(by_directory)} directories")
        ctx.info(f"Retrieved {len(files)} audio files (total: {total_count})")
        
        # Simplified response to avoid context overflow
        response = {
            "status": "success",
            "count": len(files),
            "total_count": total_count,
            "truncated": truncated,
            "directories": list(by_directory.keys())[:20],  # Limit directories shown
            "sample_files": [f["name"] for f in files[:20]]  # Show sample files
        }
        
        # Only include full file list if not too large
        if len(files) <= 50:
            response["files"] = [{"name": f["name"], "path": f["path"]} for f in files]
        else:
            response["message"] = f"Too many files ({total_count}). Use search_query to filter results."
        
        return response
        
    except Exception as e:
        error_msg = f"Error listing audio files: {str(e)}"
        logger.error(error_msg)
        ctx.error(error_msg)
        raise

@mcp.tool()
async def play_audio(filename: str, ctx: Context) -> dict:
    """Play an audio file
    
    Args:
        filename: Either just the filename or a path relative to the audio directory
    """
    logger.info(f"Attempting to play: {filename}")
    
    # Handle both simple filenames and paths
    if '/' in filename or '\\' in filename:
        # It's a path - use it as relative to AUDIO_DIR
        file_path = AUDIO_DIR / filename
    else:
        # Just a filename - search for it
        all_files = find_audio_files(AUDIO_DIR)
        matches = [f for f in all_files if f["name"] == filename]
        
        if not matches:
            # Try partial match
            matches = [f for f in all_files if filename.lower() in f["name"].lower()]
        
        if not matches:
            raise FileNotFoundError(f"Audio file not found: {filename}")
        elif len(matches) > 1:
            # Multiple matches - prefer exact match or ask for clarification
            exact_matches = [f for f in matches if f["name"] == filename]
            if exact_matches:
                file_path = Path(exact_matches[0]["full_path"])
            else:
                paths_info = [f"{f['path']} (in {f['directory'] or 'root'})" for f in matches[:5]]
                raise ValueError(f"Multiple files found matching '{filename}': {', '.join(paths_info)}. Please specify the full path.")
        else:
            file_path = Path(matches[0]["full_path"])
    
    try:
        # Validate file
        if not file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {filename}")
        if not str(file_path.resolve()).startswith(str(AUDIO_DIR.resolve())):
            raise ValueError("File must be in the audio directory")
        
        # Initialize mixer if needed with WSL2 compatibility
        if not pygame.mixer.get_init():
            try:
                # SDL audio driver for WSL2 is already set at the top
                if 'microsoft' in os.uname().release.lower():
                    # Try multiple initialization strategies
                    init_attempts = [
                        # Try with default device
                        lambda: pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512),
                        # Try with smaller buffer
                        lambda: pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=256),
                        # Try with dummy driver as last resort
                        lambda: (os.environ.update({'SDL_AUDIODRIVER': 'dummy'}), pygame.mixer.init())
                    ]
                    
                    for i, attempt in enumerate(init_attempts):
                        try:
                            attempt()
                            logger.info(f"Audio initialized with attempt {i+1}")
                            break
                        except Exception as e:
                            if i == len(init_attempts) - 1:
                                raise e
                            logger.warning(f"Attempt {i+1} failed: {e}")
                else:
                    pygame.mixer.init()
                logger.info("Initialized audio system")
                ctx.info("Initialized audio system")
            except Exception as e:
                # WSL fallback - simulate playback
                logger.warning(f"Audio initialization failed (WSL?): {e}")
                ctx.info("Audio system not available - simulating playback")
                return {
                    "status": "simulated",
                    "file": filename,
                    "message": "Playback simulated (no audio device available)"
                }
        
        # Stop any current playback
        if state.playing:
            pygame.mixer.music.stop()
            ctx.info("Stopped previous playback")
        
        # Load and play
        ctx.info(f"Loading audio file: {filename}")
        try:
            # Convert non-standard formats to MP3 if needed
            play_file = file_path
            if file_path.suffix.lower() in {'.m4a', '.flac'}:
                try:
                    play_file = convert_to_mp3(file_path)
                    logger.info(f"Converted {file_path.name} to MP3 for playback")
                except Exception as conv_error:
                    logger.warning(f"Conversion failed, trying direct playback: {conv_error}")
                    play_file = file_path
            
            pygame.mixer.music.load(str(play_file))
            pygame.mixer.music.set_volume(state.volume / 10.0)
            pygame.mixer.music.play()
            
            # Update state
            state.playing = filename
            state.paused = False
            
            logger.info(f"Playing {filename} at volume {state.volume}/10")
            ctx.info(f"Started playback: {filename}")
            
            return {
                "status": "playing",
                "file": filename,
                "volume": state.volume
            }
        except Exception as e:
            # Fallback for WSL - simulate playback
            logger.warning(f"Audio playback failed (WSL?): {e}")
            ctx.info("Simulating playback due to audio device limitations")
            
            # Update state as if playing
            state.playing = filename
            state.paused = False
            
            return {
                "status": "simulated",
                "file": filename,
                "volume": state.volume,
                "message": f"Successfully simulated playback of {filename} (audio device not available)"
            }
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Playback error: {error_msg}")
        ctx.error(error_msg)
        raise

@mcp.tool()
def stop_playback(ctx: Context) -> dict:
    """Stop playback"""
    try:
        if not pygame.mixer.get_init():
            msg = "Audio system not initialized"
            logger.warning(msg)
            return {"status": "not_initialized", "message": msg}
        
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception as e:
            raise Exception(f"Failed to stop playback: {e}")
        
        state.playing = None
        state.paused = False
        
        msg = "Playback stopped"
        logger.info(msg)
        ctx.info(msg)
        return {"status": "stopped"}
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Stop error: {error_msg}")
        ctx.error(error_msg)
        raise

if __name__ == "__main__":
    logger.info(f"Starting audio player MCP server with directory: {AUDIO_DIR}")
    mcp.run()