import * as Crypto from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import type { GraphContent } from "./agent-team-graphs-api";

export type CreateJournal = { kind: "create"; body: GraphContent & { idempotency_key: string } };
export type UpdateJournal = { kind: "update"; version: number; revisionId: string; contentHash: string };
export type GraphJournal = CreateJournal | UpdateJournal;
export function stableGraphJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableGraphJson).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${stableGraphJson(v)}`).join(",")}}`;
  return JSON.stringify(value);
}
export function normalizeGraphContent(value: GraphContent): GraphContent {
  const key = (v: string | null) => v === null ? null : v.trim();
  const d = value.definition;
  return { ...value, title: value.title.trim(), definition: { ...d, root_key: key(d.root_key),
    nodes: d.nodes.map(n => ({ ...n, key: n.key.trim() })),
    edges: d.edges.map(e => ({ ...e, key: e.key.trim(), source: e.source.trim(), target: e.target.trim(), output_key: key(e.output_key) })),
    groups: d.groups.map(g => ({ ...g, key: g.key.trim(), title: g.title.trim(), node_keys: [...new Set(g.node_keys.map(k => k.trim()))], leader_key: key(g.leader_key) })) } };
}
export const graphContentHash = (value: GraphContent) => Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, stableGraphJson(normalizeGraphContent(value)));
async function key(identity: string, scope: string, target: string): Promise<string> {
  return `teamgraph_${await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, JSON.stringify([identity, scope, target]))}`;
}
export async function readGraphJournal(identity: string, scope: string, target: string): Promise<GraphJournal | null> {
  const raw = await SecureStore.getItemAsync(await key(identity, scope, target));
  if (raw === null) return null;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object") throw new Error("invalid graph journal");
  const row = value as Record<string, unknown>;
  if (row.kind === "create" && row.body && typeof row.body === "object") {
    const body = row.body as Record<string, unknown>;
    if (typeof body.title === "string" && typeof body.idempotency_key === "string" && (typeof body.project_id === "string" || body.project_id === null) && body.definition && typeof body.definition === "object") return value as CreateJournal;
  }
  if (row.kind === "update" && Number.isInteger(row.version) && typeof row.revisionId === "string" && typeof row.contentHash === "string") return value as UpdateJournal;
  throw new Error("invalid graph journal");
}
const locks = new Map<string, Promise<void>>();
async function exclusive<T>(storageKey: string, action: () => Promise<T>): Promise<T> {
  const previous = locks.get(storageKey) ?? Promise.resolve();
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  const tail = previous.then(() => gate); locks.set(storageKey, tail);
  await previous;
  try { return await action(); } finally { release(); if (locks.get(storageKey) === tail) locks.delete(storageKey); }
}
export async function writeGraphJournal(identity: string, scope: string, target: string, value: GraphJournal): Promise<void> {
  const storageKey = await key(identity, scope, target);
  await exclusive(storageKey, async () => {
    const previous = await SecureStore.getItemAsync(storageKey);
    if (previous !== null && stableGraphJson(JSON.parse(previous)) !== stableGraphJson(value)) throw new Error("another operation is pending");
    await SecureStore.setItemAsync(storageKey, JSON.stringify(value));
  });
}
export async function clearGraphJournal(identity: string, scope: string, target: string, expected: GraphJournal): Promise<void> {
  const storageKey = await key(identity, scope, target);
  await exclusive(storageKey, async () => {
    const previous = await SecureStore.getItemAsync(storageKey);
    if (previous === null || stableGraphJson(JSON.parse(previous)) !== stableGraphJson(expected)) throw new Error("operation record changed");
    await SecureStore.deleteItemAsync(storageKey);
  });
}
