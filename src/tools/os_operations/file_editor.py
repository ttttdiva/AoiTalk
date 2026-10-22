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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..text_content import TextContentError, read_safe_text, read_safe_text_lines
from . import file_edit_helpers as _edit

logger = logging.getLogger(__name__)

#: Maximum number of lines a single rendered view displays.
DISPLAY_MAX_LINES = 100


def _display_window(lines: List[str], max_lines: int) -> Tuple[List[str], bool]:
    """Return the lines a view actually renders and whether it was truncated.

    Single source of truth for the display cap: both the rendered text and the
    structured window metadata come from this slice, so they cannot disagree.
    """
    if len(lines) > max_lines:
        return lines[:max_lines], True
    return lines, False


@dataclass(frozen=True)
class ViewWindow:
    """Rendered view text together with the window that was really displayed.

    ``end_line`` / ``next_start_line`` describe the rendered text, not the
    request, so callers never report a line number the user cannot see.
    """

    text: str
    start_line: int
    end_line: Optional[int]
    total_lines: int
    truncated: bool
    next_start_line: Optional[int]


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
        return self._read_file_with_encoding(path)[0]

    def _read_file_with_encoding(self, path: Path) -> Tuple[str, str]:
        """Read file contents and the detected encoding (BOM aware)."""
        try:
            # Imported lazily to avoid coupling module import order while using
            # the same extension hints as preview/full-content reads.
            from ..file_explorer.file_explorer_service import TEXT_EXTENSIONS

            return read_safe_text(
                path,
                known_text_extensions=TEXT_EXTENSIONS,
            )
        except TextContentError as e:
            raise FileEditorError(
                "Binary files cannot be read as text",
                error_code=getattr(e, "error_code", "binary_file"),
            ) from e
        except Exception as e:
            raise FileEditorError(f"Failed to read file: {e}")

    @staticmethod
    def _encode_like(content: str, encoding: str) -> bytes:
        """Encode text with the file's original encoding, restoring any BOM."""
        import codecs

        boms = {
            "utf-8-sig": codecs.BOM_UTF8,
            "utf-16-le": codecs.BOM_UTF16_LE,
            "utf-16-be": codecs.BOM_UTF16_BE,
            "utf-32-le": codecs.BOM_UTF32_LE,
            "utf-32-be": codecs.BOM_UTF32_BE,
        }
        codec = "utf-8" if encoding == "utf-8-sig" else encoding
        return boms.get(encoding, b"") + content.encode(codec)

    def _write_text(self, path: Path, content: str, encoding: str = "utf-8") -> Tuple[int, str]:
        """Write byte-exact in the original encoding and verify by readback.

        Returns ``(size, sha256)`` of the bytes now on disk.
        """
        try:
            _run_scoped_path(str(path), access="mutation", operation="write")
            data = self._encode_like(content, encoding)
            with path.open("wb") as stream:
                stream.write(data)
            on_disk = path.read_bytes()
        except Exception as e:
            raise FileEditorError(f"Failed to write file: {e}")
        if on_disk != data:
            raise FileEditorError(
                f"Write verification failed for {path}: bytes on disk differ from what was written. "
                "Re-read the file with read_file(start_line, end_line) before retrying."
            )
        return len(on_disk), _edit.sha256_hex(on_disk)

    def _write_file(self, path: Path, content: str) -> None:
        """Write content to file."""
        try:
            # Re-check immediately before the write.  The public mutators do a
            # preflight in ``_validate_path``; this second check also protects
            # future/internal callers that invoke ``_write_file`` directly.
            _run_scoped_path(str(path), access="mutation", operation="write")
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" keeps the content byte-exact: reads do not normalise line
            # endings, so the Windows LF->CRLF translation would corrupt LF files and
            # double the CR of CRLF files.
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(content)
        except Exception as e:
            raise FileEditorError(f"Failed to write file: {e}")
            
    def _make_output(
        self,
        content: str,
        file_descriptor: str,
        init_line: int = 1,
        max_lines: int = DISPLAY_MAX_LINES
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
        
        lines, truncated = _display_window(lines, max_lines)
            
        # Add line numbers
        numbered_lines = [
            f"{i + init_line:6}\t{line}"
            for i, line in enumerate(lines)
        ]
        
        output = f"Content of {file_descriptor}:\n" + "\n".join(numbered_lines)
        
        if truncated:
            output += f"\n... (truncated, showing first {max_lines} lines)"
            
        return output

    def _window_from_lines(
        self,
        lines: List[str],
        file_descriptor: str,
        *,
        init_line: int,
        total_lines: int,
        max_lines: int = DISPLAY_MAX_LINES,
    ) -> ViewWindow:
        """Render ``lines`` and report only the window that was displayed.

        The text comes from :meth:`_make_output` and the window comes from
        :func:`_display_window`; both use the same cap, so the metadata can
        never advertise a line the rendered text does not contain.
        """
        displayed, truncated = _display_window(lines, max_lines)
        text = self._make_output(
            "\n".join(lines),
            file_descriptor,
            init_line=init_line,
            max_lines=max_lines,
        )
        end_line = (init_line + len(displayed) - 1) if displayed else None
        return ViewWindow(
            text=text,
            start_line=init_line,
            end_line=end_line,
            total_lines=total_lines,
            truncated=truncated,
            next_start_line=(
                end_line + 1 if truncated and end_line is not None else None
            ),
        )

    def view(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        *,
        max_file_size: Optional[int] = None,
    ) -> str:
        """Return the rendered view text.

        Kept as the string-compatible entry point; :meth:`view_window` also
        reports which lines the rendered text really contains.
        """
        return self.view_window(
            path,
            start_line=start_line,
            end_line=end_line,
            max_file_size=max_file_size,
        ).text

    def view_window(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        *,
        max_file_size: Optional[int] = None,
    ) -> ViewWindow:
        """
        View file contents with line numbers.
        
        Args:
            path: Path to the file.
            start_line: Starting line (1-indexed, inclusive).
            end_line: Ending line (1-indexed, inclusive). Use -1 for end of file.
            
        Returns:
            A ``ViewWindow`` whose ``text`` is the rendered file content with
            line numbers and whose ``start_line`` / ``end_line`` /
            ``total_lines`` / ``truncated`` / ``next_start_line`` describe the
            window that text actually contains.
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
                raise FileEditorError(
                    f"start_line must be >= 1, got {start}",
                    error_code="read_invalid_line_range",
                )
            if end is not None and end != -1 and end < start:
                raise FileEditorError(
                    f"end_line ({end}) must be >= start_line ({start})",
                    error_code="read_invalid_line_range",
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
                    f"start_line ({start}) exceeds file length ({n_lines} lines)",
                    error_code="read_invalid_line_range",
                )
            return self._window_from_lines(
                selected,
                str(file_path),
                init_line=start,
                total_lines=n_lines,
            )

        content = self._read_file(file_path)
        init_line = 1
        view_lines = content.split("\n")

        return self._window_from_lines(
            view_lines,
            str(file_path),
            init_line=init_line,
            total_lines=len(view_lines),
        )

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
                f"File already exists: {file_path}. "
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
        content, encoding = self._read_file_with_encoding(file_path)

        if not old_str:
            raise FileEditorError(
                "old_str is empty. Provide the exact existing text to replace "
                "(use insert_to_file to add text at a line)."
            )
        if old_str == new_str:
            raise FileEditorError(
                "old_str and new_str are identical; nothing to change."
            )

        resolution = _edit.resolve_matches(content, old_str, new_str)
        matches = resolution.matches

        if not matches:
            raise FileEditorError(
                _edit.describe_missing(content, old_str, new_str, resolution.notes)
            )
        if len(matches) > 1:
            raise FileEditorError(_edit.describe_ambiguous(content, matches))

        match = matches[0]
        new_content = content[: match.start] + resolution.new + content[match.end :]

        # Save current content to history
        self._file_history[file_path].append(content)

        size, digest = self._write_text(file_path, new_content, encoding)

        # Snippet around the actual changed region (original, untouched text)
        start_line = max(0, match.start_line - 1 - self.SNIPPET_LINES)
        end_line = match.start_line - 1 + self.SNIPPET_LINES + resolution.new.count("\n")
        snippet = "\n".join(new_content.split("\n")[start_line:end_line + 1])

        logger.info(f"Edited file: {file_path}")

        new_last = match.start_line + resolution.new.count("\n")
        span = (
            f"line {match.start_line}"
            if match.start_line == new_last
            else f"lines {match.start_line}-{new_last}"
        )
        result = f"File edited successfully: {file_path}\n"
        result += f"Changed {span}. Saved {size} bytes, sha256 {digest[:12]} (verified by readback).\n"
        for note in resolution.notes:
            result += f"Note: {note}.\n"
        result += "\n" + self._make_output(snippet, "edited section", init_line=start_line + 1)
        result += "\n\nDiff:\n" + _edit.build_diff(str(file_path), content, new_content)
        syntax = _edit.syntax_note(str(file_path), content, new_content)
        if syntax:
            result += "\n\n" + syntax
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
        file_content, encoding = self._read_file_with_encoding(file_path)
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
        content_lines = content.split("\n")
        new_lines = lines[:line_number] + content_lines + lines[line_number:]
        new_content = "\n".join(new_lines)
        
        self._write_text(file_path, new_content, encoding)
        
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
        _current, encoding = self._read_file_with_encoding(file_path)
        self._write_text(file_path, previous_content, encoding)
        
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
