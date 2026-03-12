"""
Repository Map for AoiTalk

Generates a concise map of repository structure for LLM context.
Uses tree-sitter to parse code and extract definitions/references,
then ranks files using PageRank based on inter-file dependencies.

Based on Aider's repomap.py implementation.
"""

import logging
import os
import warnings
from collections import Counter, defaultdict, namedtuple
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Suppress tree-sitter FutureWarning
warnings.simplefilter("ignore", category=FutureWarning)

# Lazy imports for optional heavy dependencies
_grep_ast_available = None
_networkx_available = None


def _check_grep_ast():
    global _grep_ast_available
    if _grep_ast_available is None:
        try:
            from grep_ast import TreeContext, filename_to_lang
            from grep_ast.tsl import get_language, get_parser
            _grep_ast_available = True
        except ImportError:
            _grep_ast_available = False
            logger.warning("grep-ast not available. Install with: pip install grep-ast tree-sitter-language-pack")
    return _grep_ast_available


def _check_networkx():
    global _networkx_available
    if _networkx_available is None:
        try:
            import networkx
            _networkx_available = True
        except ImportError:
            _networkx_available = False
            logger.warning("networkx not available. Install with: pip install networkx")
    return _networkx_available


# Tag represents a code symbol (definition or reference)
Tag = namedtuple("Tag", ["rel_fname", "fname", "line", "name", "kind"])


# Directories to skip when scanning
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".bzr",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", ".vs",
    "dist", "build", "target", "out",
    ".tox", ".nox", ".pytest_cache",
    "vendor", "third_party",
}

# File extensions to include
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".scala",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb",
    ".php", ".swift", ".m", ".mm",
    ".lua", ".r", ".jl",
    ".sh", ".bash", ".zsh",
    ".yaml", ".yml", ".json", ".toml",
    ".md", ".rst", ".txt",
}


class RepoMap:
    """
    Repository map generator for LLM context.
    
    Analyzes code files to extract definitions/references,
    ranks them by relevance using PageRank, and generates
    a compressed tree representation.
    """
    
    CACHE_VERSION = 1
    
    def __init__(
        self,
        root: str = ".",
        max_tokens: int = 4096,
        verbose: bool = False,
    ):
        """
        Initialize the RepoMap.
        
        Args:
            root: Root directory of the repository.
            max_tokens: Maximum tokens for the output map.
            verbose: Enable verbose logging.
        """
        self.root = Path(root).resolve()
        self.max_tokens = max_tokens
        self.verbose = verbose
        
        # Caches
        self._tags_cache: Dict[str, Dict] = {}
        self._tree_cache: Dict[tuple, str] = {}
        
        # Check dependencies
        self._has_grep_ast = _check_grep_ast()
        self._has_networkx = _check_networkx()
        
    def get_repo_map(
        self,
        chat_files: Optional[List[str]] = None,
        other_files: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> str:
        """
        Generate a repository map.
        
        Args:
            chat_files: Files currently in chat context (excluded from map).
            other_files: Additional files to include. If None, scans repository.
            force_refresh: Force regeneration ignoring cache.
            
        Returns:
            String representation of the repository structure.
        """
        if not self._has_grep_ast:
            return self._generate_simple_map(chat_files, other_files)
            
        chat_files = set(chat_files or [])
        
        if other_files is None:
            other_files = self._find_source_files()
        other_files = [f for f in other_files if f not in chat_files]
        
        if not other_files:
            return ""
            
        # Get ranked tags
        ranked_tags = self._get_ranked_tags(chat_files, other_files)
        
        if not ranked_tags:
            return self._generate_simple_map(chat_files, other_files)
            
        # Convert to tree representation
        tree = self._to_tree(ranked_tags, chat_files)
        
        # Truncate if needed
        if self.max_tokens and len(tree) > self.max_tokens * 4:  # rough char estimate
            lines = tree.split("\n")
            tree = "\n".join(lines[:self.max_tokens // 2]) + "\n... (truncated)"
            
        return tree
        
    def _find_source_files(self) -> List[str]:
        """Find all source files in the repository."""
        files = []
        
        for root_dir, dirs, filenames in os.walk(self.root):
            # Filter out skip directories
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            
            for filename in filenames:
                ext = Path(filename).suffix.lower()
                if ext in SOURCE_EXTENSIONS:
                    filepath = Path(root_dir) / filename
                    files.append(str(filepath))
                    
        return files
        
    def _get_rel_fname(self, fname: str) -> str:
        """Get relative path from root."""
        try:
            return str(Path(fname).relative_to(self.root))
        except ValueError:
            return fname
            
    def _get_tags(self, fname: str, rel_fname: str) -> List[Tag]:
        """Extract tags (definitions/references) from a file."""
        if not self._has_grep_ast:
            return []
            
        # Check cache
        mtime = self._get_mtime(fname)
        if mtime is None:
            return []
            
        cache_key = fname
        if cache_key in self._tags_cache:
            cached = self._tags_cache[cache_key]
            if cached.get("mtime") == mtime:
                return cached.get("data", [])
                
        # Parse file
        tags = list(self._extract_tags_raw(fname, rel_fname))
        
        # Cache result
        self._tags_cache[cache_key] = {"mtime": mtime, "data": tags}
        
        return tags
        
    def _extract_tags_raw(self, fname: str, rel_fname: str):
        """Extract tags using tree-sitter."""
        from grep_ast import filename_to_lang
        from grep_ast.tsl import get_language, get_parser
        
        lang = filename_to_lang(fname)
        if not lang:
            return
            
        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception as e:
            if self.verbose:
                logger.debug(f"Skipping {fname}: {e}")
            return
            
        # Read file
        try:
            with open(fname, 'r', encoding='utf-8', errors='replace') as f:
                code = f.read()
        except Exception:
            return
            
        if not code:
            return
            
        # Parse
        tree = parser.parse(bytes(code, "utf-8"))
        
        # Query for definitions and references
        query_scm = self._get_query_scm(lang)
        if not query_scm:
            return
            
        try:
            query = language.query(query_scm)
            captures = query.captures(tree.root_node)
        except Exception:
            return
            
        # Process captures
        if isinstance(captures, dict):
            all_nodes = []
            for tag, nodes in captures.items():
                all_nodes.extend((node, tag) for node in nodes)
        else:
            all_nodes = list(captures)
            
        for node, tag in all_nodes:
            if tag.startswith("name.definition."):
                kind = "def"
            elif tag.startswith("name.reference."):
                kind = "ref"
            else:
                continue
                
            yield Tag(
                rel_fname=rel_fname,
                fname=fname,
                name=node.text.decode("utf-8"),
                kind=kind,
                line=node.start_point[0],
            )
            
    def _get_query_scm(self, lang: str) -> Optional[str]:
        """Get tree-sitter query for a language."""
        try:
            # Try to find query file in local queries directory
            queries_dir = Path(__file__).parent / "queries"
            
            # Check both tree-sitter-language-pack and tree-sitter-languages
            for subdir in ["tree-sitter-language-pack", "tree-sitter-languages"]:
                query_file = queries_dir / subdir / f"{lang}-tags.scm"
                if query_file.exists():
                    return query_file.read_text(encoding="utf-8")
                
            # Fallback: basic definition query for Python
            return """
            (function_definition name: (identifier) @name.definition.function)
            (class_definition name: (identifier) @name.definition.class)
            """
        except Exception:
            return None
            
    def _get_mtime(self, fname: str) -> Optional[float]:
        """Get file modification time."""
        try:
            return os.path.getmtime(fname)
        except (OSError, FileNotFoundError):
            return None
            
    def _get_ranked_tags(
        self,
        chat_files: Set[str],
        other_files: List[str],
    ) -> List[Tag]:
        """Rank tags using PageRank."""
        if not self._has_networkx:
            # Fallback: just return all tags
            all_tags = []
            for fname in other_files:
                rel_fname = self._get_rel_fname(fname)
                all_tags.extend(self._get_tags(fname, rel_fname))
            return all_tags
            
        import networkx as nx
        
        defines = defaultdict(set)  # name -> set of files that define it
        references = defaultdict(list)  # name -> list of files that reference it
        definitions = defaultdict(set)  # (file, name) -> set of Tag objects
        
        chat_rel_fnames = set(self._get_rel_fname(f) for f in chat_files)
        
        # Collect all tags
        for fname in other_files:
            rel_fname = self._get_rel_fname(fname)
            tags = self._get_tags(fname, rel_fname)
            
            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    definitions[(rel_fname, tag.name)].add(tag)
                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)
                    
        # Build graph
        G = nx.MultiDiGraph()
        
        idents = set(defines.keys()) & set(references.keys())
        
        for ident in idents:
            definers = defines[ident]
            for referencer, count in Counter(references[ident]).items():
                for definer in definers:
                    G.add_edge(referencer, definer, weight=count, ident=ident)
                    
        if not G.nodes():
            return []
            
        # Run PageRank
        try:
            ranked = nx.pagerank(G, weight="weight")
        except Exception:
            ranked = {node: 1.0 for node in G.nodes()}
            
        # Distribute rank to definitions
        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            src_rank = ranked.get(src, 0)
            out_edges = list(G.out_edges(src, data=True))
            if not out_edges:
                continue
            total_weight = sum(d["weight"] for _, _, d in out_edges)
            for _, dst, data in out_edges:
                ident = data["ident"]
                ranked_definitions[(dst, ident)] += src_rank * data["weight"] / total_weight
                
        # Sort and collect tags
        sorted_defs = sorted(ranked_definitions.items(), key=lambda x: -x[1])
        
        ranked_tags = []
        for (fname, ident), rank in sorted_defs:
            if fname in chat_rel_fnames:
                continue
            ranked_tags.extend(definitions.get((fname, ident), []))
            
        # Add files without tags
        tagged_files = set(tag.rel_fname for tag in ranked_tags)
        for fname in other_files:
            rel_fname = self._get_rel_fname(fname)
            if rel_fname not in tagged_files and rel_fname not in chat_rel_fnames:
                ranked_tags.append(Tag(rel_fname, fname, 0, "", "file"))
                
        return ranked_tags
        
    def _to_tree(self, tags: List[Tag], chat_files: Set[str]) -> str:
        """Convert ranked tags to tree representation."""
        if not tags:
            return ""
            
        chat_rel_fnames = set(self._get_rel_fname(f) for f in chat_files)
        
        output_lines = []
        current_file = None
        file_lines = []
        
        for tag in tags:
            if tag.rel_fname in chat_rel_fnames:
                continue
                
            if tag.rel_fname != current_file:
                # Output previous file
                if current_file and file_lines:
                    output_lines.append(f"\n{current_file}:")
                    output_lines.extend(file_lines)
                elif current_file:
                    output_lines.append(f"\n{current_file}")
                    
                current_file = tag.rel_fname
                file_lines = []
                
            if tag.kind in ("def", "ref") and tag.name:
                file_lines.append(f"  │{tag.kind}: {tag.name} (L{tag.line + 1})")
                
        # Output last file
        if current_file and file_lines:
            output_lines.append(f"\n{current_file}:")
            output_lines.extend(file_lines)
        elif current_file:
            output_lines.append(f"\n{current_file}")
            
        return "\n".join(output_lines)
        
    def _generate_simple_map(
        self,
        chat_files: Optional[List[str]],
        other_files: Optional[List[str]],
    ) -> str:
        """Generate a simple file listing when tree-sitter is not available."""
        if other_files is None:
            other_files = self._find_source_files()
            
        chat_files = set(chat_files or [])
        
        output = ["Repository structure:"]
        
        # Group by directory
        by_dir = defaultdict(list)
        for f in other_files:
            if f in chat_files:
                continue
            rel = self._get_rel_fname(f)
            parent = str(Path(rel).parent)
            by_dir[parent].append(Path(rel).name)
            
        for dir_path in sorted(by_dir.keys()):
            output.append(f"\n{dir_path}/")
            for fname in sorted(by_dir[dir_path])[:20]:  # Limit files per dir
                output.append(f"  {fname}")
            if len(by_dir[dir_path]) > 20:
                output.append(f"  ... and {len(by_dir[dir_path]) - 20} more files")
                
        return "\n".join(output)


# Global instance
_repo_map_instance: Optional[RepoMap] = None


def get_repo_map_instance(root: str = ".") -> RepoMap:
    """Get or create a RepoMap instance."""
    global _repo_map_instance
    if _repo_map_instance is None or str(_repo_map_instance.root) != str(Path(root).resolve()):
        _repo_map_instance = RepoMap(root=root)
    return _repo_map_instance
