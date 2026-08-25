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

from ..text_content import TextContentError, read_safe_text, read_safe_text_lines

logger = logging.getLogger(__name__)


class FileEditorError(Exception):
    """Exception raised by FileEditor operations."""

    def __init__(self, message: str, *, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


def _run_scoped_path(path: str, *, access: str, operation: str) -> Path:
    """Resolve *path* through the active run contract when one is bound.

    The editor is also used by ordinary user/app APIs, so an absent scope keeps
    the historical path behaviour.  Agent runs bind ``AgentRunScope`` in their
    task context; in that case every path is checked before any filesystem
    operation (including a create with a missing final component).
    """

    try:
        from ...security.agent_run_scope import (
            RunScopeViolation,
            get_current_run_scope,
        )
    except ImportError:  # pragma: no cover - defensive for stripped builds
        return Path(path).resolve()

    scope = get_current_run_scope()
    if scope is None:
        return Path(path).resolve()
    try:
        if access == "read":
            return scope.assert_read_allowed(path)
        if access == "delete":
            return scope.assert_delete_allowed(path)
        return scope.assert_mutation_allowed(path, operation)
    except RunScopeViolation as exc:
        raise FileEditorError(str(exc), error_code="run_scope_violation") from exc


def allowed_absolute_paths() -> List[str]:
    """AOITALK_ALLOWED_PATHS による許可ディレクトリ（未設定なら制限なし）。

    ファイル系ツールの許可パス判定はここ1系統に統一する。呼び出しごとに
    環境変数を読むので、起動後に設定を変えても判定が食い違わない。
    """
    raw = os.environ.get("AOITALK_ALLOWED_PATHS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def path_outside_allowed_error(
    path: str,
    allowed_paths: Optional[List[str]] = None,
) -> Optional[str]:
    """許可ディレクトリ外なら理由文字列、許可されていれば None を返す。"""
    allowed = list(allowed_paths) if allowed_paths is not None else allowed_absolute_paths()
    if not allowed:
        return None

    target = Path(path).resolve()
    for item in allowed:
        try:
            target.relative_to(Path(item).resolve())
            return None
        except ValueError:
            continue
    return f"Path is outside allowed directories: {path}. Allowed: {allowed}"


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
                          If None, AOITALK_ALLOWED_PATHS is read on every check
                          （起動時スナップショットにしない）。
        """
        # 明示指定が無い場合は環境変数を判定のたびに読む。シングルトン生成時に
        # 固定すると、同じ read_file でも行範囲指定の有無で判定が食い違う。
        self._explicit_allowed_paths: Optional[List[str]] = (
            list(allowed_paths) if allowed_paths is not None else None
        )

        # Per-file edit history for undo
        self._file_history: Dict[Path, List[str]] = defaultdict(list)

    @property
    def allowed_paths(self) -> List[str]:
        if self._explicit_allowed_paths is not None:
            return list(self._explicit_allowed_paths)
        return allowed_absolute_paths()

    @allowed_paths.setter
    def allowed_paths(self, value: Optional[List[str]]) -> None:
        self._explicit_allowed_paths = list(value) if value is not None else None

    def _validate_path(
        self,
        path: str,
        must_exist: bool = True,
        allow_create: bool = False,
        max_file_size: Optional[int] = None,
        *,
        access: str = "read",
        operation: str = "read",
    ) -> Path:
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
        file_path = _run_scoped_path(path, access=access, operation=operation)

        # Check if path is within allowed paths (if restrictions are set)
        # Check the canonical candidate.  In a run scope a relative input is
        # resolved against the selected repository, not the process cwd.
        denied = path_outside_allowed_error(str(file_path), self._explicit_allowed_paths)
        if denied:
            raise FileEditorError(denied)

        # Check existence
        if must_exist and not file_path.exists():
            raise FileEditorError(f"File does not exist: {path}")
            
        if file_path.exists() and not allow_create:
            if file_path.is_dir():
                raise FileEditorError(f"Path is a directory, not a file: {path}")
                
            # Read-only callers may provide a separate ceiling without
            # changing the 10MB edit/write limit used by mutation methods.
            size_limit = self.MAX_FILE_SIZE if max_file_size is None else max_file_size
            if file_path.stat().st_size > size_limit:
                raise FileEditorError(
                    f"File too large ({file_path.stat().st_size / 1024 / 1024:.1f}MB). "
                    f"Maximum: {size_limit / 1024 / 1024:.0f}MB"
                )
                
        return file_path
        
    def _read_file(self, path: Path) -> str:
        """Read file contents."""
        try:
            # Imported lazily to avoid coupling module import order while using
            # the same extension hints as preview/full-content reads.
            from ..file_explorer.file_explorer_service import TEXT_EXTENSIONS

            content, _encoding = read_safe_text(
                path,
                known_text_extensions=TEXT_EXTENSIONS,
            )
            return content
        except TextContentError as e:
            raise FileEditorError(
                "Binary files cannot be read as text",
                error_code=getattr(e, "error_code", "binary_file"),
            ) from e
        except Exception as e:
            raise FileEditorError(f"Failed to read file: {e}")
            
    def _write_file(self, path: Path, content: str) -> None:
        """Write content to file."""
        try:
            # Re-check immediately before the write.  The public mutators do a
            # preflight in ``_validate_path``; this second check also protects
            # future/internal callers that invoke ``_write_file`` directly.
            _run_scoped_path(str(path), access="mutation", operation="write")
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
        end_line: Optional[int] = None,
        *,
        max_file_size: Optional[int] = None,
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
        file_path = self._validate_path(
            path,
            must_exist=True,
            max_file_size=max_file_size,
            access="read",
            operation="read",
        )
        size_limit = self.MAX_FILE_SIZE if max_file_size is None else max_file_size

        if start_line is not None or end_line is not None:
            start = start_line if start_line is not None else 1
            end = end_line
            if start < 1:
                raise FileEditorError(f"start_line must be >= 1, got {start}")
            if end is not None and end != -1 and end < start:
                raise FileEditorError(
                    f"end_line ({end}) must be >= start_line ({start})"
                )
            if end == -1:
                end = None

            try:
                from ..file_explorer.file_explorer_service import TEXT_EXTENSIONS

                selected, n_lines, _encoding = read_safe_text_lines(
                    file_path,
                    start_line=start,
                    end_line=end,
                    max_selected_lines=101,
                    max_bytes=size_limit,
                    known_text_extensions=TEXT_EXTENSIONS,
                )
            except TextContentError as e:
                raise FileEditorError(
                    "Binary files cannot be read as text",
                    error_code=getattr(e, "error_code", "binary_file"),
                ) from e

            if start > n_lines:
                raise FileEditorError(
                    f"start_line ({start}) exceeds file length ({n_lines} lines)"
                )
            return self._make_output(
                "\n".join(selected),
                str(file_path),
                init_line=start,
            )

        content = self._read_file(file_path)
        init_line = 1

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
        file_path = self._validate_path(
            path,
            must_exist=False,
            allow_create=True,
            access="mutation",
            operation="create",
        )
        
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
        file_path = self._validate_path(
            path,
            must_exist=True,
            access="mutation",
            operation="edit",
        )
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
        file_path = self._validate_path(
            path,
            must_exist=True,
            access="mutation",
            operation="insert",
        )
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
        file_path = self._validate_path(
            path,
            must_exist=True,
            access="mutation",
            operation="undo",
        )
        
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
        file_path = _run_scoped_path(path, access="read", operation="read")
        return len(self._file_history.get(file_path, []))


# Global instance
_editor: Optional[FileEditor] = None


def get_file_editor() -> FileEditor:
    """Get or create the global FileEditor instance."""
    global _editor
    if _editor is None:
        _editor = FileEditor()
    return _editor
