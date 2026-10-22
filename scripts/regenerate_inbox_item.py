"""既存Inbox項目の文書・返信案を、保持済みの元資料から再生成する。

既定は dry-run（対象特定・元資料件数・現在の返信案状態の表示のみ。モデル呼び出しなし）。
  --generate  モデルを呼び出して生成結果をプレビューする（書込みなし）。
  --apply     生成して Inbox 文書と返信案を置き換える（--generate を含む）。

対象は --item-id、または --session-id / --run-id（Inbox受付revisionの source_refs から特定）。
元メール/元資料ノードは変更しない。出力が空・上限打ち切りの場合は置換しない。

例:
  venv\\Scripts\\python scripts/regenerate_inbox_item.py --run-id 843f468b-...
  venv\\Scripts\\python scripts/regenerate_inbox_item.py --item-id <uuid> --generate
  venv\\Scripts\\python scripts/regenerate_inbox_item.py --item-id <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.llm.generation_error import ModelOutputIncompleteError  # noqa: E402
from src.services.inbox_regeneration_service import (  # noqa: E402
    InboxRegenerationError,
    InboxRegenerationReport,
    REPLY_DRAFT_PLACEHOLDER,
    find_inbox_item_ids,
    regenerate_inbox_item,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_argument_group("対象")
    target.add_argument("--item-id", help="Inbox項目のDocsノードID")
    target.add_argument("--session-id", help="/inbox を実行した会話セッションID")
    target.add_argument("--run-id", help="/inbox のAgentRun ID")
    parser.add_argument("--generate", action="store_true", help="モデルを呼び出して生成プレビュー")
    parser.add_argument("--apply", action="store_true", help="生成結果で置き換える")
    parser.add_argument("--user-id", help="書込みユーザー（既定: 項目の最終更新者/作成者）")
    parser.add_argument("--no-draft", action="store_true", help="返信案は再生成しない")
    parser.add_argument(
        "--replace-existing-draft",
        action="store_true",
        help=f"「{REPLY_DRAFT_PLACEHOLDER}」以外の既存返信案も置き換える",
    )
    return parser


def _default_generator():
    from src.config import Config
    from src.llm.manager import create_llm_client

    client = create_llm_client(Config())
    if not hasattr(client, "generate_plain_text_async"):
        # Only the plain-text API reports truncation via
        # require_complete_model_output; refuse other transports.
        raise InboxRegenerationError(
            "設定中のLLMクライアントは未完出力を判定できないため再生成に使えません。"
        )

    async def generate(prompt: str) -> str:
        return str(
            await client.generate_plain_text_async(
                prompt, request_type="inbox_regeneration"
            )
        )

    return generate


def format_report(report: InboxRegenerationReport) -> list[str]:
    context = report.context
    kinds: dict[str, int] = {}
    for material in context.materials:
        kinds[material.kind] = kinds.get(material.kind, 0) + 1
    draft = context.reply_draft
    lines = [
        f"Inbox項目: {context.item_id} {context.title}",
        f"プロジェクト: {context.project_id}",
        "元資料: "
        + (", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())) or "なし"),
        f"紐付けタスク: {context.task_id or 'なし'}",
        "現在の返信案: "
        + (
            "欄なし"
            if draft is None
            else ("未作成(固定文言)" if draft in ("", REPLY_DRAFT_PLACEHOLDER) else "あり")
        ),
    ]
    if report.result is not None:
        document = report.result.document
        lines.append(f"生成タイトル: {document.title}")
        lines.append("生成概要:")
        lines.extend(f"  - {block.text}" for block in document.overview)
        lines.extend(f"生成章: {section.title}" for section in document.sections)
        if report.result.reply_draft is not None:
            lines.append("生成返信案:")
            lines.extend(f"  {line}" for line in report.result.reply_draft.splitlines())
    lines.extend(f"note: {note}" for note in report.notes)
    lines.append("結果: " + ("置き換えました" if report.applied else "書込みなし"))
    return lines


async def run(args: argparse.Namespace, *, session=None, generate=None) -> int:
    owns_session = session is None
    if owns_session:
        from src.memory.database import get_database_manager

        session = await get_database_manager().get_session()
    try:
        if args.item_id:
            item_ids = [UUID(args.item_id)]
        else:
            item_ids = await find_inbox_item_ids(
                session, session_id=args.session_id, run_id=args.run_id
            )
        if len(item_ids) != 1:
            print(
                f"対象Inbox項目を1件に特定できません: {len(item_ids)}件"
                + "".join(f"\n  - {item_id}" for item_id in item_ids)
            )
            return 2
        want_generate = bool(args.generate or args.apply)
        if want_generate and generate is None:
            generate = _default_generator()
        report = await regenerate_inbox_item(
            session,
            item_id=item_ids[0],
            generate=generate if want_generate else None,
            apply=bool(args.apply),
            user_id=UUID(args.user_id) if args.user_id else None,
            regenerate_draft=not args.no_draft,
            replace_existing_draft=bool(args.replace_existing_draft),
        )
        print("\n".join(format_report(report)))
        return 0
    except ModelOutputIncompleteError as exc:
        print(f"モデル出力が未完のため置き換えませんでした: {exc}")
        return 3
    except (InboxRegenerationError, ValueError) as exc:
        print(f"再生成できませんでした: {exc}")
        return 2
    finally:
        if owns_session:
            await session.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.item_id or args.session_id or args.run_id):
        parser.error("--item-id / --session-id / --run-id のいずれかを指定してください。")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
