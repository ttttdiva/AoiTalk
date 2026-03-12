
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
load_dotenv()

from src.tools.image_generation import generate_image
from src.tools.utils import extract_original_function

import asyncio

def test_generate_image():
    print("Testing generate_image tool...")
    prompt = "A futuristic city with flying cars at sunset, cyberpunk style"
    
    # Extract original function from FunctionTool
    func = extract_original_function(generate_image)
    
    # Run async function
    result = asyncio.run(func(prompt))
    print(f"Result: {result}")
    
    if "[GENERATED_IMAGE:" in result:
        print("✅ Success: Image tag found.")
        path_start = result.find("[GENERATED_IMAGE:") + len("[GENERATED_IMAGE:")
        path_end = result.find("]", path_start)
        path = result[path_start:path_end]
        print(f"Image path: {path}")
        
        if os.path.exists(path):
            print("✅ Success: Image file exists.")
        else:
            print("❌ Failure: Image file does not exist.")
    else:
        print("❌ Failure: Image tag not found in response.")

if __name__ == "__main__":
    test_generate_image()
