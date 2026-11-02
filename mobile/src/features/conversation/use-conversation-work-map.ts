import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { fetchApiAtServerFingerprint, getConfiguredApiServerFingerprint } from "../../lib/api-client";
import { getAuthScopeRevision, readAuthActor, subscribeAuthScope } from "../../lib/auth";
import { getNetworkEndpointRoutingRevision, subscribeNetworkEndpointRouting } from "../../lib/connection-routing";
import type { components } from "../../types/api-types.gen";
import type { ConversationWorkMapData, ConversationWorkMapEntry } from "./ConversationWorkMapSheet";

type Node = components["schemas"]["DelegationMapNode"];
type Edge = components["schemas"]["DelegationMapEdge"];
type MapDto = components["schemas"]["DelegationMapResponse"];
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const nullableString = (value: unknown) => value === null || typeof value === "string";

export function parseConversationDelegationMap(value: unknown, sessionId: string): MapDto {
  if (!object(value) || value.schema_version !== "conversation-delegation-map-v1" || value.session_id !== sessionId || typeof value.observed_at !== "string" || typeof value.has_more !== "boolean" || !object(value.graph) || !Array.isArray(value.graph.nodes) || !Array.isArray(value.graph.edges)) throw new Error("Invalid map");
  const nodes = value.graph.nodes;
  const edges = value.graph.edges;
  if (!nodes.every((n: unknown) => object(n) && typeof n.id === "string" && !!n.id && ["conversation", "dialogue", "work"].includes(n.kind as string) && typeof n.title === "string" && typeof n.status === "string" && ["agent_id", "agent_revision_id", "agent_name", "work_id", "dialogue_id", "task_id"].every((key) => nullableString(n[key]))) ||
      !edges.every((e: unknown) => object(e) && typeof e.id === "string" && !!e.id && typeof e.source === "string" && typeof e.target === "string" && ["delegation", "work"].includes(e.kind as string)) ||
      new Set(nodes.map((n: Node) => n.id)).size !== nodes.length || new Set(edges.map((e: Edge) => e.id)).size !== edges.length) throw new Error("Invalid map");
  const nodeIds = new Set(nodes.map((n: Node) => n.id));
  if (!edges.every((e: Edge) => nodeIds.has(e.source) && nodeIds.has(e.target))) throw new Error("Invalid map");
  return value as MapDto;
}

function statusLabel(node: Node): string {
  if (node.status === "succeeded") return node.kind === "dialogue" ? "応答済み" : "処理完了";
  const labels: Record<string, string> = { pending: "待機中", queued: "待機中", running: "実行中", failed: "失敗", cancelled: "キャンセル", canceled: "キャンセル", blocked: "保留中" };
  return labels[node.status] ?? node.status;
}
function label(node: Node | undefined): string { return node?.agent_name?.trim() || node?.title?.trim() || "会話"; }
export function projectConversationDelegationMap(dto: MapDto, scopeKey: string): ConversationWorkMapData {
  const nodes = new Map(dto.graph.nodes.map((node) => [node.id, node]));
  const incoming = new Map<string, Edge[]>();
  for (const edge of dto.graph.edges) incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge]);
  const entries: ConversationWorkMapEntry[] = dto.graph.nodes.filter((node) => node.kind !== "conversation").map((node) => {
    const parent = nodes.get(incoming.get(node.id)?.[0]?.source ?? "");
    return { id: node.id, parentId: parent && parent.kind !== "conversation" ? parent.id : null, instructorLabel: label(parent), assignedAgentLabel: label(node), shortInstruction: node.title, status: statusLabel(node), ...(node.work_id?.trim() ? { workReference: { kind: "work" as const, id: node.work_id } } : {}) };
  });
  const relationships = dto.graph.edges.filter((edge) => (incoming.get(edge.target)?.indexOf(edge) ?? 0) > 0).map((edge) => ({ id: edge.id, sourceLabel: label(nodes.get(edge.source)), targetLabel: label(nodes.get(edge.target)) }));
  return { scopeKey, conversationId: dto.session_id, state: "ready", entries, relationships, hasMore: dto.has_more };
}

type Options = { sessionId: string; userId?: string | null; isAuthenticated: boolean; enabled: boolean };
export function useConversationWorkMap({ sessionId, userId, isAuthenticated, enabled }: Options) {
  const eligible = enabled && isAuthenticated && !!userId && !!sessionId && sessionId !== "new" && !sessionId.startsWith("local-");
  const routingRevision = useSyncExternalStore(subscribeNetworkEndpointRouting, getNetworkEndpointRoutingRevision);
  const authRevision = useSyncExternalStore(subscribeAuthScope, getAuthScopeRevision);
  const identityBase = JSON.stringify([eligible, userId, sessionId, routingRevision, authRevision]);
  const identityRef = useRef({ base: identityBase, epoch: 0 });
  if (identityRef.current.base !== identityBase) identityRef.current = { base: identityBase, epoch: identityRef.current.epoch + 1 };
  const identity = JSON.stringify([identityBase, identityRef.current.epoch]);
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [result, setResult] = useState<{ identity: string; version: number; data: ConversationWorkMapData } | null>(null);
  const generation = useRef(0);
  const visible = eligible && openedFor === identity;
  const pendingKey = JSON.stringify(["pending", identity, version]);
  const [scope, setScope] = useState<{ identity: string; version: number; key: string } | null>(null);
  const currentScopeKey = visible && scope?.identity === identity && scope.version === version ? scope.key : pendingKey;
  const data: ConversationWorkMapData = visible && result?.identity === identity && result.version === version && result.data.scopeKey === currentScopeKey && result.data.conversationId === sessionId ? result.data : { scopeKey: currentScopeKey, conversationId: sessionId, state: "loading" };
  const open = useCallback(() => { if (eligible) { setOpenedFor(identity); setVersion((v) => v + 1); } }, [eligible, identity]);
  const close = useCallback(() => { generation.current++; setOpenedFor(null); }, []);
  const refresh = useCallback(() => { if (visible) setVersion((v) => v + 1); }, [visible]);
  useEffect(() => {
    if (!visible || !userId) return;
    const abort = new AbortController();
    const request = ++generation.current;
    const valid = () => !abort.signal.aborted && generation.current === request && getNetworkEndpointRoutingRevision() === routingRevision && getAuthScopeRevision() === authRevision;
    void (async () => {
      try {
        const [server, actorRead] = await Promise.all([getConfiguredApiServerFingerprint(), readAuthActor()]);
        if (!valid()) return;
        if (!actorRead.readable || !actorRead.actor || actorRead.actor.user_id !== userId || actorRead.actor.serverFingerprint !== server) throw new Error("Identity unavailable");
        const role = actorRead.actor.role;
        const key = JSON.stringify([server, userId, sessionId]);
        setScope({ identity, version, key });
        setResult({ identity, version, data: { scopeKey: key, conversationId: sessionId, state: "loading" } });
        const raw = await fetchApiAtServerFingerprint<unknown>(server, `/api/conversations/${encodeURIComponent(sessionId)}/delegation-map`, { method: "GET", signal: abort.signal });
        const dto = parseConversationDelegationMap(raw, sessionId);
        const [currentServer, currentActor] = await Promise.all([getConfiguredApiServerFingerprint(), readAuthActor()]);
        if (!valid() || currentServer !== server || !currentActor.readable || currentActor.actor?.user_id !== userId || currentActor.actor.serverFingerprint !== server || currentActor.actor.role !== role) return;
        setResult({ identity, version, data: projectConversationDelegationMap(dto, key) });
      } catch {
        if (valid()) {
          const key = JSON.stringify(["error", identity, version]);
          setScope({ identity, version, key });
          setResult({ identity, version, data: { scopeKey: key, conversationId: sessionId, state: "error" } });
        }
      }
    })();
    return () => { abort.abort(); generation.current++; };
  }, [visible, identity, version, userId, sessionId, routingRevision, authRevision]);
  return { visible, open, close, refresh, currentScopeKey, data };
}
