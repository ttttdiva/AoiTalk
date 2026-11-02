"use client";

import { useCallback, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import useSWR from "swr";
import { useOptionalRuntimeContext } from "@/contexts/runtime-context";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { employeeApi } from "@/lib/agent-employees-api";
import { TeamGraphWorkspace } from "./agents/team-graph-workspace";

/** The execution projection and the persisted design share the Map entry, not data. */
export function OperationsMapAuthoring({ children }: { children: ReactNode }) {
  const router = useRouter();
  const query = useSearchParams();
  const userId = useCurrentUserId();
  const features = useOptionalRuntimeContext()?.runtimeFeatures?.application_features;
  const enabled = features?.virtual_company === true && features?.autonomous_agent_runtime === true;
  const design = query?.get("map_mode") === "design";
  const readOnly = query?.get("read_only") === "1";
  const [dirty, setDirty] = useState(false);
  const updateDirty = useCallback((value: boolean) => setDirty(value), []);
  const employees = useSWR(design && enabled && userId ? ["map-employee-list", userId] : null, () => employeeApi.list(), { shouldRetryOnError: false });
  const catalog = useSWR(design && enabled && userId ? ["map-employee-catalog", userId] : null, () => employeeApi.catalog(), { shouldRetryOnError: false });
  const navigate = (next: boolean, guarded = true) => {
    if (!next && guarded && dirty && !window.confirm("未保存の変更があります。実行状況に移動しますか？下書きはこのブラウザーに保持されます。")) return;
    const params = new URLSearchParams(query?.toString());
    params.set("tab", "overview"); params.set("overview_view", "map");
    if (next) params.set("map_mode", "design"); else params.delete("map_mode");
    router.push(`/operations?${params.toString()}`, { scroll: false });
  };
  return <div className="flex min-h-0 min-w-0 flex-1 flex-col">
    <div role="group" aria-label="Operations Map モード" className="flex shrink-0 gap-1 border-b border-border px-4 py-2">
      <button type="button" aria-pressed={!design} className="rounded-md px-3 py-1.5 text-sm aria-pressed:bg-primary aria-pressed:text-primary-foreground" onClick={() => navigate(false)}>実行状況</button>
      <button type="button" aria-pressed={design} className="rounded-md px-3 py-1.5 text-sm aria-pressed:bg-primary aria-pressed:text-primary-foreground" onClick={() => navigate(true)}>Team設計</button>
    </div>
    {design ? <div className="min-h-0 flex-1 overflow-auto p-4">
      {!enabled ? <p>この環境では Agent Team を利用できません。</p> : !userId || (!employees.data && !employees.error) || (!catalog.data && !catalog.error) ? <p role="status">Agent Team を読み込み中…</p> : employees.error || catalog.error ? <p role="alert">Agent または権限を読み込めませんでした。</p> : readOnly || !catalog.data?.can_manage ? <p>Team設計の編集には管理権限が必要です。</p> : <TeamGraphWorkspace key={userId} agents={employees.data?.agents ?? []} mapMode onDirtyChange={updateDirty} onBack={() => navigate(false, false)} />}
    </div> : children}
  </div>;
}
