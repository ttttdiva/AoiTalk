"""
Repository Map package for AoiTalk

Provides repository structure analysis and visualization:
- Tree-sitter based code parsing for definitions/references
- PageRank ranking for file relevance
- Compressed tree representation for LLM context

Based on Aider's repomap implementation.
"""

from .repo_map import RepoMap, get_repo_map_instance
from .tools import get_repo_map

__all__ = [
    'RepoMap',
    'get_repo_map_instance',
    'get_repo_map',
]
