"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { withBasePath } from "@/lib/base-path";
import { Button } from "@/components/ui/button";

export type DelegationNode = {
  id: string; kind: "conversation" | "dialogue" | "work";
  agent_id: string | null; agent_revision_id: string | null; agent_name: string | null;
  title: string; status: string; work_id: string | null; dialogue_id: string | null; task_id: string | null;
};
export type DelegationMap = {
  schema_version: "conversation-delegation-map-v1"; session_id: string; observed_at: string;
  graph: { nodes: DelegationNode[]; edges: { id: string; source: string; target: string; kind: "delegation" | "work" }[] };
  has_more: boolean;
};

function validMap(value: unknown, sessionId: string): value is DelegationMap {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  if (v.schema_version !== "conversation-delegation-map-v1" || v.session_id !== sessionId || typeof v.observed_at !== "string" || typeof v.has_more !== "boolean" || !v.graph || typeof v.graph !== "object") return false;
  const graph = v.graph as Record<string, unknown>;
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return false;
  const nullable = (x: unknown) => x === null || typeof x === "string";
  const ids = new Set<string>();
  for (const n of graph.nodes) {
    if (!n || typeof n !== "object" || typeof n.id !== "string" || !n.id || ids.has(n.id) || !["conversation", "dialogue", "work"].includes(n.kind) || typeof n.title !== "string" || typeof n.status !== "string" || ![n.agent_id, n.agent_revision_id, n.agent_name, n.work_id, n.dialogue_id, n.task_id].every(nullable)) return false;
    ids.add(n.id);
  }
  return graph.edges.every((e) => e && typeof e === "object" && typeof e.id === "string" && typeof e.source === "string" && typeof e.target === "string" && ["delegation", "work"].includes(e.kind) && ids.has(e.source) && ids.has(e.target));
}

function statusLabel(node: DelegationNode): string {
  const labels: Record<string, string> = {
    queued: "待機中", pending: "待機中", running: "実行中", active: "実行中",
    failed: "失敗", cancelled: "キャンセル", canceled: "キャンセル",
    succeeded: node.kind === "dialogue" ? "応答済み" : "処理完了",
  };
  return labels[node.status] ?? node.status;
}

type Snapshot = { scope: string; data: DelegationMap | null; error: boolean };

/** Conversation-scoped read projection. No request is made while closed or unsaved. */
export function ConversationDelegationMap({ sessionId, ownerId, serverIdentity, open, onClose, presentation = "sidebar" }: {
  sessionId: string | null; ownerId: string | null | undefined; serverIdentity: string; open: boolean; onClose: () => void; presentation?: "sidebar" | "message-overlay";
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const scope = JSON.stringify([serverIdentity, ownerId, sessionId]);
  const visible = open && Boolean(sessionId && sessionId !== "new" && !sessionId.startsWith("local-") && ownerId && serverIdentity);

  useEffect(() => {
    if (!visible || !sessionId) return;
    let alive = true;
    let controller: AbortController | null = null;
    const load = async () => {
      if (document.visibilityState === "hidden") return;
      controller?.abort();
      const request = new AbortController();
      controller = request;
      try {
        const response = await fetch(withBasePath(`/api/python-proxy/conversations/${encodeURIComponent(sessionId)}/delegation-map`), {
          credentials: "include", cache: "no-store", signal: request.signal,
        });
        if (!response.ok) throw new Error("request failed");
        const data: unknown = await response.json();
        if (!validMap(data, sessionId)) throw new Error("invalid response");
        if (alive && !request.signal.aborted) setSnapshot({ scope, data, error: false });
      } catch {
        if (alive && !request.signal.aborted) setSnapshot({ scope, data: null, error: true });
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") controller?.abort();
      else void load();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => { alive = false; controller?.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [visible, sessionId, scope]);

  const current = snapshot?.scope === scope ? snapshot : null;
  const nodes = current?.data?.graph.nodes ?? [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = current?.data?.graph.edges ?? [];
  return open ? <aside aria-label="委任の流れ" data-testid="conversation-delegation-map" className={presentation === "message-overlay" ? "absolute right-0 top-0 z-30 flex max-h-[min(40%,12rem)] w-[min(92vw,22rem)] flex-col overflow-hidden rounded-bl-lg border border-border-subtle bg-background shadow-lg" : "absolute right-0 top-12 z-30 flex max-h-[min(70vh,36rem)] w-[min(92vw,22rem)] flex-col overflow-hidden rounded-bl-lg border border-border-subtle bg-background shadow-lg min-[768px]:static min-[768px]:max-h-none min-[768px]:w-80 min-[768px]:shrink-0 min-[768px]:rounded-none min-[768px]:shadow-none"}>
      <div className="flex items-center justify-between border-b border-border-subtle p-3"><h2 className="text-sm font-medium">委任の流れ</h2><Button type="button" variant="ghost" size="sm" onClick={onClose}>閉じる</Button></div>
      <div className="overflow-y-auto p-3 text-sm">
        {!visible ? <p>保存済みの会話で表示できます。</p> : !current ? <p role="status">読み込み中…</p> : current.error ? <p role="alert">委任の流れを読み込めませんでした。</p> : nodes.filter((node) => node.kind !== "conversation").length === 0 ? <p>この会話の委任はまだありません。</p> : <ul className="space-y-2">{nodes.filter((node) => node.kind !== "conversation").map((node) => {
          const parents = [...new Map(edges.filter((edge) => edge.target === node.id).map((edge) => [`${edge.kind}:${edge.source}:${edge.target}`, edge])).values()];
          return <li key={node.id} className="rounded border border-border-subtle p-2"><div className="text-xs text-text-secondary">{parents.map((edge) => { const parent = byId.get(edge.source); const sameWorkAgent = edge.kind === "work" && parent?.agent_id && parent.agent_id === node.agent_id; return <div key={`${edge.kind}:${edge.source}:${edge.target}`}>{sameWorkAgent ? (node.agent_name || parent.agent_name || "担当") : <>{parent?.agent_name || parent?.title || "会話"} → {node.agent_name || "担当"}</>}</div>; })}</div><div className="break-words font-medium">{node.title}</div><div className="mt-1 flex items-center justify-between gap-2 text-xs text-text-secondary"><span>{statusLabel(node)}</span>{node.work_id && <Link className="text-primary underline" href={`/operations?tab=work&work_id=${encodeURIComponent(node.work_id)}`}>実行の詳細</Link>}</div></li>;
        })}</ul>}
        {current?.data?.has_more && <p className="mt-3 text-xs text-text-secondary">表示件数の上限に達しています。</p>}
      </div>
    </aside> : null;
}
