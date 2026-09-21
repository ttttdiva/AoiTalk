"use client";

import { ChevronRight, ShieldCheck, Sparkles } from "lucide-react";
import type {
  ContextAuthorizedReference,
  ContextCapabilities,
  ContextManifestInspector,
  ContextSnapshotBinding,
} from "@/lib/chat-api";

function shortHash(value: string | null | undefined): string {
  const text = String(value || "").trim();
  if (!text) return "—";
  if (text.length <= 18) return text;
  return `${text.slice(0, 10)}…${text.slice(-6)}`;
}

function statusLabel(status: string): string {
  switch (status) {
    case "active":
      return "使用中";
    case "deferred":
      return "保留";
    case "failed":
      return "取得失敗";
    default:
      return status;
  }
}

function formatVersion(value: string | number | null | undefined): string {
  return value == null || value === "" ? "—" : String(value);
}

function formatCount(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}

function ManifestSection({
  title,
  count,
  children,
  open = false,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
  open?: boolean;
}) {
  return (
    <details className="group border-t border-border-subtle px-3 py-2" open={open}>
      <summary className="flex cursor-pointer list-none items-center gap-1 text-[11px] font-semibold text-text-secondary [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden="true" />
        <span>{title}</span>
        {count != null && <span className="ml-auto font-mono text-[10px] text-text-secondary">{count}</span>}
      </summary>
      <div className="mt-2 space-y-1.5">{children}</div>
    </details>
  );
}

/**
 * Structured, hash-only view of one persisted ContextManifest.
 *
 * This component intentionally does not accept/render the canonical Manifest
 * object.  The API keeps that object available to trusted callers, but the
 * chat UI receives the server's explicit inspector projection so prompt text,
 * IDs, tool arguments/results, and provider reasoning cannot become a second
 * display channel.
 */
export function WorkIntelligenceInspector({
  inspector,
  capabilities,
  binding,
  authorizedReferences,
}: {
  inspector?: ContextManifestInspector | null;
  capabilities?: ContextCapabilities | null;
  binding?: ContextSnapshotBinding | null;
  authorizedReferences?: ContextAuthorizedReference[];
}) {
  if (!inspector && !capabilities?.executed?.length && !(authorizedReferences?.length)) return null;

  const categories = inspector?.categories ?? [];
  const layers = inspector?.layers ?? [];
  const resources = inspector?.resources ?? [];
  const evidence = inspector?.evidence ?? [];
  const policies = inspector?.policy_decisions ?? [];
  const requests = inspector?.requests ?? [];
  const work = inspector?.work_intelligence;
  const omissionCounts = inspector?.omission_reason_counts ?? {};
  const freshness = work?.freshness ?? {};
  const workEvidence = work?.evidence ?? [];
  const workRelations = work?.relations ?? [];
  const workItems = work?.items ?? [];

  const safeReferenceLabel = (value: string): string => {
    const normalized = value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
    if (!normalized || /(?:api[_ -]?key|bearer\s|password|secret|token\s*[:=]|sk-[a-z0-9])/i.test(normalized)) {
      return "認可済み参照";
    }
    return normalized.slice(0, 180);
  };

  const safeReferenceHref = (value: string | null | undefined): string | null => {
    if (!value || !value.startsWith("/")) return null;
    if (value.startsWith("//") || /[\r\n]/.test(value)) return null;
    return value;
  };

  const safeReferenceToken = (value: string | null | undefined): string | null => {
    if (!value) return null;
    const normalized = value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
    if (!normalized || /(?:api[_ -]?key|bearer\s|password|secret|token\s*[:=]|sk-[a-z0-9])/i.test(normalized)) {
      return null;
    }
    return normalized.slice(0, 120);
  };

  return (
    <section
      className="mx-3 mb-3 overflow-hidden rounded-md border border-border-subtle bg-surface-slate/60"
      aria-label="Work Intelligence Inspector"
      data-testid="work-intelligence-inspector"
    >
      <div className="flex items-center gap-1.5 px-3 py-2">
        <Sparkles className="size-3.5 text-primary" aria-hidden="true" />
        <h3 className="text-[11px] font-semibold">根拠インスペクター</h3>
        {inspector?.mode && (
          <span className="ml-auto rounded border border-border-subtle px-1.5 py-0.5 text-[9px] text-text-secondary">
            {inspector.mode}
          </span>
        )}
      </div>

      {binding && (
        <div className="border-t border-border-subtle px-3 py-2 text-[10px] text-text-secondary">
          <div className="flex items-center gap-1.5 font-medium text-on-surface">
            <ShieldCheck className="size-3 text-primary" aria-hidden="true" />
            この応答に紐付いた観測
          </div>
          <p className="mt-1">
            {binding.active_branch ? "アクティブブランチのassistant応答に紐付け済み" : "アクティブブランチ外のため非表示"}
          </p>
        </div>
      )}

      {inspector && (
        <>
          <div className="grid grid-cols-2 gap-1 border-t border-border-subtle px-3 py-2 text-[10px] text-text-secondary">
            <span>Manifest {formatVersion(inspector.producer_version)}</span>
            <span className="text-right">hash {shortHash(inspector.manifest_hash)}</span>
            {inspector.subject?.include_project_context != null && (
              <span>
                Project Context {inspector.subject.include_project_context ? "ON" : "OFF"}
              </span>
            )}
            {inspector.bundle_char_budget != null && (
              <span className="text-right">予算 {formatCount(inspector.bundle_char_budget)}文字</span>
            )}
          </div>

          {categories.length > 0 && (
            <ManifestSection title="カテゴリ（使用 / 保留 / 失敗）" count={categories.length} open>
              <ul className="space-y-1" aria-label="コンテキストカテゴリ">
                {categories.slice(0, 16).map((category, index) => (
                  <li key={`${category.category}-${index}`} className="flex items-center gap-2 text-[10px]">
                    <span className="min-w-0 flex-1 truncate">{category.category}</span>
                    <span className={category.status === "failed" ? "text-destructive" : category.status === "deferred" ? "text-text-secondary" : "text-primary"}>
                      {statusLabel(category.status)}
                    </span>
                    {(category.tokens != null || category.percentage != null) && (
                      <span className="shrink-0 font-mono text-text-secondary">
                        {category.tokens != null ? `${formatCount(category.tokens)}t` : `${Math.round(category.percentage ?? 0)}%`}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {layers.length > 0 && (
            <ManifestSection title="選択レイヤー" count={layers.length}>
              <ul className="space-y-1.5" aria-label="コンテキストレイヤー">
                {layers.slice(0, 32).map((layer, index) => (
                  <li key={`${layer.category}-${index}`} className="rounded border border-border-subtle/70 px-2 py-1.5 text-[10px]">
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate">{layer.category}</span>
                      <span className={layer.status === "failed" ? "text-destructive" : layer.status === "deferred" ? "text-text-secondary" : "text-primary"}>
                        {statusLabel(layer.status)}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-text-secondary">
                      {layer.source && <span>{layer.source}</span>}
                      {layer.inclusion_reason && <span>{layer.inclusion_reason}</span>}
                      {(layer.retrieved_chars != null || layer.selected_chars != null) && (
                        <span>
                          {formatCount(layer.selected_chars)} / {formatCount(layer.retrieved_chars)}文字
                        </span>
                      )}
                      {layer.truncated && <span>budget clip</span>}
                    </div>
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {resources.length > 0 && (
            <ManifestSection title="参照リソース" count={resources.length}>
              <ul className="space-y-1" aria-label="参照リソース">
                {resources.slice(0, 24).map((resource, index) => (
                  <li key={`${resource.ref_hash}-${index}`} className="min-w-0 text-[10px]">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="shrink-0 rounded border border-border-subtle px-1 py-0.5">{resource.kind}</span>
                      <span className="min-w-0 flex-1 truncate font-mono text-text-secondary" title="認可には使わない相関ハッシュ">{shortHash(resource.ref_hash)}</span>
                      {resource.relation && <span className="shrink-0 text-text-secondary">{resource.relation}</span>}
                    </div>
                    {(resource.source || resource.version != null || resource.freshness || resource.supersedes_ref_hash) && (
                      <div className="mt-0.5 flex flex-wrap gap-x-2 text-text-secondary">
                        {resource.source && <span>{resource.source}</span>}
                        {resource.version != null && <span>v{formatVersion(resource.version)}</span>}
                        {resource.freshness && <span>fresh {formatVersion(resource.freshness)}</span>}
                        {resource.supersedes_ref_hash && <span>supersedes {shortHash(resource.supersedes_ref_hash)}</span>}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {evidence.length > 0 && (
            <ManifestSection title="根拠ソース" count={evidence.length}>
              <ul className="space-y-1" aria-label="Manifest根拠ソース">
                {evidence.slice(0, 24).map((item, index) => (
                  <li key={`${item.locator_hash}-${index}`} className="flex min-w-0 items-center gap-2 text-[10px]">
                    <span className="shrink-0">{item.kind}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-text-secondary">{shortHash(item.locator_hash)}</span>
                    {item.source_type && <span className="shrink-0 text-text-secondary">{item.source_type}</span>}
                    {item.version != null && <span className="shrink-0 text-text-secondary">v{formatVersion(item.version)}</span>}
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {policies.length > 0 && (
            <ManifestSection title="ポリシー判断" count={policies.length}>
              <ul className="space-y-1" aria-label="ポリシー判断">
                {policies.slice(0, 24).map((policy, index) => (
                  <li key={`${policy.decision_ref_hash}-${index}`} className="flex min-w-0 items-center gap-2 text-[10px]">
                    <span className="min-w-0 flex-1 truncate">{policy.authority} / {policy.scope}</span>
                    <span className={policy.decision === "allow" ? "text-primary" : "text-text-secondary"}>{policy.decision}</span>
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {requests.length > 0 && (
            <ManifestSection title="モデルリクエスト" count={requests.length}>
              <ul className="space-y-1" aria-label="モデルリクエスト">
                {requests.slice(-8).map((request, index) => (
                  <li key={`${request.request_hash}-${index}`} className="flex min-w-0 items-center gap-2 text-[10px]">
                    <span className="shrink-0">#{request.request_index ?? index + 1}</span>
                    <span className="min-w-0 flex-1 truncate">{request.observed_provider || "provider"}</span>
                    {request.input_tokens != null && <span className="shrink-0 font-mono text-text-secondary">{formatCount(request.input_tokens)}t</span>}
                    <span className="shrink-0 font-mono text-text-secondary">{shortHash(request.request_hash)}</span>
                  </li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {work && (workItems.length > 0 || workEvidence.length > 0 || workRelations.length > 0) && (
            <ManifestSection title="Work Intelligence" count={workItems.length + workEvidence.length}>
              <div className="space-y-1.5 text-[10px]">
                {workItems.length > 0 && (
                  <ul className="space-y-1" aria-label="Work Intelligence項目">
                    {workItems.slice(0, 12).map((item, index) => (
                      <li key={`${item.ref_hash}-${index}`} className="flex min-w-0 items-center gap-2">
                        <span className="min-w-0 flex-1 truncate">{item.kind}</span>
                        {item.status && <span className="shrink-0">{statusLabel(item.status)}</span>}
                        {item.version != null && <span className="shrink-0 text-text-secondary">v{formatVersion(item.version)}</span>}
                        {item.freshness && <span className="shrink-0 text-text-secondary">{formatVersion(item.freshness)}</span>}
                        {item.advisory_conflict && <span className="shrink-0 text-warning">conflict</span>}
                        {item.uncertain && <span className="shrink-0 text-text-secondary">uncertain</span>}
                      </li>
                    ))}
                  </ul>
                )}
                {workEvidence.length > 0 && (
                  <p>根拠 {workEvidence.length}件 · {workEvidence.slice(0, 8).map((item) => `${item.kind}${item.source_type ? ` (${item.source_type})` : ""}${item.freshness ? ` ${formatVersion(item.freshness)}` : ""}`).join(" / ")}</p>
                )}
                {workRelations.length > 0 && <p>関係 {workRelations.length}件</p>}
                {Object.keys(freshness).length > 0 && <p className="text-text-secondary">freshness: {Object.entries(freshness).slice(0, 4).map(([key, value]) => `${key}=${formatVersion(value)}`).join(" · ")}</p>}
              </div>
            </ManifestSection>
          )}

          {Object.keys(omissionCounts).length > 0 && (
            <ManifestSection title="省略理由" count={Object.keys(omissionCounts).length}>
              <ul className="space-y-1 text-[10px] text-text-secondary" aria-label="省略理由">
                {Object.entries(omissionCounts).slice(0, 16).map(([reason, count]) => (
                  <li key={reason} className="flex justify-between gap-2"><span className="truncate">{reason}</span><span className="shrink-0 font-mono">{count}</span></li>
                ))}
              </ul>
            </ManifestSection>
          )}

          {authorizedReferences && authorizedReferences.length > 0 && (
            <ManifestSection title="現在アクセス可能な根拠" count={authorizedReferences.length} open>
              <ul className="space-y-1.5" aria-label="現在アクセス可能な根拠">
                {authorizedReferences.slice(0, 8).map((reference, index) => {
                  const href = safeReferenceHref(reference.href);
                  const body = (
                    <>
                      <span className="min-w-0 flex-1 truncate">{safeReferenceLabel(reference.label)}</span>
                      <span
                        aria-hidden="true"
                        className="shrink-0 font-mono text-text-secondary"
                        title="認可には使わない相関ハッシュ"
                      >
                        {shortHash(reference.ref_hash)}
                      </span>
                    </>
                  );
                  return (
                    <li key={`${reference.ref_hash}-${index}`} className="min-w-0 text-[10px]">
                      {href ? (
                        <a href={href} className="flex min-w-0 items-center gap-2 hover:underline" rel="noreferrer">
                          {body}
                        </a>
                      ) : (
                        <div className="flex min-w-0 items-center gap-2">{body}</div>
                      )}
                      <div className="mt-0.5 flex flex-wrap gap-x-2 text-text-secondary">
                        {safeReferenceToken(reference.kind) && <span>{safeReferenceToken(reference.kind)}</span>}
                        {safeReferenceToken(reference.relation) && <span>{safeReferenceToken(reference.relation)}</span>}
                        {safeReferenceToken(reference.source) && <span>{safeReferenceToken(reference.source)}</span>}
                        {reference.freshness && <span>fresh {formatVersion(reference.freshness)}</span>}
                        {safeReferenceToken(reference.selection_reason) && <span>{safeReferenceToken(reference.selection_reason)}</span>}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </ManifestSection>
          )}
        </>
      )}

      {capabilities?.executed && capabilities.executed.length > 0 && (
        <ManifestSection title="実行されたCapability" count={capabilities.executed.length}>
          <ul className="space-y-1" aria-label="実行されたCapability">
            {capabilities.executed.slice(0, 24).map((entry, index) => (
              <li key={`${entry.name}-${index}`} className="flex items-center gap-2 text-[10px]">
                <span className="min-w-0 flex-1 truncate font-mono">{entry.name}</span>
                <span className="shrink-0 text-primary">{entry.reason}</span>
              </li>
            ))}
          </ul>
        </ManifestSection>
      )}
    </section>
  );
}
