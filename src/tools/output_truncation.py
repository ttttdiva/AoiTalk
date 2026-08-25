"""コマンド出力のトリム（中央省略）ユーティリティ

LLM へ返すコマンド出力は、そのまま渡すとコンテキストを破壊するため必ずトリムする。
単純な先頭 N 文字カットは実務で役に立たない（エラーやスタックトレースは末尾に出る）ため、
OpenAI Codex CLI と同様に **先頭と末尾を残して中央を省略する** 方式を採用する。

このモジュールはコマンド実行以外（長いファイル出力の要約など）からも使えるよう、
os_operations パッケージには依存しない独立実装にしている。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "TruncatedOutput",
    "truncate_middle",
    "format_command_output",
]


@dataclass
class TruncatedOutput:
    """トリム結果

    Attributes:
        text: トリム後のテキスト（トリムしていない場合は元テキストそのまま）
        original_bytes: 元テキストの UTF-8 バイト数
        original_lines: 元テキストの行数
        truncated: 実際にトリムしたかどうか
    """

    text: str
    original_bytes: int
    original_lines: int
    truncated: bool


def _byte_len(text: str) -> int:
    """UTF-8 でのバイト数を返す"""
    return len(text.encode("utf-8"))


def _cut_bytes(text: str, max_bytes: int, from_end: bool = False) -> str:
    """行境界で切れない場合のフォールバック。UTF-8 バイト単位で切り出す。

    マルチバイト文字の途中で切れた場合は errors="ignore" で捨てる。
    """
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    chunk = data[-max_bytes:] if from_end else data[:max_bytes]
    return chunk.decode("utf-8", errors="ignore")


def _take_head_lines(lines: List[str], budget: int) -> Tuple[List[str], int]:
    """先頭から予算バイト数に収まる行を取り出す"""
    taken: List[str] = []
    used = 0
    for line in lines:
        size = _byte_len(line)
        if used + size > budget:
            break
        taken.append(line)
        used += size
    return taken, used


def _take_tail_lines(lines: List[str], budget: int) -> Tuple[List[str], int]:
    """末尾から予算バイト数に収まる行を取り出す"""
    taken: List[str] = []
    used = 0
    for line in reversed(lines):
        size = _byte_len(line)
        if used + size > budget:
            break
        taken.insert(0, line)
        used += size
    return taken, used


def truncate_middle(text: str, max_bytes: int) -> TruncatedOutput:
    """テキストの中央を省略して max_bytes 程度に収める。

    先頭と末尾におよそ半分ずつ予算を割り当て、行境界で切る。
    省略した箇所には `\\n[... 中略: N 行 / M バイトを省略 ...]\\n` を挿入する。

    Args:
        text: 対象テキスト
        max_bytes: 許容する UTF-8 バイト数。0 以下ならトリムしない。

    Returns:
        TruncatedOutput: トリム結果
    """
    if text is None:
        text = ""

    original_bytes = _byte_len(text)
    lines = text.splitlines(keepends=True)
    original_lines = len(lines)

    if max_bytes <= 0 or original_bytes <= max_bytes:
        return TruncatedOutput(
            text=text,
            original_bytes=original_bytes,
            original_lines=original_lines,
            truncated=False,
        )

    head_budget = max_bytes // 2
    tail_budget = max_bytes - head_budget

    head_lines, head_bytes = _take_head_lines(lines, head_budget)
    tail_lines, tail_bytes = _take_tail_lines(lines, tail_budget)

    # 行数が少ない（＝1 行が極端に長い）場合は行境界で切れないのでバイト単位で切る
    if not head_lines and not tail_lines:
        head_text = _cut_bytes(text, head_budget, from_end=False)
        tail_text = _cut_bytes(text, tail_budget, from_end=True)
        omitted_bytes = original_bytes - _byte_len(head_text) - _byte_len(tail_text)
        marker = f"\n[... 中略: {max(original_lines - 2, 0)} 行 / {max(omitted_bytes, 0)} バイトを省略 ...]\n"
        return TruncatedOutput(
            text=f"{head_text}{marker}{tail_text}",
            original_bytes=original_bytes,
            original_lines=original_lines,
            truncated=True,
        )

    # 先頭側と末尾側が重ならないようにする
    if len(head_lines) + len(tail_lines) > original_lines:
        keep_tail = max(original_lines - len(head_lines), 0)
        if keep_tail == 0:
            tail_lines = []
            tail_bytes = 0
        else:
            tail_lines = tail_lines[-keep_tail:]
            tail_bytes = sum(_byte_len(line) for line in tail_lines)

    omitted_lines = original_lines - len(head_lines) - len(tail_lines)
    omitted_bytes = original_bytes - head_bytes - tail_bytes

    head_text = "".join(head_lines)
    tail_text = "".join(tail_lines)
    if head_text and not head_text.endswith("\n"):
        head_text += "\n"

    marker = f"\n[... 中略: {max(omitted_lines, 0)} 行 / {max(omitted_bytes, 0)} バイトを省略 ...]\n"
    return TruncatedOutput(
        text=f"{head_text}{marker}{tail_text}",
        original_bytes=original_bytes,
        original_lines=original_lines,
        truncated=True,
    )


def format_command_output(
    *,
    stdout: Optional[str],
    stderr: Optional[str],
    exit_code: Optional[int],
    duration_seconds: float,
    timed_out: bool,
    timeout_seconds: Optional[int],
    max_output_bytes: int,
) -> Dict[str, Any]:
    """コマンド実行結果を LLM 向けのヘッダ付きテキストへ整形する。

    Codex CLI 準拠の形式:

        Exit code: 1
        Wall time: 2.4 seconds
        Total output lines: 5231     ← トリムした時だけ出す
        Output:
        <先頭>
        [... 中略: ... ...]
        <末尾>

    タイムアウト時は先頭に `command timed out after N seconds` を前置する。
    stdout と stderr は結合せず、それぞれ個別にトリムして返す。

    Args:
        stdout: 標準出力
        stderr: 標準エラー出力
        exit_code: 終了コード
        duration_seconds: 実行にかかった秒数
        timed_out: タイムアウトしたか
        timeout_seconds: 適用されたタイムアウト秒数
        max_output_bytes: stdout / stderr それぞれの許容バイト数

    Returns:
        Dict[str, Any]: output（整形済みテキスト）, stdout, stderr, truncated,
            original_lines, original_bytes を含む辞書
    """
    stdout_result = truncate_middle(stdout or "", max_output_bytes)
    stderr_result = truncate_middle(stderr or "", max_output_bytes)
    truncated = stdout_result.truncated or stderr_result.truncated

    total_lines = stdout_result.original_lines + stderr_result.original_lines
    total_bytes = stdout_result.original_bytes + stderr_result.original_bytes

    header_lines: List[str] = []
    if timed_out:
        header_lines.append(
            f"command timed out after {timeout_seconds} seconds"
        )
    header_lines.append(f"Exit code: {exit_code if exit_code is not None else 'unknown'}")
    header_lines.append(f"Wall time: {duration_seconds:.1f} seconds")
    if truncated:
        header_lines.append(f"Total output lines: {total_lines}")
    header_lines.append("Output:")

    body = stdout_result.text
    if stderr_result.text.strip():
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"Stderr:\n{stderr_result.text}"

    output_text = "\n".join(header_lines)
    if body:
        output_text = f"{output_text}\n{body}"

    return {
        "output": output_text,
        "stdout": stdout_result.text,
        "stderr": stderr_result.text,
        "truncated": truncated,
        "original_lines": total_lines,
        "original_bytes": total_bytes,
    }
