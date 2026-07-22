"""terminal_mode のスラッシュコマンド処理群

`TerminalMode` から挙動不変で切り出したコマンドハンドラ（web_search /
docs_ingest / work_intake）とメール添付処理群を `TerminalCommandsMixin` に
まとめる。各メソッドの本文・self 依存・例外処理・戻り値は移設前と完全に同一で、
`TerminalMode` が本 Mixin を継承することで従来どおり self.<method> で呼び出せる。
"""

from .command_handlers import TerminalCommandsMixin

__all__ = ["TerminalCommandsMixin"]
