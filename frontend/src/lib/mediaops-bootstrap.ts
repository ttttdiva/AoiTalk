/** Closed MediaOps import protocol. All authority remains in authenticated APIs. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type ObjectValue = { [key: string]: Json };
type Reference = { $ref: string; path: string[] };
type Endpoint = string | (string | Reference)[];
export type BootstrapAction = {
  key: string; endpoint: Endpoint; payload: ObjectValue; payload_hash: string;
  idempotency_key: string; read_path: Endpoint; unwrap: string | null;
  reuse: { path: string; unwrap?: string; match?: ObjectValue; list_match?: ObjectValue; explicit_id?: string | null } | null;
  rollback: ObjectValue | null;
};
export type BootstrapPlan = {
  schema: "mediaops-bootstrap/v1"; scope_key: string; expected_owner_user_id: string;
  mode: "research_only"; requested_image_model: string; model_substitution_allowed: false;
  role_bindings: Record<string, { agent_team_id: string; execution_profile_id: string; allowed_subagent_ids?: string[]; resolved_provider: string; resolved_model_id: string }>;
  actions: BootstrapAction[]; blocked: ObjectValue[]; plan_hash: string;
};
export type Binding = { id: string; payload_hash: string; readback_hash: string; created: boolean };
export type BootstrapJournal = {
  schema: "mediaops-bootstrap/v1"; scope_key: string; owner: string;
  bindings: Record<string, Binding>; pending?: { key: string; idempotency_key: string; payload_hash: string; id?: string } | null;
  live_runs: Record<string, { id: string; state: string }>;
};
export type BootstrapResult = { key: string; status: "create" | "reuse" | "applied" | "verified" | "not_applied"; id?: string };
export type ApiCall = (method: "GET" | "POST" | "PATCH", path: string, payload?: Json, key?: string) => Promise<Json>;
const MEDIA = "/api/operations/media";
const MAX_SIZE = 16 * 1024 * 1024;
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/u;
const HEX = /^[a-f0-9]{64}$/u;

export class BootstrapApiError extends Error {
  constructor(readonly status: number, readonly path: string) { super(`API ${status}: ${path}`); }
}
function object(value: unknown): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("オブジェクト形式が不正です");
  return value as ObjectValue;
}
function strings(value: unknown): string[] {
  if (!Array.isArray(value) || value.some(x => typeof x !== "string")) throw new Error("文字列リストが不正です");
  return value as string[];
}
export function canonical(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical((value as Record<string, unknown>)[k])}`).join(",")}}`;
  throw new Error("JSON以外の値は取り込めません");
}
export async function sha256(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonical(value));
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
}
export function stableProjection(value: Json): Json {
  if (Array.isArray(value)) return value.map(stableProjection);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([k]) => !["findings", "revisions", "route_warnings", "updated_at", "last_due_at", "next_due_at", "latest_run"].includes(k))
    .map(([k, v]) => [k, stableProjection(v)]));
  return value;
}
function expectedEndpoint(action: BootstrapAction): Endpoint {
  const kind = action.key.split(":", 1)[0];
  if (["source", "source-content", "research-source", "research-content"].includes(kind)) return "/api/docs/nodes";
  if (kind === "agent") return "/api/agents";
  if (kind === "revision") return ["/api/agents/", { $ref: "agent:" + action.key.slice(9), path: ["id"] }, "/revisions"];
  if (kind === "routine") return MEDIA + "/research-routines";
  if (kind === "import-run") return MEDIA + "/research-runs";
  if (kind === "candidate") return MEDIA + "/research-candidates";
  if (kind === "evidence") return [MEDIA + "/research-runs/", { $ref: "import-run:" + action.key.split(":")[1], path: ["id"] }, "/findings"];
  if (action.key === "automation:bootstrap-research") return MEDIA + "/automations";
  throw new Error("未対応の登録種別です");
}
function expectedRead(action: BootstrapAction): Endpoint {
  const kind = action.key.split(":", 1)[0];
  if (kind === "revision") return "/api/agent-revisions/{id}";
  if (kind === "evidence") return [MEDIA + "/research-runs/", { $ref: "import-run:" + action.key.split(":")[1], path: ["id"] }, "/findings?limit=100"];
  return String(expectedEndpoint(action)) + "/{id}";
}
function checkReferences(value: Json, prior: Set<string>, depth = 0): void {
  if (depth > 20) throw new Error("入れ子が深すぎます");
  if (Array.isArray(value)) { value.forEach(x => checkReferences(x, prior, depth + 1)); return; }
  if (!value || typeof value !== "object") return;
  if ("$ref" in value) {
    const path = strings(value.path);
    if (!prior.has(String(value.$ref)) || !["id", "current_revision.version"].includes(path.join(".")) || Object.keys(value).length !== 2) throw new Error("参照先が不正です");
    return;
  }
  for (const item of Object.values(value)) checkReferences(item, prior, depth + 1);
}

export async function parseBootstrapPlan(text: string): Promise<BootstrapPlan> {
  if (new TextEncoder().encode(text).length > MAX_SIZE) throw new Error("ファイルは16 MiB以下にしてください");
  const raw = object(JSON.parse(text));
  const { plan_hash: hash, ...body } = raw;
  if (!HEX.test(String(hash)) || hash !== await sha256(body)) throw new Error("planのハッシュが一致しません");
  if (raw.schema !== "mediaops-bootstrap/v1" || raw.mode !== "research_only" || raw.model_substitution_allowed !== false) throw new Error("research_onlyのplanのみ取り込めます");
  if (!/^[A-Za-z0-9_.:-]{1,80}$/u.test(String(raw.scope_key)) || !ID.test(String(raw.expected_owner_user_id))) throw new Error("ownerまたはscopeが不正です");
  if (!Array.isArray(raw.actions) || !raw.actions.length || raw.actions.length > 6000) throw new Error("登録件数が不正です");
  if (!Array.isArray(raw.blocked) || raw.blocked.some(x => !x || typeof x !== "object" || Array.isArray(x))) throw new Error("未解決項目の形式が不正です");
  if (!raw.role_bindings || typeof raw.role_bindings !== "object" || Array.isArray(raw.role_bindings) || Object.values(raw.role_bindings).some(x => !x || typeof x !== "object" || Array.isArray(x))) throw new Error("モデルbindingの形式が不正です");
  if (typeof raw.requested_image_model !== "string" || !raw.requested_image_model.trim()) throw new Error("指定画像モデルがありません");
  const plan = raw as unknown as BootstrapPlan;
  const prior = new Set<string>();
  for (const action of plan.actions) {
    if (typeof action.key !== "string" || prior.has(action.key) || action.key.length > 240) throw new Error("登録キーが不正または重複しています");
    if (canonical(action.endpoint) !== canonical(expectedEndpoint(action)) || canonical(action.read_path) !== canonical(expectedRead(action))) throw new Error("許可されていないAPIです");
    if (!/^mediaops:[a-f0-9]{56}$/u.test(action.idempotency_key) || action.payload_hash !== await sha256(action.payload)) throw new Error("登録内容のハッシュが不正です");
    const payload = object(action.payload);
    if (action.key.startsWith("revision:")) {
      const binding = plan.role_bindings[action.key.slice(9)];
      if (!binding || payload.agent_team_id !== binding.agent_team_id || payload.execution_profile_id !== binding.execution_profile_id
          || canonical(payload.allowed_subagent_ids || []) !== canonical(binding.allowed_subagent_ids || [])) throw new Error("revisionと確認済みモデルbindingが一致しません");
    }
    if (["actor_user_id", "owner_user_id", "permissions", "authorization", "role"].some(k => k in payload)) throw new Error("主体指定・権限変更は取り込めません");
    if (payload.enabled === true || (payload.state !== undefined && payload.state !== "draft")) throw new Error("自動起動・有効化は取り込めません");
    if (action.key.startsWith("automation:") && (payload.execution_mode !== "research_only" || Object.keys(object(payload.generation_action)).length)) throw new Error("生成・公開操作は取り込めません");
    if (action.key.startsWith("agent:") || action.key.startsWith("revision:")) {
      if (payload.idempotency_key !== action.idempotency_key) throw new Error("Agentの再実行キーが一致しません");
    }
    if (String(action.endpoint) === "/api/docs/nodes") {
      if (!ID.test(String(payload.id)) || payload.node_type !== "node") throw new Error("Docs identityが不正です");
      const doc = object(payload.body_json);
      if (Object.keys(doc).length && (doc.format !== "doc_block" || doc.block_type !== "code" || typeof doc.content !== "string" || typeof doc.label !== "string" || !payload.parent_id)) throw new Error("Docs原文は型付き子ノードに保存してください");
    }
    if (action.reuse) {
      const docs = action.endpoint === "/api/docs/nodes";
      const allowed = docs ? `/api/docs/nodes/${payload.id}` : action.key.startsWith("agent:") ? "/api/agents?limit=200" : "";
      if (action.reuse.path !== allowed || !allowed) throw new Error("既存照合APIが不正です");
    }
    checkReferences(action.payload, prior);
    if (Array.isArray(action.endpoint)) checkReferences(action.endpoint as Json, prior);
    prior.add(action.key);
  }
  return plan;
}

export const bootstrapApi: ApiCall = async (method, path, payload, key) => {
  // Callers pass only the closed registry's computed paths, never an input URL.
  if (!path.startsWith("/api/") || path.includes("..") || path.includes("://")) throw new Error("APIパスが不正です");
  const target = path === "/api/auth/status" ? path : "/api/python-proxy" + path;
  const response = await fetch(target, { method, credentials: "include", cache: "no-store", redirect: "error",
    headers: { "Accept": "application/json", ...(payload ? { "Content-Type": "application/json" } : {}), ...(key ? { "Idempotency-Key": key } : {}) },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
  if (!response.ok) throw new BootstrapApiError(response.status, path);
  return await response.json() as Json;
};
function unwrap(value: Json, name: string | null, id?: string): ObjectValue {
  if (name === "find_by_id") {
    const found = Array.isArray(value) ? value.find(x => object(x).id === id) : null;
    if (!found) throw new Error("保存結果が見つかりません");
    return object(found);
  }
  return object(name ? object(value)[name] : value);
}
function resolve(value: Json, results: Record<string, ObjectValue>): Json {
  if (Array.isArray(value)) return value.map(x => resolve(x, results));
  if (value && typeof value === "object") {
    if ("$ref" in value) {
      let current: Json = results[String(value.$ref)];
      for (const part of strings(value.path)) current = object(current)[part];
      if (current === undefined) throw new Error("前の登録結果がありません");
      return current;
    }
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolve(v, results)]));
  }
  return value;
}
function endpoint(value: Endpoint, results: Record<string, ObjectValue>, id?: string): string {
  const resolved = resolve(value as Json, results);
  const path = (Array.isArray(resolved) ? resolved.join("") : String(resolved)).replace("{id}", id || "");
  if (path.includes("undefined") || path.includes("..") || path.includes("://")) throw new Error("API参照が不正です");
  return path;
}
function matches(actual: ObjectValue, expected: ObjectValue): boolean {
  return Object.entries(expected).every(([k, v]) => canonical(actual[k] ?? null) === canonical(v));
}
export function newJournal(plan: BootstrapPlan): BootstrapJournal {
  return { schema: plan.schema, scope_key: plan.scope_key, owner: plan.expected_owner_user_id, bindings: {}, live_runs: {} };
}
export function journalKey(plan: BootstrapPlan): string { return `mediaops-bootstrap:${plan.expected_owner_user_id}:${plan.scope_key}`; }

export class BootstrapRunner {
  readonly results: Record<string, ObjectValue> = {};
  constructor(readonly plan: BootstrapPlan, readonly journal: BootstrapJournal, readonly api: ApiCall = bootstrapApi,
    readonly persist: (value: BootstrapJournal) => void = () => undefined) {
    if (journal.owner !== plan.expected_owner_user_id || journal.scope_key !== plan.scope_key || journal.schema !== plan.schema) throw new Error("再開記録のowner/scopeが一致しません");
  }
  async verifyOwner(): Promise<void> {
    const auth = object(await this.api("GET", "/api/auth/status"));
    const user = object(auth.user);
    if (auth.authenticated !== true || user.id !== this.plan.expected_owner_user_id || user.password_reset_required === true) throw new Error("認証済みownerがplanと一致しません。書き込みは行いません");
  }
  async verifyCatalog(): Promise<void> {
    const bindings = Object.entries(this.plan.role_bindings || {});
    if (!bindings.length) return;
    const catalog = object(await this.api("GET", "/api/agents/catalog"));
    for (const [role, binding] of bindings) {
      const teams = Array.isArray(catalog.teams) ? catalog.teams.map(object) : [];
      const team = teams.find(t => t.team_id === binding.agent_team_id);
      const profiles = team && Array.isArray(team.execution_profiles) ? team.execution_profiles.map(object) : [];
      const profile = profiles.find(p => p.profile_id === binding.execution_profile_id);
      const members = binding.allowed_subagent_ids || [];
      const routes = profile && Array.isArray(profile.routes) ? profile.routes.map(object).filter(r => members.includes(String(r.subagent_id))) : [];
      if (!members.length || routes.length !== members.length || routes.some(r => r.provider !== binding.resolved_provider || r.model !== binding.resolved_model_id)) throw new Error(`モデル経路が未解決または変更されています: ${role}`);
    }
  }
  async read(action: BootstrapAction, id: string): Promise<ObjectValue> {
    if (!ID.test(id)) throw new Error("登録IDが不正です");
    const path = endpoint(action.read_path, this.results, id);
    let value: ObjectValue | null = null;
    if (action.unwrap === "find_by_id") {
      for (let offset = 0; offset <= 6000; offset += 100) {
        const page = await this.api("GET", `${path}&offset=${offset}`);
        if (!Array.isArray(page)) throw new Error("Evidence一覧が不正です");
        value = page.map(object).find(x => x.id === id) || null;
        if (value || page.length < 100) break;
      }
    } else value = unwrap(await this.api("GET", path), action.unwrap, id);
    if (!value) throw new Error("保存結果が見つかりません");
    if (action.endpoint === "/api/docs/nodes") {
      const expected = object(resolve(action.payload, this.results));
      const fields = Object.fromEntries(["id", "title", "parent_id", "node_type", "body_json"].filter(k => k in expected).map(k => [k, expected[k]]));
      if (!matches(value, fields)) throw new Error("Docs原文または親ノードが変更されています");
    }
    if (value.id !== id || (value.owner_user_id && value.owner_user_id !== this.plan.expected_owner_user_id)) throw new Error("保存結果のidentity/ownerが一致しません");
    return value;
  }
  async reuse(action: BootstrapAction): Promise<ObjectValue | null> {
    const spec = action.reuse;
    if (!spec) return null;
    let raw: Json;
    try { raw = await this.api("GET", spec.path); }
    catch (error) { if (error instanceof BootstrapApiError && error.status === 404) return null; throw error; }
    if (spec.list_match) {
      const list = spec.unwrap ? object(raw)[spec.unwrap] : raw;
      if (!Array.isArray(list)) throw new Error("既存Agent一覧が不正です");
      const found = list.map(object).filter(x => spec.explicit_id ? x.id === spec.explicit_id : matches(x, spec.list_match!));
      if (found.length > 1) throw new Error("既存Agentを一意に選択できません");
      const selected = found[0];
      if (!selected) return null;
      if (selected.created_by !== this.plan.expected_owner_user_id) throw new Error("既存Agentの作成者が異なります");
      if (selected.state !== "draft" && !spec.explicit_id) throw new Error("稼働中Agentには明示的なbindingが必要です");
      return selected;
    }
    const found = unwrap(raw, spec.unwrap || null);
    if (!matches(found, spec.match || {})) throw new Error("既存資料と内容が異なります。上書きしません");
    return found;
  }
  async run(mode: "preview" | "apply" | "readback", progress: (rows: BootstrapResult[]) => void = () => undefined,
    stopped: () => boolean = () => false): Promise<BootstrapResult[]> {
    await this.verifyOwner();
    await this.verifyCatalog();
    const output: BootstrapResult[] = [];
    for (const action of this.plan.actions) {
      if (stopped()) break; // In-flight requests finish and are read back first.
      const previous = this.journal.bindings[action.key];
      if (previous) {
        if (previous.payload_hash !== action.payload_hash) throw new Error(`前回の内容から変更されています: ${action.key}`);
        const actual = await this.read(action, previous.id);
        if (previous.readback_hash !== await sha256(stableProjection(actual))) throw new Error(`登録後に内容が変更されています: ${action.key}`);
        this.results[action.key] = actual;
        output.push({ key: action.key, status: "verified", id: previous.id });
      } else if (mode === "readback") {
        output.push({ key: action.key, status: "not_applied" });
      } else {
        const pending = this.journal.pending;
        if (pending?.key === action.key && (pending.idempotency_key !== action.idempotency_key || pending.payload_hash !== action.payload_hash)) throw new Error("未照合の登録内容を変更できません");
        let current = pending?.key === action.key && pending.id ? await this.read(action, pending.id) : await this.reuse(action);
        const existed = Boolean(current) && pending?.key !== action.key;
        if (!current && mode === "preview") {
          this.results[action.key] = { id: "PLAN_ONLY", current_revision: { version: 1 } };
          output.push({ key: action.key, status: "create" });
        } else {
          if (!current) {
            if (this.journal.pending?.key && this.journal.pending.key !== action.key) throw new Error("結果未照合の登録があります");
            // On an explicit resume, the exact persisted key is replayed through
            // the API's transactional idempotency contract. No automatic retry.
            this.journal.pending = { key: action.key, idempotency_key: action.idempotency_key, payload_hash: action.payload_hash };
            this.persist(this.journal);
            current = unwrap(await this.api("POST", endpoint(action.endpoint, this.results), resolve(action.payload, this.results), action.idempotency_key),
              action.unwrap === "find_by_id" ? null : action.unwrap);
            this.journal.pending.id = String(current.id);
            this.persist(this.journal);
          }
          this.results[action.key] = current;
          const actual = await this.read(action, String(current.id));
          this.results[action.key] = actual;
          if (mode === "apply") {
            this.journal.bindings[action.key] = { id: String(actual.id), payload_hash: action.payload_hash,
              readback_hash: await sha256(stableProjection(actual)), created: !existed };
            this.journal.pending = null;
            this.persist(this.journal);
          }
          output.push({ key: action.key, status: existed ? "reuse" : "applied", id: String(actual.id) });
        }
      }
      progress([...output]);
    }
    return output;
  }
  async initialRun(): Promise<{ id: string; state: string }> {
    await this.verifyOwner();
    const action = this.plan.actions.find(a => a.key === "automation:bootstrap-research");
    const binding = this.journal.bindings["automation:bootstrap-research"];
    if (!action || !binding || !ID.test(binding.id)) throw new Error("初回Researchの登録が必要です");
    const current = object(await this.api("GET", `${MEDIA}/automations/${binding.id}`));
    if (object(current.current_revision).execution_mode !== "research_only" || await sha256(stableProjection(current)) !== binding.readback_hash) throw new Error("Automationの内容が変更されています");
    const trigger = "mediaops-bootstrap:" + action.payload_hash.slice(0, 40);
    const result = object(await this.api("POST", `${MEDIA}/automations/${binding.id}/runs`, { trigger_key: trigger, trigger_kind: "manual", execute: true }));
    if (!ID.test(String(result.id))) throw new Error("実行IDが不正です");
    const read = object(await this.api("GET", `${MEDIA}/automations/runs/${result.id}`));
    if (read.id !== result.id) throw new Error("実行結果のidentityが一致しません");
    const row = { id: String(read.id), state: String(read.state) };
    this.journal.live_runs[trigger] = row; this.persist(this.journal); return row;
  }
  async pauseCreated(): Promise<number> {
    await this.verifyOwner();
    let count = 0;
    for (const action of [...this.plan.actions].reverse()) {
      const binding = this.journal.bindings[action.key];
      if (!binding?.created) continue;
      // Do not trust rollback instructions in imported JSON.
      const agent = action.key.startsWith("agent:");
      const automation = action.key === "automation:bootstrap-research";
      if (!agent && !automation) continue;
      const actual = await this.read(action, binding.id);
      if ((agent && actual.state !== "active") || (automation && actual.enabled === false)) continue;
      if (await sha256(stableProjection(actual)) !== binding.readback_hash) throw new Error("変更された登録は自動停止しません");
      const path = agent ? `/api/agents/${binding.id}/state` : `${MEDIA}/automations/${binding.id}/enabled`;
      await this.api("PATCH", path, agent ? { state: "paused", expected_state: actual.state } : { enabled: false });
      const read = await this.read(action, binding.id);
      binding.readback_hash = await sha256(stableProjection(read)); this.persist(this.journal); count++;
    }
    return count;
  }
}
