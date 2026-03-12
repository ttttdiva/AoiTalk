"""
Documents processing tools.
"""
from .office_reader import (
    convert_office_bytes_to_markdown,
    convert_office_file_to_markdown,
    is_supported,
    SUPPORTED_EXTENSIONS,
)

__all__ = [
    "convert_office_bytes_to_markdown",
    "convert_office_file_to_markdown",
    "is_supported",
    "SUPPORTED_EXTENSIONS",
]
