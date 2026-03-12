#!/usr/bin/env python
# coding=utf-8
"""
Qwen3-TTS Voice Management Script

This script helps manage voice embeddings for Qwen3-TTS engine.
You can add, list, and delete voice embeddings.

Usage:
    # Add a voice from an audio file
    python scripts/manage_qwen3_voices.py --add path/to/audio.wav --name my_voice --text "transcription text"
    
    # List all saved voices
    python scripts/manage_qwen3_voices.py --list
    
    # Delete a voice
    python scripts/manage_qwen3_voices.py --delete my_voice
"""
import argparse
import asyncio
import sys
import os
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.tts.engines.qwen3_tts_engine import Qwen3TTSEngine


async def add_voice(args):
    """Add a new voice embedding"""
    print(f"Creating voice embedding: {args.name}")
    print(f"Audio file: {args.audio}")
    print(f"Transcript: {args.text}")
    
    # Initialize engine
    engine = Qwen3TTSEngine(
        voices_dir=args.voices_dir,
        use_gpu=args.use_gpu,
    )
    
    if not await engine.initialize():
        print("Failed to initialize Qwen3-TTS engine")
        return False
    
    # Save voice
    success = await engine.save_voice(
        audio_path=args.audio,
        voice_name=args.name,
        ref_text=args.text,
        language=args.language,
        description=args.description or "",
        x_vector_only=args.x_vector_only,
    )
    
    await engine.cleanup()
    
    if success:
        print(f"\n✓ Voice '{args.name}' created successfully!")
        print(f"  Location: {os.path.join(engine.voices_dir, f'{args.name}.pkl')}")
        print(f"\nYou can now use this voice in character configs:")
        print(f"  voice:")
        print(f"    engine: qwen3tts")
        print(f"    voice_name: {args.name}")
        return True
    else:
        print(f"\n✗ Failed to create voice '{args.name}'")
        return False


async def list_voices(args):
    """List all saved voices"""
    # Initialize engine (doesn't load model, just reads index)
    engine = Qwen3TTSEngine(voices_dir=args.voices_dir)
    engine._load_voices_index()
    
    voices = engine.get_voices()
    
    if not voices:
        print("No voices found.")
        print(f"Voice directory: {engine.voices_dir}")
        return
    
    print(f"\nFound {len(voices)} voice(s):\n")
    print("-" * 80)
    
    for voice in voices:
        print(f"Name:        {voice['name']}")
        print(f"Language:    {voice.get('language', 'Unknown')}")
        print(f"Description: {voice.get('description', '-')}")
        print(f"Created:     {voice.get('created_at', 'Unknown')}")
        print(f"Mode:        {'X-vector only' if voice.get('x_vector_only', False) else 'Full ICL'}")
        print(f"File:        {voice['file']}")
        print("-" * 80)


async def delete_voice(args):
    """Delete a voice embedding"""
    # Initialize engine
    engine = Qwen3TTSEngine(voices_dir=args.voices_dir)
    engine._load_voices_index()
    
    if args.name not in engine.voices:
        print(f"✗ Voice '{args.name}' not found")
        return False
    
    # Confirm deletion
    if not args.force:
        response = input(f"Delete voice '{args.name}'? (y/N): ")
        if response.lower() != 'y':
            print("Cancelled")
            return False
    
    # Delete voice file
    voice_file = engine.voices[args.name]['file']
    try:
        if os.path.exists(voice_file):
            os.remove(voice_file)
            print(f"Removed file: {voice_file}")
        
        # Remove from index
        del engine.voices[args.name]
        engine._save_voices_index()
        
        print(f"✓ Voice '{args.name}' deleted successfully")
        return True
        
    except Exception as e:
        print(f"✗ Error deleting voice: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Manage Qwen3-TTS voice embeddings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Add a voice with audio file and transcript
  python scripts/manage_qwen3_voices.py --add sample.wav --name "alice" --text "Hello, this is a test."
  
  # Add a voice with x-vector only mode (faster, lower quality)
  python scripts/manage_qwen3_voices.py --add sample.wav --name "bob" --text "Hi there" --x-vector-only
  
  # List all voices
  python scripts/manage_qwen3_voices.py --list
  
  # Delete a voice
  python scripts/manage_qwen3_voices.py --delete "alice"
        """
    )
    
    # Action arguments
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument('--add', metavar='AUDIO', help='Add a voice from audio file')
    action_group.add_argument('--list', action='store_true', help='List all saved voices')
    action_group.add_argument('--delete', metavar='NAME', help='Delete a voice by name')
    
    # Add voice arguments
    parser.add_argument('--name', help='Name for the voice (required with --add)')
    parser.add_argument('--text', help='Transcription of the audio (required with --add)')
    parser.add_argument('--language', default='Auto', help='Language of the audio (default: Auto)')
    parser.add_argument('--description', help='Description of the voice')
    parser.add_argument('--x-vector-only', action='store_true',
                        help='Use x-vector only mode (faster but lower quality)')
    
    # Common arguments
    parser.add_argument('--voices-dir', default='cache/qwen3_voices',
                        help='Directory for voice embeddings (default: cache/qwen3_voices)')
    parser.add_argument('--use-gpu', action='store_true', help='Use GPU for voice extraction')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompts')
    
    args = parser.parse_args()
    
    # Validate add arguments
    if args.add:
        if not args.name:
            parser.error("--add requires --name")
        if not args.text and not args.x_vector_only:
            parser.error("--add requires --text (or use --x-vector-only)")
        if not os.path.exists(args.add):
            parser.error(f"Audio file not found: {args.add}")
        args.audio = args.add
    
    # Run action
    try:
        if args.add:
            success = asyncio.run(add_voice(args))
            sys.exit(0 if success else 1)
        elif args.list:
            asyncio.run(list_voices(args))
            sys.exit(0)
        elif args.delete:
            success = asyncio.run(delete_voice(args))
            sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nCancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
