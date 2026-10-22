"""実装タスクで「新しい情報の得られない読取」だけが続く停滞を、正直な停止で打ち切る。

対象は ``mutate`` モードの Agent task だけで、呼び出し側が明示的に有効化する。

- 新規の調査（結果の内容が初出の読取）は進捗として扱い、件数だけでは停止しない。
  変更・検証なしで読取が続く場合は、通知（チェックポイント）だけを繰り返し付ける。
- 停止するのは、結果が既出と同一の読取（引数だけ変えても同じ結果なら同一）が
  連続したときだけ。読取元が外部で変化して結果が変われば新規情報として扱う。
- 進捗として戻すのは、実行され成功した変更系・コマンド系ツールだけ。失敗・未実行の
  変更、未知のツール、未完了コマンドの出力確認（read_command_output）は進捗にも
  停滞にも数えない。停止後の再実行や副作用の再生は行わない。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .generation_error import GenerationFailure, read_stagnation_failure

# 変更・コマンドなしで続いた読取ラウンド数ごとに通知を付ける（停止はしない）。
NOTICE_EVERY_READ_ROUNDS = 24
# 新規情報のない読取の連続回数。通知→停止。
NOTICE_AFTER_STALE_READS = 3
STOP_AFTER_STALE_READS = 6
# 既出結果として記憶するダイジェスト数の上限。
SEEN_DIGEST_CAPACITY = 512

CHECKPOINT_NOTICE = (
    "[進捗チェックポイント] 変更や検証コマンドを伴わない読取が続いています。"
    "次のラウンドでは、(1) 把握できた事実から対象ファイルを編集する、"
    "(2) 実行すべき検証コマンドを実行する、(3) 判断に必要な情報が得られないなら"
    "その不足を具体的に報告して停止する、のいずれかを行ってください。"
    "同じ内容の読取の繰り返しや、実行していない変更の報告は行わないでください。"
)


def _tool_sets() -> tuple[frozenset[str], frozenset[str]]:
    from . import tool_policy

    def short(names: Any) -> frozenset[str]:
        return frozenset(str(n).rsplit(".", 1)[-1].casefold() for n in names)

    effect = (
        tool_policy.FILESYSTEM_MUTATION_TOOL_NAMES
        | tool_policy.COMMAND_TOOL_NAMES
        | tool_policy.DOCS_MUTATION_TOOL_NAMES
    )
    read = (
        tool_policy.FILESYSTEM_READ_TOOL_NAMES
        | tool_policy.DOCS_READ_TOOL_NAMES
        | tool_policy.SEARCH_TOOL_NAMES
    ) - effect
    # 未完了コマンドの出力確認は待機であり、読取の停滞にも進捗にも数えない。
    return short(read) - {"read_command_output"}, short(effect)


def _digest(output: Any) -> str:
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
    hasher = hashlib.sha256()
    for start in range(0, len(text), 65536):
        hasher.update(text[start:start + 65536].encode("utf-8", "replace"))
    return hasher.hexdigest()[:24]


@dataclass
class ReadStagnationPolicy:
    notice_every_rounds: int = NOTICE_EVERY_READ_ROUNDS
    notice_after_stale: int = NOTICE_AFTER_STALE_READS
    stop_after_stale: int = STOP_AFTER_STALE_READS
    stopped: bool = field(default=False, init=False)
    stop_signature: str = field(default="", init=False)
    _reads: int = field(default=0, init=False)
    _stale: int = field(default=0, init=False)
    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict, init=False)
    _sets: tuple[frozenset[str], frozenset[str]] | None = field(default=None, init=False)

    def observe(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        executed: bool = True,
        success: bool = True,
        output: Any = "",
    ) -> tuple[str, GenerationFailure | None]:
        """1回のツール結果を記録し、(モデルへ添える通知, 停止failure) を返す。"""

        if self._sets is None:
            self._sets = _tool_sets()
        reads, effects = self._sets
        name = str(tool_name or "").rsplit(".", 1)[-1].casefold()
        if not executed or self.stopped:
            return "", None
        if name in effects:
            if success:
                self._reads = self._stale = 0
                self._seen.clear()
            return "", None
        if name not in reads:
            return "", None
        self._reads += 1
        digest = _digest(output)
        if digest in self._seen:
            self._seen.move_to_end(digest)
            self._stale += 1
        else:
            self._seen[digest] = None
            while len(self._seen) > SEEN_DIGEST_CAPACITY:
                self._seen.popitem(last=False)
            self._stale = 0
        if self._stale >= self.stop_after_stale:
            self.stopped = True
            self.stop_signature = "no_new_information_reads"
            return "", read_stagnation_failure(signature=self.stop_signature)
        if (self._stale and self._stale >= self.notice_after_stale) or (
            self._reads % max(1, self.notice_every_rounds) == 0
        ):
            return CHECKPOINT_NOTICE, None
        return "", None


_ACTIVE: ContextVar[ReadStagnationPolicy | None] = ContextVar("aoitalk_read_stagnation", default=None)


def current_read_stagnation_policy() -> ReadStagnationPolicy | None:
    return _ACTIVE.get()


@contextmanager
def activate_read_stagnation_policy(
    policy: ReadStagnationPolicy | None = None,
) -> Iterator[ReadStagnationPolicy]:
    active = policy or ReadStagnationPolicy()
    token = _ACTIVE.set(active)
    try:
        yield active
    finally:
        _ACTIVE.reset(token)
