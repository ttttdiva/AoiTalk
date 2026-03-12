
import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.config import Config
from src.bot.modes.discord_mode import DiscordMode

async def main():
    print("Testing Discord Vision with Gemini...")
    
    # Mock config
    config = Config()
    # Force vision model to gemini
    config.config['discord'] = config.config.get('discord', {})
    config.config['discord']['vision_model'] = 'gemini-3-flash-preview'
    
    # Initialize DiscordMode
    mode = DiscordMode(config)
    
    # Mock image data (1x1 transparent pixel)
    # In a real scenario, this would be downloaded bytes. 
    # Gemini might reject a 1x1 pixel or random bytes if it validates image format strictly.
    # Let's try to use a real tiny valid JPEG structure or similar if possible, 
    # but for now I'll use a hardcoded base64 of a small red dot.
    
    import base64
    # Small red dot 1x1 png
    red_dot_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    image_bytes = base64.b64decode(red_dot_b64)
    
    images_data = [{
        'data': image_bytes,
        'mime_type': 'image/png',
        'url': 'http://mock.url/image.png'
    }]
    
    context = {'history': [], 'character': 'ずんだもん'}
    
    print(f"Model: {config.get('discord.vision_model')}")
    print("Sending request...")
    
    # We are calling the private method directly to test the logic
    # In production, process_text_with_images calls this.
    try:
        response = await mode._generate_response_with_images(
            "この画像について説明して", 
            images_data, 
            context
        )
        print("\nResponse from Gemini:")
        print("-" * 20)
        print(response)
        print("-" * 20)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
