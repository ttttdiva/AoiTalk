
import os
import time
from pathlib import Path
import google.generativeai as genai
from .core import tool

@tool
async def generate_image(prompt: str) -> str:
    """Generate an image based on the prompt using Gemini 3 Pro.
    
    Args:
        prompt: Description of the image to generate. Please be descriptive.
        
    Returns:
        String containing the path to the generated image in a special tag format [GENERATED_IMAGE:<path>]
    """
    try:
        print(f"[ImageGeneration] Compiling prompt: {prompt}")
        
        # Ensure API key is set
        api_key = os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
        if not api_key:
            return "エラー: Google APIキーが設定されていません。"
            
        genai.configure(api_key=api_key)
        
        # Initialize model
        # Using the specific model version requested by user
        model_name = "gemini-3-pro-image-preview" 
        try:
            model = genai.GenerativeModel(model_name)
        except Exception as e:
            return f"エラー: モデル {model_name} の初期化に失敗しました: {e}"
            
        # Generate content
        print(f"[ImageGeneration] Generating image with model {model_name}...")
        
        # Run blocking generation in a thread to avoid blocking the event loop
        import asyncio
        import functools
        
        # Check if there is a running loop
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                functools.partial(model.generate_content, prompt)
            )
        except RuntimeError:
            # Fallback for sync execution (e.g. in tests)
            response = model.generate_content(prompt)

        
        # Check if generation was successful
        if not response.parts:
            return "エラー: 画像生成に失敗しました（レスポンスが空です）。"
            
        # Find image part
        image_part = None
        for part in response.parts:
            if hasattr(part, 'inline_data') and part.inline_data:
                image_part = part
                break
                
        if not image_part:
            return "エラー: 生成されたレスポンスに画像データが含まれていませんでした。"
            
        # Save image
        # Create temp directory if it doesn't exist
        output_dir = Path("temp/generated_images")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time())
        filename = f"gen_{timestamp}.jpg" # Defaulting to jpg, though mime_type might vary
        
        # Check mime type to be sure
        mime_type = image_part.inline_data.mime_type
        if "png" in mime_type:
            filename = f"gen_{timestamp}.png"
            
        output_path = output_dir / filename
        
        with open(output_path, "wb") as f:
            f.write(image_part.inline_data.data)
            
        abs_path = output_path.resolve()
        print(f"[ImageGeneration] Image saved to {abs_path}")
        
        # Return special tag for Discord bot to pick up
        return f"[GENERATED_IMAGE:{str(abs_path)}]"
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"エラー: 画像生成中に問題が発生しました: {str(e)}"
