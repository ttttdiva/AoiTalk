"use client";

import { useState } from "react";
import useSWR from "swr";
import { mediaOperationsApi } from "@/lib/media-operations-api";
import { assignmentEnvelope, employeeApi, employeePath, employeeRequest, employeeScopeRequest, type AssignmentKind, type EmployeeAssignment, type EmployeeProfile, type ScopeOption } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { EmployeeCheck, EmployeeField, EmployeeForm, EmployeeNotice, EmployeeSelect, EmployeeStatus, employeeInputClass } from "./employee-fields";

export const optionLabel = (o: ScopeOption) => o.name || o.display_name || o.title || o.current_revision?.display_name || o.id;
export function EmployeeAssignments({ agentId, canManage }: { agentId: string; canManage: boolean }) {
  const profile = useSWR(["employee-profile", agentId], () => employeeApi.profile(agentId), { shouldRetryOnError: false });
  const spaces = useSWR("employee-spaces", () => employeeScopeRequest<{ spaces: ScopeOption[] }>("/api/spaces"), { shouldRetryOnError: false });
  const projects = useSWR("employee-projects", () => employeeScopeRequest<{ projects: ScopeOption[] }>("/api/projects"), { shouldRetryOnError: false });
  return <section className="space-y-6"><h3 className="text-lg font-semibold">組織・所属とアクセス範囲</h3>
    <EmployeeNotice error={spaces.error || projects.error} reload={() => { void spaces.mutate(); void projects.mutate(); }} />
    {profile.isLoading ? <p>組織プロフィールを読み込み中…</p> : profile.error && profile.error.status !== 404 ? <EmployeeNotice error={profile.error} reload={() => void profile.mutate()} /> :
      <OrganizationProfile key={JSON.stringify(profile.data)} agentId={agentId} profile={profile.data?.organization_profile ?? {}} spaces={spaces.data?.spaces ?? []} canManage={canManage} onSaved={() => void profile.mutate()} />}
    <AssignmentEditor agentId={agentId} canManage={canManage} spaces={spaces.data?.spaces ?? []} projects={projects.data?.projects ?? []} />
  </section>;
}
function OrganizationProfile({ agentId, profile, spaces, canManage, onSaved }: { agentId: string; profile: EmployeeProfile; spaces: ScopeOption[]; canManage: boolean; onSaved: () => void }) {
  const [form, setForm] = useState({ job_title: profile.job_title ?? "", responsibility_summary: profile.responsibility_summary ?? "", primary_space_id: profile.primary_space_id ?? "", autonomy_level: profile.autonomy_level ?? "supervised", employment_state: profile.employment_state ?? "active" });
  return <EmployeeForm label="組織プロフィールを保存" disabled={!canManage} reload={onSaved} onSave={async () => { await employeeApi.saveProfile(agentId, { ...form, primary_space_id: form.primary_space_id || null }); onSaved(); }}>
    <div className="grid gap-4 md:grid-cols-2">
      <EmployeeField label="役職"><input className={employeeInputClass} maxLength={160} value={form.job_title} onChange={e => setForm({ ...form, job_title: e.target.value })} /></EmployeeField>
      <EmployeeField label="組織での責務"><textarea className={employeeInputClass} maxLength={10000} value={form.responsibility_summary} onChange={e => setForm({ ...form, responsibility_summary: e.target.value })} /></EmployeeField>
      <EmployeeSelect label="主所属 Space" value={form.primary_space_id} onChange={v => setForm({ ...form, primary_space_id: v })} options={spaces.map(s => ({ value: s.id, label: optionLabel(s) }))} />
      <EmployeeSelect label="雇用状態" required value={form.employment_state} onChange={v => setForm({ ...form, employment_state: v })} options={[{ value: "active", label: "在籍" }, { value: "on_leave", label: "休職" }, { value: "suspended", label: "停止" }, { value: "terminated", label: "契約終了" }, { value: "contractor", label: "業務委託" }]} />
      <EmployeeSelect label="自律レベル" required value={form.autonomy_level} onChange={v => setForm({ ...form, autonomy_level: v })} options={[{ value: "disabled", label: "無効" }, { value: "supervised", label: "監督付き" }, { value: "bounded", label: "範囲内自律" }, { value: "autonomous", label: "自律（組織の上限内）" }]} />
    </div><p className="text-sm text-muted-foreground">主所属のプロフィールとアクセス権は別々の設定です。次の所属・権限一覧で必要なアクセスを追加してください。</p>
  </EmployeeForm>;
}
function AssignmentEditor({ agentId, canManage, spaces, projects }: { agentId: string; canManage: boolean; spaces: ScopeOption[]; projects: ScopeOption[] }) {
  const [kind, setKind] = useState<AssignmentKind>("space-assignments");
  const [target, setTarget] = useState("");
  const [projectId, setProjectId] = useState("");
  const [role, setRole] = useState("supporting");
  const [read, setRead] = useState(true);
  const [write, setWrite] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [revoking, setRevoking] = useState("");
  const assignments = useSWR(["employee-assignments", agentId, kind], () => employeeRequest<Record<string, EmployeeAssignment[]>>(`${employeePath(agentId)}/${kind}?include_inactive=true`), { shouldRetryOnError: false });
  const tasks = useSWR(kind === "task-assignments" && projectId ? ["employee-tasks", projectId] : null, () => employeeScopeRequest<ScopeOption[]>(`/api/tasks?project_id=${encodeURIComponent(projectId)}`), { shouldRetryOnError: false });
  const personas = useSWR(kind === "persona-operator-assignments" ? "employee-personas" : null, () => mediaOperationsApi.listPersonas(), { shouldRetryOnError: false });
  const options = kind === "space-assignments" ? spaces : kind === "project-grants" ? projects : kind === "task-assignments" ? tasks.data ?? [] : personas.data ?? [];
  const roles = kind === "space-assignments" ? ["primary", "secondary", "supporting"] : kind === "project-grants" ? ["viewer", "member", "admin", "owner"] : kind === "task-assignments" ? ["executor", "owner", "reviewer", "observer"] : ["operator", "strategist", "researcher", "creator", "analyst", "publisher"];
  const roleLabels: Record<string, string> = { primary: "主所属", secondary: "副所属", supporting: "支援", viewer: "閲覧", member: "メンバー", admin: "管理者", owner: "所有者", executor: "実行", reviewer: "レビュー", observer: "観察", operator: "運用", strategist: "戦略", researcher: "調査", creator: "制作", analyst: "分析", publisher: "公開" };
  return <div className="space-y-4 border-t pt-5">
    <EmployeeSelect label="所属・権限の種類" required value={kind} onChange={v => { setKind(v as AssignmentKind); setTarget(""); setRole(v === "space-assignments" ? "supporting" : v === "project-grants" ? "viewer" : v === "task-assignments" ? "executor" : "operator"); }} options={[{ value: "space-assignments", label: "Space 所属" }, { value: "project-grants", label: "Project 権限" }, { value: "task-assignments", label: "Task 担当" }, { value: "persona-operator-assignments", label: "Persona 担当" }]} />
    <EmployeeNotice error={assignments.error || tasks.error || personas.error || error} reload={() => void assignments.mutate()} />
    <EmployeeForm disabled={!canManage} label="所属・権限を追加" onSave={async () => {
      const body = kind === "space-assignments" ? { space_id: target, assignment_kind: role }
        : kind === "project-grants" ? { project_id: target, role, permissions: { read, write, delete: false, manage_members: false, manage_settings: false } }
        : kind === "task-assignments" ? { task_id: target, assignment_role: role }
        : { persona_id: target, role, is_primary: false, capability_ceiling: [] };
      await employeeRequest(`${employeePath(agentId)}/${kind}`, "POST", body); setTarget(""); await assignments.mutate();
    }}>
      {kind === "task-assignments" && <EmployeeSelect label="Task の Project" required value={projectId} onChange={v => { setProjectId(v); setTarget(""); }} options={projects.map(p => ({ value: p.id, label: optionLabel(p) }))} />}
      <div className="grid gap-4 md:grid-cols-2"><EmployeeSelect label="割り当て先" required value={target} onChange={setTarget} options={options.map(o => ({ value: o.id, label: optionLabel(o) }))} />
        <EmployeeSelect label="担当ロール" required value={role} onChange={setRole} options={roles.map(r => ({ value: r, label: roleLabels[r] }))} /></div>
      {kind === "project-grants" && <><EmployeeCheck label="読取を許可" checked={read} onChange={setRead} /><EmployeeCheck label="書込を許可" checked={write} onChange={setWrite} /></>}
      {!options.length && <p className="text-sm text-muted-foreground">アクセス可能な割り当て先がありません。対象の作成・所属設定を確認してください。</p>}
    </EmployeeForm>
    <ul className="space-y-2">{(assignments.data?.[assignmentEnvelope[kind]] ?? []).map(a => {
      const id = a.space_id || a.project_id || a.task_id || a.persona_id;
      return <li key={a.id} className="flex flex-wrap items-center gap-3 rounded border p-3 text-sm"><span>{options.find(o => o.id === id) ? optionLabel(options.find(o => o.id === id)!) : id}</span><span>{roleLabels[a.role || a.assignment_kind || a.assignment_role || ""]}</span><EmployeeStatus state={a.state} />
        {canManage && a.state === "active" && <Button type="button" size="sm" variant="outline" disabled={revoking === a.id} onClick={async () => { setRevoking(a.id); setError(null); try { await employeeRequest(`${employeePath(agentId)}/${kind}/${encodeURIComponent(a.id)}/revoke`, "POST"); await assignments.mutate(); } catch (e) { setError(e); } finally { setRevoking(""); } }}>解除</Button>}</li>;
    })}</ul><p className="text-sm text-muted-foreground">解除した所属は履歴として残り、新しい仕事の許可には使われません。</p>
  </div>;
}
