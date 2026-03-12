"""
File Editor for AoiTalk

Provides file editing capabilities with:
- View files with line numbers
- Create new files
- Edit via string replacement
- Insert at specific line
- Undo/redo support

Based on Open Interpreter's edit.py patterns.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class FileEditorError(Exception):
    """Exception raised by FileEditor operations."""
    pass


class FileEditor:
    """
    File editing engine with undo support.
    
    Features:
    - View file contents with line numbers
    - Create new files
    - Edit files via string replacement (str_replace)
    - Insert content at specific lines
    - Undo recent edits (per-file history)
    - Path validation and security checks
    """
    
    # Maximum lines to show in snippets
    SNIPPET_LINES: int = 4
    # Maximum file size to read (10MB)
    MAX_FILE_SIZE: int = 10 * 1024 * 1024
    
    def __init__(self, allowed_paths: Optional[List[str]] = None):
        """
        Initialize the file editor.
        
        Args:
            allowed_paths: List of paths where files can be edited.
                          If None, loads from AOITALK_ALLOWED_PATHS env var.
        """
        # Load allowed paths from environment if not specified
        if allowed_paths is None:
            env_paths = os.environ.get("AOITALK_ALLOWED_PATHS", "")
            if env_paths:
                self.allowed_paths = [p.strip() for p in env_paths.split(",") if p.strip()]
            else:
                self.allowed_paths = []
        else:
            self.allowed_paths = allowed_paths
            
        # Per-file edit history for undo
        self._file_history: Dict[Path, List[str]] = defaultdict(list)
        
    def _validate_path(self, path: str, must_exist: bool = True, allow_create: bool = False) -> Path:
        """
        Validate and resolve a file path.
        
        Args:
            path: The file path to validate.
            must_exist: If True, raises error if file doesn't exist.
            allow_create: If True, allows non-existent paths for file creation.
            
        Returns:
            Resolved Path object.
            
        Raises:
            FileEditorError: If path is invalid or outside allowed paths.
        """
        file_path = Path(path).resolve()
        
        # Check if path is within allowed paths (if restrictions are set)
        if self.allowed_paths:
            allowed = False
            for allowed_path in self.allowed_paths:
                try:
                    file_path.relative_to(Path(allowed_path).resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise FileEditorError(
                    f"Path is outside allowed directories: {path}. "
                    f"Allowed: {self.allowed_paths}"
                )
                
        # Check existence
        if must_exist and not file_path.exists():
            raise FileEditorError(f"File does not exist: {path}")
            
        if file_path.exists() and not allow_create:
            if file_path.is_dir():
                raise FileEditorError(f"Path is a directory, not a file: {path}")
                
            # Check file size
            if file_path.stat().st_size > self.MAX_FILE_SIZE:
                raise FileEditorError(
                    f"File too large ({file_path.stat().st_size / 1024 / 1024:.1f}MB). "
                    f"Maximum: {self.MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
                )
                
        return file_path
        
    def _read_file(self, path: Path) -> str:
        """Read file contents."""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Try with other encodings
            for encoding in ["utf-8-sig", "cp932", "shift_jis", "latin-1"]:
                try:
                    return path.read_text(encoding=encoding)
                except UnicodeDecodeError:
                    continue
            raise FileEditorError(f"Could not decode file: {path}")
        except Exception as e:
            raise FileEditorError(f"Failed to read file: {e}")
            
    def _write_file(self, path: Path, content: str) -> None:
        """Write content to file."""
        try:
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            raise FileEditorError(f"Failed to write file: {e}")
            
    def _make_output(
        self,
        content: str,
        file_descriptor: str,
        init_line: int = 1,
        max_lines: int = 100
    ) -> str:
        """
        Format file content with line numbers for display.
        
        Args:
            content: File content to format.
            file_descriptor: Description of the file.
            init_line: Starting line number.
            max_lines: Maximum lines to show (truncates if exceeded).
            
        Returns:
            Formatted output string.
        """
        lines = content.split("\n")
        
        if len(lines) > max_lines:
            truncated = True
            lines = lines[:max_lines]
        else:
            truncated = False
            
        # Add line numbers
        numbered_lines = [
            f"{i + init_line:6}\t{line}"
            for i, line in enumerate(lines)
        ]
        
        output = f"Content of {file_descriptor}:\n" + "\n".join(numbered_lines)
        
        if truncated:
            output += f"\n... (truncated, showing first {max_lines} lines)"
            
        return output
        
    def view(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None
    ) -> str:
        """
        View file contents with line numbers.
        
        Args:
            path: Path to the file.
            start_line: Starting line (1-indexed, inclusive).
            end_line: Ending line (1-indexed, inclusive). Use -1 for end of file.
            
        Returns:
            Formatted file content with line numbers.
        """
        file_path = self._validate_path(path, must_exist=True)
        content = self._read_file(file_path)
        lines = content.split("\n")
        n_lines = len(lines)
        
        init_line = 1
        
        if start_line is not None or end_line is not None:
            # Validate line range
            start = start_line if start_line is not None else 1
            end = end_line if end_line is not None else n_lines
            
            if start < 1:
                raise FileEditorError(f"start_line must be >= 1, got {start}")
            if start > n_lines:
                raise FileEditorError(
                    f"start_line ({start}) exceeds file length ({n_lines} lines)"
                )
            if end != -1 and end < start:
                raise FileEditorError(
                    f"end_line ({end}) must be >= start_line ({start})"
                )
                
            init_line = start
            
            if end == -1:
                content = "\n".join(lines[start - 1:])
            else:
                content = "\n".join(lines[start - 1:end])
                
        return self._make_output(content, str(file_path), init_line=init_line)
        
    def create(self, path: str, content: str) -> str:
        """
        Create a new file with the given content.
        
        Args:
            path: Path for the new file.
            content: Content to write.
            
        Returns:
            Success message.
            
        Raises:
            FileEditorError: If file already exists or path is invalid.
        """
        file_path = self._validate_path(path, must_exist=False, allow_create=True)
        
        if file_path.exists():
            raise FileEditorError(
                f"File already exists: {path}. "
                "Use edit_file to modify existing files, or delete first."
            )
            
        self._write_file(file_path, content)
        self._file_history[file_path].append(content)
        
        logger.info(f"Created file: {file_path}")
        return f"File created successfully: {file_path}"
        
    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        """
        Replace a string in the file.
        
        The old_str must appear exactly once in the file (for safety).
        
        Args:
            path: Path to the file.
            old_str: String to find and replace.
            new_str: Replacement string.
            
        Returns:
            Success message with snippet of changes.
            
        Raises:
            FileEditorError: If old_str not found or appears multiple times.
        """
        file_path = self._validate_path(path, must_exist=True)
        content = self._read_file(file_path)
        
        # Normalize tabs for consistent matching
        content_normalized = content.expandtabs()
        old_str_normalized = old_str.expandtabs()
        new_str_normalized = new_str.expandtabs() if new_str else ""
        
        # Check occurrences
        occurrences = content_normalized.count(old_str_normalized)
        
        if occurrences == 0:
            # Try to find similar content for helpful error message
            raise FileEditorError(
                f"String not found in file. Make sure the text matches exactly, "
                f"including whitespace and line endings."
            )
        elif occurrences > 1:
            # Find line numbers where it appears
            lines = content_normalized.split("\n")
            matching_lines = [
                idx + 1 for idx, line in enumerate(lines)
                if old_str_normalized in line
            ]
            raise FileEditorError(
                f"String appears {occurrences} times (lines: {matching_lines}). "
                f"Please provide a more specific string that appears only once."
            )
            
        # Save current content to history
        self._file_history[file_path].append(content)
        
        # Perform replacement
        new_content = content_normalized.replace(old_str_normalized, new_str_normalized)
        self._write_file(file_path, new_content)
        
        # Create snippet of the change
        replacement_line = content_normalized.split(old_str_normalized)[0].count("\n")
        start_line = max(0, replacement_line - self.SNIPPET_LINES)
        end_line = replacement_line + self.SNIPPET_LINES + new_str_normalized.count("\n")
        snippet = "\n".join(new_content.split("\n")[start_line:end_line + 1])
        
        logger.info(f"Edited file: {file_path}")
        
        result = f"File edited successfully: {file_path}\n\n"
        result += self._make_output(snippet, "edited section", init_line=start_line + 1)
        result += "\n\nReview the changes and use undo_edit if needed."
        
        return result
        
    def insert(self, path: str, line_number: int, content: str) -> str:
        """
        Insert content at a specific line.
        
        Args:
            path: Path to the file.
            line_number: Line number where to insert (0 = beginning, n = after line n).
            content: Content to insert.
            
        Returns:
            Success message with snippet.
        """
        file_path = self._validate_path(path, must_exist=True)
        file_content = self._read_file(file_path)
        lines = file_content.split("\n")
        n_lines = len(lines)
        
        if line_number < 0 or line_number > n_lines:
            raise FileEditorError(
                f"Invalid line_number: {line_number}. "
                f"Must be between 0 and {n_lines}."
            )
            
        # Save to history
        self._file_history[file_path].append(file_content)
        
        # Insert content
        content_lines = content.expandtabs().split("\n")
        new_lines = lines[:line_number] + content_lines + lines[line_number:]
        new_content = "\n".join(new_lines)
        
        self._write_file(file_path, new_content)
        
        # Create snippet
        start = max(0, line_number - self.SNIPPET_LINES)
        end = line_number + len(content_lines) + self.SNIPPET_LINES
        snippet = "\n".join(new_lines[start:end])
        
        logger.info(f"Inserted content at line {line_number} in: {file_path}")
        
        result = f"Content inserted at line {line_number}: {file_path}\n\n"
        result += self._make_output(snippet, "inserted section", init_line=start + 1)
        
        return result
        
    def undo(self, path: str) -> str:
        """
        Undo the last edit to a file.
        
        Args:
            path: Path to the file.
            
        Returns:
            Success message with restored content snippet.
        """
        file_path = self._validate_path(path, must_exist=True)
        
        if not self._file_history[file_path]:
            raise FileEditorError(f"No edit history for: {path}")
            
        # Pop the last saved state
        previous_content = self._file_history[file_path].pop()
        self._write_file(file_path, previous_content)
        
        logger.info(f"Undid last edit: {file_path}")
        
        result = f"Undo successful: {file_path}\n\n"
        result += self._make_output(previous_content, "restored content", max_lines=50)
        
        return result
        
    def get_history_count(self, path: str) -> int:
        """Get the number of undo steps available for a file."""
        file_path = Path(path).resolve()
        return len(self._file_history.get(file_path, []))


# Global instance
_editor: Optional[FileEditor] = None


def get_file_editor() -> FileEditor:
    """Get or create the global FileEditor instance."""
    global _editor
    if _editor is None:
        _editor = FileEditor()
    return _editor
