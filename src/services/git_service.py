"""
Git Service - Local Git version control for user and project directories.

Provides Git operations for workspace directories without remote repository.
"""

import logging
import subprocess
import os
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class GitServiceError(Exception):
    """Git operation error"""
    pass


class GitService:
    """
    Service for local Git operations.
    
    Manages Git repositories within workspace directories for version control.
    No remote repository is used - purely local versioning.
    """
    
    @staticmethod
    def _run_git_command(
        repo_path: Path,
        args: List[str],
        check: bool = True,
        capture_output: bool = True
    ) -> subprocess.CompletedProcess:
        """
        Run a git command in the specified repository.
        
        Args:
            repo_path: Path to the git repository
            args: Git command arguments (without 'git' prefix)
            check: Raise exception on non-zero exit code
            capture_output: Capture stdout/stderr
            
        Returns:
            CompletedProcess result
            
        Raises:
            GitServiceError: If command fails and check=True
        """
        cmd = ["git"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=capture_output,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )
            if check and result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else f"Exit code: {result.returncode}"
                raise GitServiceError(f"Git command failed: {' '.join(args)}\n{error_msg}")
            return result
        except subprocess.TimeoutExpired:
            raise GitServiceError(f"Git command timed out: {' '.join(args)}")
        except FileNotFoundError:
            raise GitServiceError("Git is not installed or not in PATH")
        except Exception as e:
            raise GitServiceError(f"Git command error: {e}")
    
    @staticmethod
    def is_git_available() -> bool:
        """Check if git command is available."""
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    @staticmethod
    def is_repository(path: Path) -> bool:
        """Check if path is a git repository."""
        git_dir = path / ".git"
        return git_dir.exists() and git_dir.is_dir()
    
    @classmethod
    def init_repository(cls, path: Path, initial_commit: bool = True) -> bool:
        """
        Initialize a new git repository.
        
        Args:
            path: Directory to initialize
            initial_commit: Create initial commit with .gitkeep
            
        Returns:
            True if successful
            
        Raises:
            GitServiceError: If initialization fails
        """
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        
        if cls.is_repository(path):
            logger.debug(f"Repository already exists at {path}")
            return True
        
        # Initialize repository
        cls._run_git_command(path, ["init"])
        logger.info(f"Initialized git repository at {path}")
        
        # Configure user for this repository
        cls._run_git_command(path, ["config", "user.email", "aoitalk@local"])
        cls._run_git_command(path, ["config", "user.name", "AoiTalk"])
        
        if initial_commit:
            # Create .gitkeep to have an initial commit
            gitkeep = path / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text("# Git repository initialized by AoiTalk\n", encoding="utf-8")
            
            cls._run_git_command(path, ["add", "."])
            cls._run_git_command(path, ["commit", "-m", "初期化: リポジトリ作成"])
        
        return True
    
    @classmethod
    def get_status(cls, path: Path) -> Dict:
        """
        Get repository status.
        
        Args:
            path: Repository path
            
        Returns:
            Dict with:
                - is_repo: bool
                - has_changes: bool
                - staged: List of staged files
                - modified: List of modified files
                - untracked: List of untracked files
        """
        if not cls.is_repository(path):
            return {
                "is_repo": False,
                "has_changes": False,
                "staged": [],
                "modified": [],
                "untracked": []
            }
        
        try:
            result = cls._run_git_command(path, ["status", "--porcelain"])
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            
            staged = []
            modified = []
            untracked = []
            
            for line in lines:
                if len(line) < 3:
                    continue
                status = line[:2]
                filename = line[3:]
                
                if status[0] in "MADRC":
                    staged.append(filename)
                if status[1] == "M":
                    modified.append(filename)
                elif status[1] == "D":
                    modified.append(filename)
                if status == "??":
                    untracked.append(filename)
            
            return {
                "is_repo": True,
                "has_changes": len(staged) > 0 or len(modified) > 0 or len(untracked) > 0,
                "staged": staged,
                "modified": modified,
                "untracked": untracked
            }
        except GitServiceError as e:
            logger.error(f"Failed to get status: {e}")
            return {
                "is_repo": True,
                "has_changes": False,
                "staged": [],
                "modified": [],
                "untracked": [],
                "error": str(e)
            }
    
    @classmethod
    def commit_all(cls, path: Path, message: str) -> Optional[str]:
        """
        Stage all changes and commit.
        
        Args:
            path: Repository path
            message: Commit message
            
        Returns:
            Commit hash if successful, None otherwise
        """
        if not cls.is_repository(path):
            raise GitServiceError(f"Not a git repository: {path}")
        
        # Stage all changes
        cls._run_git_command(path, ["add", "-A"])
        
        # Check if there are changes to commit
        status = cls.get_status(path)
        if not status.get("staged") and not status.get("has_changes"):
            logger.info("No changes to commit")
            return None
        
        # Commit
        cls._run_git_command(path, ["commit", "-m", message])
        
        # Get commit hash
        result = cls._run_git_command(path, ["rev-parse", "HEAD"])
        commit_hash = result.stdout.strip()
        
        logger.info(f"Committed changes: {commit_hash[:8]} - {message}")
        return commit_hash
    
    @classmethod
    def get_log(cls, path: Path, limit: int = 20) -> List[Dict]:
        """
        Get commit history.
        
        Args:
            path: Repository path
            limit: Maximum number of commits to return
            
        Returns:
            List of commit dicts with hash, message, author, date
        """
        if not cls.is_repository(path):
            return []
        
        try:
            # Format: hash|author|date|subject
            format_str = "%H|%an|%ai|%s"
            result = cls._run_git_command(
                path,
                ["log", f"-{limit}", f"--format={format_str}"]
            )
            
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append({
                        "hash": parts[0],
                        "hash_short": parts[0][:8],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3]
                    })
            
            return commits
        except GitServiceError as e:
            logger.error(f"Failed to get log: {e}")
            return []
    
    @classmethod
    def get_diff(cls, path: Path, commit_hash: Optional[str] = None) -> str:
        """
        Get diff for uncommitted changes or specific commit.
        
        Args:
            path: Repository path
            commit_hash: If provided, show diff for that commit
            
        Returns:
            Diff text
        """
        if not cls.is_repository(path):
            return ""
        
        try:
            if commit_hash:
                # Diff for specific commit
                result = cls._run_git_command(
                    path,
                    ["show", commit_hash, "--format=", "--stat", "-p"]
                )
            else:
                # Diff for uncommitted changes
                result = cls._run_git_command(path, ["diff", "HEAD"])
                if not result.stdout.strip():
                    # Also include untracked files in diff
                    result = cls._run_git_command(path, ["diff"])
            
            return result.stdout
        except GitServiceError as e:
            logger.error(f"Failed to get diff: {e}")
            return ""
    
    @classmethod
    def get_file_history(cls, path: Path, file_path: str, limit: int = 10) -> List[Dict]:
        """
        Get commit history for a specific file.
        
        Args:
            path: Repository path
            file_path: Relative path to file
            limit: Maximum commits
            
        Returns:
            List of commits affecting the file
        """
        if not cls.is_repository(path):
            return []
        
        try:
            format_str = "%H|%an|%ai|%s"
            result = cls._run_git_command(
                path,
                ["log", f"-{limit}", f"--format={format_str}", "--", file_path]
            )
            
            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append({
                        "hash": parts[0],
                        "hash_short": parts[0][:8],
                        "author": parts[1],
                        "date": parts[2],
                        "message": parts[3]
                    })
            
            return commits
        except GitServiceError as e:
            logger.error(f"Failed to get file history: {e}")
            return []


# ── Workspace Integration ─────────────────────────────────────────────────

def get_workspaces_root() -> Path:
    """Get the workspaces root directory."""
    import os
    project_root = Path(os.environ.get("AOITALK_PROJECT_ROOT", ".")).resolve()
    workspaces_dir = os.environ.get(
        "AOITALK_WORKSPACES_DIR",
        str(project_root / "workspaces")
    )
    return Path(workspaces_dir)


def get_user_directory(user_id: str) -> Path:
    """Get user's workspace directory path."""
    return get_workspaces_root() / "_users" / f"user_{user_id}"


def get_project_directory(project_id: str) -> Path:
    """Get project's workspace directory path."""
    return get_workspaces_root() / "_projects" / f"project_{project_id}"


def ensure_user_git_repository(user_id: str) -> bool:
    """
    Ensure user directory has a git repository.
    Creates directory and initializes git if needed.
    
    Args:
        user_id: User UUID string
        
    Returns:
        True if repository exists/created successfully
    """
    user_dir = get_user_directory(user_id)
    try:
        return GitService.init_repository(user_dir)
    except GitServiceError as e:
        logger.error(f"Failed to initialize git for user {user_id}: {e}")
        return False


def ensure_project_git_repository(project_id: str) -> bool:
    """
    Ensure project directory has a git repository.
    Creates directory and initializes git if needed.
    
    Args:
        project_id: Project UUID string
        
    Returns:
        True if repository exists/created successfully
    """
    project_dir = get_project_directory(project_id)
    try:
        return GitService.init_repository(project_dir)
    except GitServiceError as e:
        logger.error(f"Failed to initialize git for project {project_id}: {e}")
        return False
