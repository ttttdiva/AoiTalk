"""
File System utilities for AoiTalk

Provides file system operations:
- List directory contents
- Search files by name or content
- Get file information

Based on Open Interpreter's files.py patterns.
"""

import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FileInfo:
    """Information about a file or directory."""
    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified_at: Optional[str] = None
    extension: Optional[str] = None


class FileSystemError(Exception):
    """Exception raised by FileSystem operations."""
    pass


class FileSystem:
    """
    File system operations utility.
    
    Features:
    - List directory contents with depth control
    - Search files by name pattern
    - Search files by content (grep-like)
    - Get file/directory information
    """
    
    # Extensions to skip when searching content
    BINARY_EXTENSIONS = {
        ".exe", ".dll", ".so", ".dylib", ".bin",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
        ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac",
        ".zip", ".tar", ".gz", ".rar", ".7z",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".pyc", ".pyo", ".class", ".o", ".obj",
        ".db", ".sqlite", ".sqlite3",
    }
    
    # Directories to skip when searching
    SKIP_DIRS = {
        ".git", ".svn", ".hg",
        "node_modules", "__pycache__", ".venv", "venv",
        ".idea", ".vscode",
        "dist", "build", "target",
    }
    
    def __init__(self, allowed_paths: Optional[List[str]] = None):
        """
        Initialize the file system utility.
        
        Args:
            allowed_paths: List of paths where operations are allowed.
                          If None, loads from AOITALK_ALLOWED_PATHS env var.
        """
        if allowed_paths is None:
            env_paths = os.environ.get("AOITALK_ALLOWED_PATHS", "")
            if env_paths:
                self.allowed_paths = [p.strip() for p in env_paths.split(",") if p.strip()]
            else:
                self.allowed_paths = []
        else:
            self.allowed_paths = allowed_paths
            
    def _validate_path(self, path: str) -> Path:
        """Validate and resolve a path."""
        resolved = Path(path).resolve()
        
        if self.allowed_paths:
            allowed = False
            for allowed_path in self.allowed_paths:
                try:
                    resolved.relative_to(Path(allowed_path).resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise FileSystemError(
                    f"Path is outside allowed directories: {path}. "
                    f"Allowed: {self.allowed_paths}"
                )
                
        return resolved
        
    def list_directory(
        self,
        path: str,
        max_depth: int = 2,
        show_hidden: bool = False,
        pattern: Optional[str] = None
    ) -> str:
        """
        List contents of a directory.
        
        Args:
            path: Directory path to list.
            max_depth: Maximum depth to recurse (1 = current dir only).
            show_hidden: Whether to show hidden files/directories.
            pattern: Optional glob pattern to filter results.
            
        Returns:
            Formatted listing of directory contents.
        """
        dir_path = self._validate_path(path)
        
        if not dir_path.exists():
            raise FileSystemError(f"Directory does not exist: {path}")
        if not dir_path.is_dir():
            raise FileSystemError(f"Path is not a directory: {path}")
            
        items: List[FileInfo] = []
        
        def collect_items(current: Path, depth: int):
            if depth > max_depth:
                return
                
            try:
                for item in sorted(current.iterdir()):
                    # Skip hidden files unless requested
                    if not show_hidden and item.name.startswith("."):
                        continue
                        
                    # Skip common uninteresting directories
                    if item.is_dir() and item.name in self.SKIP_DIRS:
                        continue
                        
                    # Apply pattern filter if specified
                    if pattern and not fnmatch.fnmatch(item.name, pattern):
                        if item.is_dir():
                            collect_items(item, depth + 1)
                        continue
                        
                    try:
                        stat = item.stat()
                        info = FileInfo(
                            path=str(item),
                            name=item.name,
                            is_dir=item.is_dir(),
                            size=stat.st_size if not item.is_dir() else 0,
                            modified_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            extension=item.suffix.lower() if not item.is_dir() else None
                        )
                        items.append(info)
                    except (PermissionError, OSError):
                        # Skip files we can't access
                        continue
                        
                    # Recurse into directories
                    if item.is_dir() and depth < max_depth:
                        collect_items(item, depth + 1)
                        
            except PermissionError:
                pass
                
        collect_items(dir_path, 1)
        
        # Format output
        if not items:
            return f"Directory is empty or no matching files: {path}"
            
        # Group by directory
        output_lines = [f"Contents of {dir_path} (max depth: {max_depth}):"]
        output_lines.append("")
        
        for item in items:
            relative = Path(item.path).relative_to(dir_path)
            indent = "  " * (len(relative.parts) - 1)
            
            if item.is_dir:
                output_lines.append(f"{indent}📁 {item.name}/")
            else:
                size_str = self._format_size(item.size)
                output_lines.append(f"{indent}📄 {item.name} ({size_str})")
                
        output_lines.append("")
        output_lines.append(f"Total: {len(items)} items")
        
        return "\n".join(output_lines)
        
    def search_files(
        self,
        query: str,
        path: str,
        extensions: Optional[List[str]] = None,
        search_content: bool = False,
        max_results: int = 50
    ) -> str:
        """
        Search for files by name or content.
        
        Args:
            query: Search query (glob pattern for names, regex for content).
            path: Directory to search in.
            extensions: Filter by file extensions (e.g., [".py", ".js"]).
            search_content: If True, search file contents instead of names.
            max_results: Maximum number of results to return.
            
        Returns:
            Formatted search results.
        """
        search_path = self._validate_path(path)
        
        if not search_path.exists():
            raise FileSystemError(f"Path does not exist: {path}")
            
        results: List[str] = []
        
        if search_content:
            results = self._search_content(search_path, query, extensions, max_results)
        else:
            results = self._search_names(search_path, query, extensions, max_results)
            
        if not results:
            return f"No files found matching '{query}' in {path}"
            
        output = [f"Search results for '{query}' in {path}:"]
        output.append("")
        output.extend(results)
        
        if len(results) >= max_results:
            output.append(f"\n... (showing first {max_results} results)")
            
        return "\n".join(output)
        
    def _search_names(
        self,
        path: Path,
        pattern: str,
        extensions: Optional[List[str]],
        max_results: int
    ) -> List[str]:
        """Search files by name pattern."""
        results = []
        
        # Support both glob and simple substring matching
        if "*" in pattern or "?" in pattern:
            matcher = lambda name: fnmatch.fnmatch(name.lower(), pattern.lower())
        else:
            matcher = lambda name: pattern.lower() in name.lower()
            
        for root, dirs, files in os.walk(path):
            # Skip hidden and common uninteresting directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in self.SKIP_DIRS]
            
            for filename in files:
                if len(results) >= max_results:
                    return results
                    
                filepath = Path(root) / filename
                
                # Check extension filter
                if extensions:
                    if filepath.suffix.lower() not in [e.lower() for e in extensions]:
                        continue
                        
                # Check name match
                if matcher(filename):
                    relative = filepath.relative_to(path)
                    results.append(f"📄 {relative}")
                    
        return results
        
    def _search_content(
        self,
        path: Path,
        query: str,
        extensions: Optional[List[str]],
        max_results: int
    ) -> List[str]:
        """Search files by content."""
        results = []
        
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            # Treat as literal string if not valid regex
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            
        for root, dirs, files in os.walk(path):
            # Skip hidden and common directories
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in self.SKIP_DIRS]
            
            for filename in files:
                if len(results) >= max_results:
                    return results
                    
                filepath = Path(root) / filename
                
                # Skip binary files
                if filepath.suffix.lower() in self.BINARY_EXTENSIONS:
                    continue
                    
                # Check extension filter
                if extensions:
                    if filepath.suffix.lower() not in [e.lower() for e in extensions]:
                        continue
                        
                # Search file content
                try:
                    content = filepath.read_text(encoding="utf-8", errors="ignore")
                    matches = list(pattern.finditer(content))
                    
                    if matches:
                        relative = filepath.relative_to(path)
                        lines = content.split("\n")
                        
                        # Find matching line numbers
                        match_lines = set()
                        for match in matches[:3]:  # Show first 3 matches
                            pos = match.start()
                            line_num = content[:pos].count("\n") + 1
                            match_lines.add(line_num)
                            
                        line_info = ", ".join(f"L{ln}" for ln in sorted(match_lines))
                        results.append(f"📄 {relative} ({line_info})")
                        
                except (PermissionError, UnicodeDecodeError, OSError):
                    continue
                    
        return results
        
    def get_file_info(self, path: str) -> str:
        """
        Get detailed information about a file or directory.
        
        Args:
            path: Path to examine.
            
        Returns:
            Formatted file/directory information.
        """
        file_path = self._validate_path(path)
        
        if not file_path.exists():
            raise FileSystemError(f"Path does not exist: {path}")
            
        stat = file_path.stat()
        
        info = []
        info.append(f"Path: {file_path}")
        info.append(f"Type: {'Directory' if file_path.is_dir() else 'File'}")
        
        if not file_path.is_dir():
            info.append(f"Size: {self._format_size(stat.st_size)}")
            info.append(f"Extension: {file_path.suffix or '(none)'}")
            
        info.append(f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        info.append(f"Created: {datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}")
        
        if file_path.is_dir():
            try:
                count = sum(1 for _ in file_path.iterdir())
                info.append(f"Items: {count}")
            except PermissionError:
                info.append("Items: (access denied)")
                
        return "\n".join(info)
        
    @staticmethod
    def _format_size(size: int) -> str:
        """Format file size in human-readable format."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# Global instance
_fs: Optional[FileSystem] = None


def get_file_system() -> FileSystem:
    """Get or create the global FileSystem instance."""
    global _fs
    if _fs is None:
        _fs = FileSystem()
    return _fs
