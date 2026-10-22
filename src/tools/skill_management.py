"""Chat tools over the existing Skill proposal/registry and browser execution."""

from __future__ import annotations

from typing import Any

from .core import ToolDefinition, ToolParam
from ..skills.routine import ROUTINE_PARAMETER, assert_public_inputs, digest, effective_values, routine_for
from ..services.skill_browser_execution import request_material_approval
from ..services.skill_execution_service import SkillExecutionService, skill_capability_enabled
from ..services.skill_learning_service import SkillLearningService
from ..services.skill_target_io import async_skill_target_lock, atomic_remove, skill_namespace_lock_key
from ..services.turn_context import get_turn_context


def build_skill_tools(config: Any, *, client: Any = None, learning: Any = None,
                      execution: Any = None) -> list[ToolDefinition]:
    def gate(browser=False):
        if not skill_capability_enabled(getattr(client, "config", None) or config, browser=browser):
            raise ValueError("skill_execution_disabled" if browser else "skills_disabled")
        turn = get_turn_context()
        if not turn.user_id or not turn.session_id or not (turn.message_id or turn.client_message_id):
            raise ValueError("skill_authenticated_turn_required")
        return turn

    async def approve(summary, arguments, context, turn):
        from ..services.agent_run_service import get_current_agent_run_id

        return await request_material_approval(summary=summary, arguments=arguments, context=context,
                                               user_id=turn.user_id, session_id=turn.session_id,
                                               agent_run_id=get_current_agent_run_id() or "", tool_name="manage_skill")

    async def manage_skill(action: str, skill_name: str = "", content: dict | None = None,
                           proposal_id: str = "") -> dict:
        try:
            turn = gate()
            service = learning or SkillLearningService()
            from ..skills.loader import load_all_skills
            from ..skills.registry import get_skill_registry

            load_all_skills(service._global_skills_dir)
            registry = get_skill_registry()
            if action == "list":
                return {"success": True, "skills": [{"name": s.name, "description": s.description,
                        "aliases": s.aliases, "executable": routine_for(s.to_dict()) is not None}
                        for s in registry.get_all()]}
            if action == "proposals":
                return {"success": True, "proposals": await service.list_proposals(actor_id=turn.user_id)}
            if action == "reject":
                proposal = await service.reject_proposal(proposal_id, actor_id=turn.user_id,
                                                         reason="チャットから却下")
                return {"success": True, "proposal": proposal}
            if action in {"create", "update", "get", "delete"}:
                skill = registry.get_by_alias(skill_name) or registry.get(skill_name)
                name = skill.name if skill else skill_name
                snapshot = service.snapshot_target(target_scope="global", target_name=name)
                if action == "get":
                    return {"success": snapshot.exists, "skill": snapshot.payload}
                if action == "delete":
                    if not snapshot.exists:
                        raise ValueError("skill_not_found")
                    approved = await approve("このスキルを削除します: " + name,
                                             {"operation": "delete", "skill_name": name},
                                             {"skill_hash": snapshot.sha256, "path": snapshot.canonical_path}, turn)
                    if approved is not True:
                        return {"success": False, "status": "needs_user", "reason": "skill_deletion_not_approved"}
                    gate()
                    async with async_skill_target_lock(skill_namespace_lock_key(snapshot.path)), async_skill_target_lock(snapshot.path):
                        current = service.snapshot_target(target_scope="global", target_name=name)
                        if current.sha256 != snapshot.sha256:
                            raise ValueError("skill_deletion_content_changed")
                        atomic_remove(snapshot.path)
                        registry.unregister(name)
                    return {"success": True, "deleted": name}
                payload = dict(snapshot.payload or {})
                payload.update(content or {})
                payload["name"] = name
                assert_public_inputs(payload)
                spec = routine_for(payload)
                if spec is not None:
                    # Validate placeholders/defaults before making a durable proposal.
                    spec.render(effective_values(payload, {}, ""))
                    payload["bound_tools"] = ["run_skill"]
                for alias in payload.get("aliases", []):
                    matched = registry.get_by_alias(alias)
                    if matched is not None and matched.name != name:
                        raise ValueError("skill_alias_ambiguous")
                proposal = await service.create_proposal(
                    actor_id=turn.user_id, operation=action, target_scope="global", target_name=name,
                    reason_type="procedure", proposed_content=payload,
                    provenance={"message_id": turn.message_id, "session_id": turn.session_id},
                    idempotency_key=digest([turn.user_id, turn.message_id or turn.client_message_id, action, name, payload]),
                )
                return {"success": True, "status": "proposed", "proposal": proposal,
                        "next_action": "manage_skill(action='apply', proposal_id=...)で内容を確認して適用してください"}
            if action == "revise":
                existing = await service.get_proposal(proposal_id, actor_id=turn.user_id)
                revised = {**existing["proposed_content"], **(content or {})}
                assert_public_inputs(revised)
                spec = routine_for(revised)
                if spec is not None:
                    spec.render(effective_values(revised, {}, ""))
                return {"success": True, "proposal": await service.revise_proposal(
                    proposal_id, actor_id=turn.user_id, proposed_content=revised)}
            if action == "apply":
                proposal = await service.get_proposal(proposal_id, actor_id=turn.user_id)
                spec = routine_for(proposal["proposed_content"])
                if spec is not None:
                    spec.render(effective_values(proposal["proposed_content"], {}, ""))
                context = {"proposal_id": proposal_id, "content_digest": digest(proposal["proposed_content"]),
                           "base_hash": proposal.get("base_hash")}
                content = proposal["proposed_content"]
                # Keep the display projection shallow enough for the existing
                # HumanInteractionManager envelope depth limit. The exact full
                # proposal remains bound by its digest and checked under DB lock.
                preview = {"operation": proposal["operation"], "proposal_id": proposal_id,
                           "skill_name": proposal["target_name"], "content_digest": context["content_digest"],
                           "description": content.get("description", ""),
                           "prompt_template": content.get("prompt_template", ""),
                           "aliases": content.get("aliases", []),
                           "parameter_defaults": {k: v.get("default", "") if isinstance(v, dict) else v
                                                  for k, v in (content.get("parameters") or {}).items()
                                                  if k != ROUTINE_PARAMETER}}
                summary = "この手順を保存します: " + proposal["target_name"] + "\n" + content.get("prompt_template", "")
                if spec is not None:
                    rendered = spec.render(effective_values(content, {}, ""))
                    preview["browser_routine"] = spec.model_dump()
                    summary += "\n既定の条件: " + " / ".join(rendered.confirmation.values())
                    summary += "\n開始URL: " + rendered.start_url + "\n成功の確認: " + rendered.completion_text
                approved = await approve(summary, preview,
                                         context, turn)
                if approved is not True:
                    return {"success": False, "status": "needs_user", "reason": "skill_apply_not_approved"}
                gate()
                fresh = await service.get_proposal(proposal_id, actor_id=turn.user_id)
                if digest(fresh["proposed_content"]) != context["content_digest"]:
                    raise ValueError("skill_approval_content_changed")
                applied = await service.apply_proposal(proposal_id, actor_id=turn.user_id,
                                                        expected_content_digest=context["content_digest"])
                return {"success": True, "proposal": applied}
            raise ValueError("skill_management_action_invalid")
        except Exception:
            # Never echo validation/provider exceptions containing model inputs or credentials.
            return {"success": False, "status": "failed", "reason": "skill_management_rejected"}

    async def run_skill(skill_name: str = "", input_text: str = "", overrides: dict | None = None,
                        request_id: str = "", resume: bool = False, cancel: bool = False, device_id: str = "") -> dict:
        try:
            gate(browser=True)
            service = execution or SkillExecutionService(getattr(client, "config", None) or config, client=client)
            return await service.run(skill_name=skill_name, input_text=input_text, overrides=overrides,
                                     request_id=request_id, resume=resume, cancel=cancel, device_id=device_id)
        except Exception:
            return {"success": False, "status": "failed", "reason": "skill_execution_rejected"}

    return [
        ToolDefinition(name="manage_skill", owner="skills", is_async=True, function=manage_skill,
                       side_effect="writes", supports_parallel=False, timeout_seconds=930,
                       description=("日常の手順を一度教えて保存・編集・一覧・削除する既存Skill管理。create/updateは提案、applyは確認dialogで明示承認後に適用。"
                                    "parameters.__routine__.defaultにbrowser実行定義(version=1,intent=check/commit,start_url,preparation_labels,commit_label,"
                                    "confirmation,confirmation_labels,form_values,completion_text,completion_url,result_fields,result_labels)を保存する。"
                                    "preparation_labelsは教えられた非確定の入力/選択/クリックcontrolだけ。"
                                    "confirmation/result_fieldsはcommit時同一のtarget/datetime/price等の値とキー集合。"
                                    "画面の明示ラベルはconfirmation_labels/result_labels（既定:対象/日時/税込総額）、料金は明示通貨と税込総額、日時は年月日/24時間で教える。"
                                    "form_valuesには確定に影響する全非secret実入力/選択/accountの表示ラベルと期待表示値を教える。日付などは{date}で展開する。"
                                    "必要なfacts/実フォーム状態を検証できない場合は確定せずneeds_userで止まる。"
                                    "ユーザーの手順だけを自然言語prompt_templateへ保存し、日付などはparametersのdefaultと{date}で変数化する。"
                                    "aliasesに『いつもの散髪』等の呼び名を含める。明示登録の提案が既にあればproposals/revise/applyを再利用する。credential/OTPは保存しない。"),
                       parameters=[ToolParam("action", "string", "操作", enum=["list", "get", "create", "update", "delete", "proposals", "revise", "apply", "reject"]),
                                   ToolParam("skill_name", "string", "正規名または呼び名", required=False, default=""),
                                   ToolParam("content", "object", "既存Skillのdescription,prompt_template,aliases,parameters等", required=False),
                                   ToolParam("proposal_id", "string", "提案ID", required=False, default="")]),
        ToolDefinition(name="run_skill", owner="skills", is_async=True, function=run_skill,
                       side_effect="writes", supports_parallel=False, timeout_seconds=930,
                       description=("『いつものあれをやって』を登録済みSkillで実行する。まずmanage_skillで呼び名を解決する。"
                                    "overridesは今回だけのdate等で保存defaultsは変えない。予約/購入/契約確定は対象・日時・料金を確認dialogで承認してから送信。"
                                    "結果はsuccess/completion_verified/evidenceで報告し、steps_completedだけでは目的の成功としない。"
                                    "needs_user時は画面handoff、unknown時は同じrequest_idとresume=trueで読取照合だけを行う。"
                                    "resumeの入力・日付・PCは凍結済み。password/OTPはモデルやチャットへ送らずブラウザでユーザーが入力する。"),
                       parameters=[ToolParam("skill_name", "string", "Skill名またはalias", required=False, default=""),
                                   ToolParam("input_text", "string", "今回の補足", required=False, default=""),
                                   ToolParam("overrides", "object", "今回だけの登録parameter値", required=False),
                                   ToolParam("request_id", "string", "初回は省略可。再開は結果のrequest_idを使用", required=False, default=""),
                                   ToolParam("resume", "boolean", "同じrequest_idで明示再開", required=False, default=False),
                                   ToolParam("cancel", "boolean", "送信前の待機を中止する。実予約の取消ではない。送信後は読取照合が必要", required=False, default=False),
                                   ToolParam("device_id", "string", "選択PC。省略時は既存PC設定", required=False, default="")]),
    ]
