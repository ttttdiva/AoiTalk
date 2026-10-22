"""
LLM Function Tools for Repository Maps

Provides function tools for generating repository structure maps.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import tool

from .repo_map import (
    SKIP_DIRS,
    SOURCE_EXTENSIONS,
    _is_link_or_reparse,
    get_repo_map_instance,
    repo_map_read_authorizer,
)

logger = logging.getLogger(__name__)


def _repo_map_path_is_within(path: Path, root: Path) -> bool:
    """Return component-aware containment for one RepoMap descendant.

    ``RepoMap`` works with absolute paths and receives paths from ``os.walk``.
    Keep the final check here rather than relying on string prefixes; on
    Windows ``normcase`` also makes the comparison case-insensitive.
    """

    try:
        path_key = os.path.normcase(os.path.normpath(os.fspath(path)))
        root_key = os.path.normcase(os.path.normpath(os.fspath(root)))
        return os.path.commonpath((path_key, root_key)) == root_key
    except (OSError, ValueError):
        return False


def _active_private_agent_read_root() -> tuple[Optional[Path], Optional[str]]:
    """Return the bound non-native AgentDialogue read root, if present."""

    try:
        from ..os_operations.tools import _agent_dialogue_read_root
    except ImportError:
        # Stripped embeddings without AgentDialogue support retain the legacy
        # Personal RepoMap lane.
        return None, None
    try:
        return _agent_dialogue_read_root()
    except Exception as exc:  # pragma: no cover - defensive boundary guard
        return None, f"Agent workspace boundary cannot be inspected: {exc}"


def _resolve_private_agent_read_path(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve one RepoMap root through a bound private Agent workspace."""

    private_root, boundary_error = _active_private_agent_read_root()
    if boundary_error:
        return None, boundary_error
    if private_root is None:
        return None, None

    try:
        from ..os_operations.tools import _resolve_read_target
        from ..file_explorer.file_explorer_service import (
            is_safe_workspace_path,
        )

        resolved, _is_absolute, error = _resolve_read_target(raw)
        if error or not resolved:
            return None, error or "Agent workspace path cannot be resolved"
        candidate = Path(resolved).resolve(strict=False)
        if not is_safe_workspace_path(private_root, candidate):
            return None, "Path is outside the Agent workspace"
        if not _repo_map_path_is_within(candidate, private_root):
            return None, "Path is outside the Agent workspace"
        return str(candidate), None
    except (OSError, RuntimeError, TypeError, ValueError, PermissionError) as exc:
        return None, f"Agent workspace path cannot be resolved: {exc}"


def _resolve_repo_map_path(path: str) -> str:
    """Resolve a RepoMap root through the active execution boundary.

    RepoMap predates the run-scoped and user/project workspace contracts and
    performs its own ``Path.resolve``/``os.walk`` internally.  Keep that
    implementation unchanged, but never let it choose a root before the
    caller's authority has been checked:

    * an active ``AgentRunScope`` is authoritative and supplies the canonical
      path directly;
    * an unscoped Enterprise call uses the canonical OS-tools read resolver,
      which applies shared-service identity and user/project ACL checks; and
    * unscoped Personal calls retain their historical path semantics.

    Imports are intentionally local.  ``os_operations`` imports the workspace
    service and the workspace service imports other tools, so importing it at
    module load would recreate a circular dependency during tool discovery.
    """

    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("repo map path must be a string or path-like value")
    try:
        raw = os.fspath(path)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError("repo map path cannot be resolved") from exc
    if isinstance(raw, bytes):
        try:
            raw = os.fsdecode(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("repo map path cannot be resolved") from exc

    try:
        from ...security.agent_run_scope import AgentRunScope, get_current_run_scope
        from ...security.harness_execution_scope import (
            get_current_harness_execution_scope,
        )
    except Exception as exc:  # pragma: no cover - stripped-build safeguard
        # A missing scope implementation means an Enterprise call cannot
        # establish its repository boundary.  Personal mode can still retain
        # the historical path behavior below.
        try:
            from ...features import Features

            if Features.is_enterprise():
                raise RuntimeError(
                    "AgentRunScope is unavailable in Enterprise"
                ) from exc
        except Exception:
            raise RuntimeError("execution boundary cannot be inspected") from exc
        return raw

    scope = get_current_run_scope()
    if scope is None:
        # A trusted HarnessExecutionScope is an upper capability.  Adapt it
        # to the repository-specific read contract rather than falling back
        # to Enterprise's shared-identity ACL lane when a lower scope has not
        # yet been bound by the caller.
        upper_scope = get_current_harness_execution_scope()
        if upper_scope is not None:
            try:
                scope = upper_scope.to_agent_run_scope()
            except Exception as exc:
                raise ValueError("invalid HarnessExecutionScope") from exc
    if scope is not None:
        if not isinstance(scope, AgentRunScope):
            raise ValueError("invalid AgentRunScope")
        try:
            # ``assert_read_allowed`` canonicalises existing links/reparse
            # components and rejects roots outside this run's read scope.
            return str(scope.assert_read_allowed(raw))
        except Exception as exc:
            raise ValueError(f"run-scope denied repo map path: {exc}") from exc

    private_path, private_error = _resolve_private_agent_read_path(raw)
    if private_error:
        raise ValueError(f"Agent workspace denied repo map path: {private_error}")
    if private_path is not None:
        return private_path

    try:
        from ...features import Features

        enterprise = Features.is_enterprise()
    except Exception as exc:  # fail closed if the profile cannot be read
        raise RuntimeError("execution profile cannot be inspected") from exc

    if not enterprise:
        # Keep the legacy input untouched.  ``get_repo_map_instance`` performs
        # the same Path.resolve it always did, preserving relative-path and
        # singleton behavior for Personal callers.
        return raw

    try:
        from ..os_operations.tools import (
            _native_employee_capability,
            _resolve_read_target,
        )

        resolved, _is_absolute, error = _resolve_read_target(
            raw,
            allow_native_employee=_native_employee_capability() is not None,
        )
    except Exception as exc:
        raise ValueError("Enterprise repo map path cannot be authorized") from exc
    if error or not resolved:
        raise ValueError(error or "Enterprise repo map path cannot be authorized")

    try:
        # The OS-tools resolver is the ACL authority.  Canonicalise once more
        # before handing the root to RepoMap so its cache key and os.walk root
        # cannot retain an alias returned by a compatibility resolver.
        return str(Path(resolved).resolve(strict=False))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ValueError("Enterprise repo map path cannot be canonicalized") from exc


def _authorize_repo_map_descendant(
    candidate: Path,
    root: Path,
    *,
    scope: Any = None,
    enterprise: bool = False,
    private_root: Path | None = None,
) -> Optional[str]:
    """Authorize one source file before RepoMap is allowed to read it.

    ``RepoMap._find_source_files`` predates the execution-boundary contract
    and performs an unrestricted ``os.walk``.  Constrained callers therefore
    use this helper for every descendant and pass the resulting canonical
    file list as ``other_files``.  Personal callers without an active scope do
    not use this path and retain the historical scanner unchanged.
    """

    try:
        if scope is not None:
            # The run scope is the authority for coding/worker calls.  It also
            # rejects escaping symlink/junction/reparse components.
            authorised = Path(scope.assert_read_allowed(str(candidate)))
        elif private_root is not None:
            # Bound non-native AgentDialogue turns must use the same private
            # read resolver as the root selection.  Without this branch a
            # descendant callback could re-enter RepoMap's Personal fallback.
            from ..os_operations.tools import _resolve_read_target

            resolved, _is_absolute, error = _resolve_read_target(str(candidate))
            if error or not resolved:
                return None
            authorised = Path(resolved)
        elif enterprise:
            # Ordinary Enterprise reads must use the same user/workspace ACL
            # resolver as read_file/list_directory/search_files.  Importing
            # lazily avoids a repo_map <-> os_operations cycle at discovery.
            from ..os_operations.tools import (
                _native_employee_capability,
                _resolve_read_target,
            )

            resolved, _is_absolute, error = _resolve_read_target(
                str(candidate),
                allow_native_employee=(
                    _native_employee_capability() is not None
                ),
            )
            if error or not resolved:
                return None
            authorised = Path(resolved)
        else:  # pragma: no cover - defensive; unconstrained Personal bypasses
            authorised = candidate

        # Always canonicalise the path that RepoMap will open.  This means an
        # in-workspace file symlink is read through its target, while an
        # external target is excluded even if a lexical path looked safe.
        canonical = Path(os.path.realpath(os.path.abspath(os.fspath(authorised))))
    except (OSError, RuntimeError, TypeError, ValueError, PermissionError):
        return None

    if not _repo_map_path_is_within(canonical, root):
        return None
    return str(canonical)


def _collect_authorized_repo_files(
    root: Path,
    *,
    private_root: Path | None = None,
) -> Optional[List[str]]:
    """Enumerate only descendants safe for a scoped/Enterprise RepoMap call.

    A ``None`` result means an unscoped Personal call, for which the legacy
    ``RepoMap`` scanner must remain untouched.  In all constrained modes,
    symlink/junction/reparse directories are pruned and every source file is
    re-authorized before being handed to RepoMap.
    """

    scope, enterprise = _repo_map_boundary_context()
    if scope is None and private_root is None:
        private_root, private_error = _active_private_agent_read_root()
        if private_error:
            raise ValueError(private_error)

    if private_root is not None and not _repo_map_path_is_within(root, private_root):
        raise ValueError("RepoMap root is outside the Agent workspace")

    if scope is None and not enterprise and private_root is None:
        # Personal mode has no new boundary and must preserve RepoMap's
        # historical os.walk behavior, including its singleton/cache path.
        return None
    files: List[str] = []
    seen: set[str] = set()
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current, dirs, filenames in walker:
            current_path = Path(current)
            # ``followlinks=False`` does not cover Windows junctions/reparse
            # points.  Prune every unsafe directory before os.walk descends.
            safe_dirs: list[str] = []
            for dirname in dirs:
                if dirname in SKIP_DIRS or dirname.startswith("."):
                    continue
                child_dir = current_path / dirname
                if _is_link_or_reparse(child_dir):
                    continue
                safe_dirs.append(dirname)
            dirs[:] = safe_dirs

            for filename in filenames:
                if Path(filename).suffix.lower() not in SOURCE_EXTENSIONS:
                    continue
                candidate = current_path / filename
                authorised = _authorize_repo_map_descendant(
                    candidate,
                    root,
                    scope=scope,
                    enterprise=enterprise,
                    private_root=private_root,
                )
                if not authorised:
                    continue
                key = os.path.normcase(os.path.normpath(authorised))
                if key in seen:
                    continue
                seen.add(key)
                files.append(authorised)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        # The root was already authorized, but a disappearing/replaced child
        # must never make RepoMap fall back to its unrestricted scanner.
        logger.warning("RepoMap descendant scan skipped unsafe path: %s", exc)
    return files


def _repo_map_boundary_context() -> tuple[Any, bool]:
    """Return the active run scope and current profile for one invocation."""

    try:
        from ...security.agent_run_scope import get_current_run_scope

        scope = get_current_run_scope()
    except Exception:
        scope = None
    if scope is None:
        try:
            from ...security.harness_execution_scope import (
                get_current_harness_execution_scope,
            )

            upper_scope = get_current_harness_execution_scope()
        except Exception:
            upper_scope = None
        if upper_scope is not None:
            try:
                scope = upper_scope.to_agent_run_scope()
            except Exception as exc:
                raise RuntimeError("invalid HarnessExecutionScope") from exc
    try:
        from ...features import Features

        enterprise = Features.is_enterprise()
    except Exception as exc:
        if scope is None:
            raise RuntimeError("execution profile cannot be inspected") from exc
        enterprise = False
    return scope, enterprise


@tool
def get_repo_map(
    path: str,
    max_tokens: int = 4096,
    exclude_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """リポジトリの構造マップを生成する
    
    コードベースの構造を効率的に把握するためのマップを返します。
    tree-sitterでコードを解析し、重要な定義（関数、クラス）と
    その参照関係を抽出してランキングします。
    
    Args:
        path: リポジトリのルートパス
        max_tokens: 出力の最大トークン数（デフォルト: 4096）
        exclude_files: 除外するファイルのリスト（チャットに含まれているファイル等）
    
    Returns:
        Dict[str, Any]: リポジトリマップと結果情報
    
    Examples:
        >>> get_repo_map(".")
        >>> get_repo_map("src", max_tokens=2048)
        >>> get_repo_map(".", exclude_files=["main.py", "config.py"])
    """
    print(f"[Tool] get_repo_map が呼び出されました: {path}")
    
    try:
        authorized_path = _resolve_repo_map_path(path)
        root = Path(authorized_path)
        scope, enterprise = _repo_map_boundary_context()
        private_root = None
        if scope is None:
            private_root, private_error = _active_private_agent_read_root()
            if private_error:
                raise ValueError(private_error)
            if private_root is not None and not _repo_map_path_is_within(
                root, private_root
            ):
                raise ValueError("RepoMap root is outside the Agent workspace")

        rm = get_repo_map_instance(authorized_path)
        if private_root is not None:
            expected_root = root.resolve(strict=False)
            try:
                actual_root = Path(getattr(rm, "root")).resolve(strict=False)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
                raise ValueError("RepoMap instance root is unavailable") from exc
            expected_key = os.path.normcase(os.path.normpath(str(expected_root)))
            actual_key = os.path.normcase(os.path.normpath(str(actual_root)))
            if actual_key != expected_key:
                raise ValueError("RepoMap instance root differs from Agent workspace root")

        # The legacy RepoMap scanner is intentionally bypassed whenever an
        # explicit AgentRunScope or Enterprise workspace boundary is active.
        # Every descendant is then canonicalized and authorized before the
        # parser can open it; an unsafe child is omitted rather than causing a
        # fallback to unrestricted ``os.walk``.
        rm.max_tokens = max_tokens
        authorized_files = _collect_authorized_repo_files(
            root,
            private_root=private_root,
        )

        if authorized_files is None:
            # Unscoped Personal mode deliberately retains RepoMap's original
            # ``other_files=None`` path and scanner semantics.
            authorizer = None
        else:
            # Re-authorize again immediately before each mtime/open operation.
            # The callback is request-local and does not mutate the singleton.
            authorizer = lambda candidate: _authorize_repo_map_descendant(
                Path(candidate),
                root,
                scope=scope,
                enterprise=enterprise,
                private_root=private_root,
            )

        with repo_map_read_authorizer(authorizer):
            repo_map = rm.get_repo_map(
                chat_files=exclude_files,
                other_files=authorized_files,
                force_refresh=False
            )
        
        if repo_map:
            return {
                "success": True,
                "repo_map": repo_map,
                "root": str(rm.root),
                "token_estimate": len(repo_map) // 4  # rough estimate
            }
        else:
            return {
                "success": True,
                "repo_map": "(empty repository or no source files found)",
                "root": str(rm.root),
                "token_estimate": 0
            }
            
    except Exception as e:
        logger.error(f"Error generating repo map: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"リポジトリマップの生成に失敗しました: {str(e)}"
        }
