"""
Office Document Reader - Convert Office files to Markdown using MarkItDown.

Supports:
- Word documents (.docx)
- Excel spreadsheets (.xlsx)
- PowerPoint presentations (.pptx)
- PDF documents (.pdf)
"""
import io
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def convert_office_bytes_to_markdown(content: bytes, filename: str) -> Dict[str, Any]:
    """
    Convert Office file bytes to Markdown text.
    
    Args:
        content: File content as bytes
        filename: Original filename (used to detect file type)
        
    Returns:
        Dict with success, content, and optional error
    """
    try:
        from markitdown import MarkItDown
        
        md = MarkItDown()
        file_extension = Path(filename).suffix.lower()
        
        # Convert using stream
        result = md.convert_stream(io.BytesIO(content), file_extension=file_extension)
        
        if result and result.text_content:
            return {
                "success": True,
                "content": result.text_content,
                "filename": filename
            }
        else:
            return {
                "success": False,
                "error": "変換結果が空でした"
            }
            
    except ImportError:
        logger.error("markitdown is not installed")
        return {
            "success": False,
            "error": "markitdown がインストールされていません"
        }
    except Exception as e:
        logger.error(f"Failed to convert office file: {e}")
        return {
            "success": False,
            "error": f"ファイル変換に失敗しました: {str(e)}"
        }


def convert_office_file_to_markdown(file_path: str) -> Dict[str, Any]:
    """
    Convert Office file to Markdown text.
    
    Args:
        file_path: Path to the Office file
        
    Returns:
        Dict with success, content, and optional error
    """
    path = Path(file_path)
    
    if not path.exists():
        return {
            "success": False,
            "error": f"ファイルが見つかりません: {file_path}"
        }
    
    try:
        content = path.read_bytes()
        return convert_office_bytes_to_markdown(content, path.name)
    except Exception as e:
        logger.error(f"Failed to read file: {e}")
        return {
            "success": False,
            "error": f"ファイル読み込みに失敗しました: {str(e)}"
        }


# Supported extensions
SUPPORTED_EXTENSIONS = {'.docx', '.xlsx', '.pptx', '.pdf'}


def is_supported(filename: str) -> bool:
    """Check if the file extension is supported."""
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
