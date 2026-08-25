"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  FileText,
  Link2,
  Loader2,
  LockKeyhole,
  RefreshCcw,
  Search,
} from "lucide-react";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import { TrpgWorkspaceShell } from "@/components/trpg/trpg-workspace";

type Ruleset = {
  key?: string;
  name?: string;
  label?: string;
  display_name?: string;
  description?: string;
  edition?: string;
  system_type?: string;
};

type ReferenceItem = Record<string, unknown>;

type ReferenceBundle = {
  count?: number;
  rules?: ReferenceItem[];
  creatures?: ReferenceItem[];
  mechanic_links?: ReferenceItem[];
};

type RulesetsPayload = { rulesets?: Ruleset[] };

const EMPTY_RULESETS: Ruleset[] = [];
const EMPTY_BUNDLE: ReferenceBundle = {};

// 資料は読み取り専用の参照 UI。フォーカス/再接続などの自動再取得は行わない。
const REFERENCE_SWR_OPTIONS = {
  revalidateOnMount: true,
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  revalidateIfStale: false,
  shouldRetryOnError: false,
} as const;

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function itemText(item: ReferenceItem, ...keys: string[]): string {
  for (const key of keys) {
    const value = textValue(item[key]);
    if (value) return value;
  }
  return "資料項目";
}

function itemDescription(item: ReferenceItem): string {
  return itemText(item, "description", "summary", "content", "source_text", "text");
}

function rulesetName(ruleset: Ruleset): string {
  return ruleset.display_name ?? ruleset.name ?? ruleset.label ?? ruleset.key ?? "ルールセット";
}

function rulesetMeta(ruleset: Ruleset): string | null {
  const values = [ruleset.system_type, ruleset.edition].map(textValue).filter(Boolean);
  return values.length ? values.join(" · ") : null;
}

async function fetchJson<T>(url: string, failureMessage: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(failureMessage);
  return (await response.json()) as T;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : "資料を取得できませんでした";
}

const sectionDefinitions = [
  { key: "rules", label: "ルール", icon: BookOpen, description: "判定・技能・戦闘などの構造化されたルール項目" },
  { key: "creatures", label: "クリーチャー", icon: Search, description: "ルールセットに登録されたクリーチャー資料" },
  { key: "mechanic_links", label: "関連資料", icon: Link2, description: "メカニクスに紐づく参照リンク" },
] as const;

function ReferenceItemCard({ item, sectionKey }: { item: ReferenceItem; sectionKey: string }) {
  const metadataKeys = sectionKey === "creatures"
    ? ["entry_type", "source_title"]
    : ["rule_domain", "mechanic_key", "source_title"];
  const metadata = metadataKeys
    .map((key) => textValue(item[key]))
    .filter((value): value is string => Boolean(value));
  const sourceUrl = textValue(item.url) ?? textValue(item.source_url);

  return (
    <article className="min-w-0 rounded-[4px] border border-border bg-background/70 p-3 transition-colors hover:border-primary/45 hover:bg-muted/20">
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 truncate text-sm font-semibold">{itemText(item, "title", "name", "label", "key")}</h3>
        {sourceUrl ? (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 text-muted-foreground hover:text-primary"
            aria-label="関連資料を開く"
          >
            <ArrowUpRight className="size-3.5" aria-hidden="true" />
          </a>
        ) : null}
      </div>
      <p className="mt-1.5 line-clamp-3 text-xs leading-5 text-muted-foreground">{itemDescription(item)}</p>
      {metadata.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {metadata.map((value) => (
            <span key={value} className="rounded-[3px] border border-border bg-muted/35 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {value}
            </span>
          ))}
        </div>
      ) : null}
    </article>
  );
}

export default function TrpgReferencePage() {
  // 明示的に選ばれていない間は先頭のルールセットを使う（effect 内 setState を避ける）。
  const [selectedRulesetKey, setSelectedRulesetKey] = useState("");
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");

  const {
    data: rulesetsPayload,
    error: rulesetsError,
    isLoading: rulesetsLoading,
    mutate: mutateRulesets,
  } = useSWR<RulesetsPayload>(
    "trpg/rulesets",
    () =>
      fetchJson<RulesetsPayload>(
        "/api/python-proxy/trpg/rulesets",
        "ルールセットを取得できませんでした",
      ),
    REFERENCE_SWR_OPTIONS,
  );

  const rulesets = rulesetsPayload?.rulesets ?? EMPTY_RULESETS;
  const rulesetKey = selectedRulesetKey || rulesets[0]?.key || "";
  const activeRuleset = rulesets.find((ruleset) => ruleset.key === rulesetKey);

  const {
    data: bundle = EMPTY_BUNDLE,
    error: referencesError,
    isLoading: referencesLoading,
    isValidating: referencesValidating,
    mutate: mutateReferences,
  } = useSWR<ReferenceBundle>(
    rulesetKey ? ["trpg/references", rulesetKey, submittedQuery] : null,
    () => {
      const params = new URLSearchParams({ query: submittedQuery, kind: "all", limit: "40" });
      return fetchJson<ReferenceBundle>(
        `/api/python-proxy/trpg/rulesets/${encodeURIComponent(rulesetKey)}/references?${params.toString()}`,
        "資料を取得できませんでした",
      );
    },
    REFERENCE_SWR_OPTIONS,
  );

  const loading =
    rulesetsLoading || (rulesetKey !== "" && (referencesLoading || referencesValidating));
  const failure = rulesetsError ?? referencesError;
  const error = failure ? errorMessage(failure) : null;

  const sections = useMemo(
    () =>
      sectionDefinitions.map((definition) => ({
        ...definition,
        items: bundle[definition.key] ?? [],
      })),
    [bundle],
  );

  const retry = () => {
    if (rulesetsError) void mutateRulesets();
    if (rulesetKey) void mutateReferences();
  };

  return (
    <TrpgWorkspaceShell>
      <div className="min-h-full bg-background text-foreground" data-trpg-capability="read-only">
        <div className="mx-auto max-w-[1480px] p-5 sm:p-6">
          <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-primary">
                TRPG · Reference Library
              </div>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-[28px]">資料を参照</h1>
              <p className="mt-2 max-w-2xl text-sm leading-5 text-muted-foreground">
                ルールセットを選び、登録済みのルール・クリーチャー・関連資料を検索します。
                この画面から資料を編集することはできません。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-[4px] border border-primary/35 bg-primary/10 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">
                <LockKeyhole className="size-3.5" aria-hidden="true" />
                Read only
              </span>
              <Button nativeButton={false} variant="outline" size="sm" render={<Link href="/trpg" />}>
                <ArrowLeft className="size-3.5" aria-hidden="true" />
                TRPGホーム
              </Button>
            </div>
          </header>

          <section className="mt-5 rounded-lg border border-border bg-card" aria-labelledby="reference-controls-title">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-muted/25 px-4 py-3">
              <div className="flex items-center gap-2">
                <Search className="size-4 text-primary" aria-hidden="true" />
                <h2 id="reference-controls-title" className="text-sm font-semibold">参照条件</h2>
              </div>
              <span className="text-[11px] text-muted-foreground">
                {bundle.count !== undefined ? `${bundle.count.toLocaleString("ja-JP")}件` : "読み取り専用"}
              </span>
            </div>
            <div className="flex flex-col gap-3 p-4 lg:flex-row lg:items-end">
              <label className="flex min-w-52 flex-1 flex-col gap-1.5 text-xs font-medium text-muted-foreground">
                ルールセット
                <AppSelect
                  aria-label="ルールセット"
                  className="h-9 w-full rounded-[4px] border border-input bg-background px-3 text-sm text-foreground"
                  value={rulesetKey}
                  disabled={rulesetsLoading || rulesets.length === 0}
                  onChange={(event) => setSelectedRulesetKey(event.target.value)}
                >
                  {rulesets.map((ruleset) => (
                    <option key={ruleset.key} value={ruleset.key}>
                      {rulesetName(ruleset)}
                    </option>
                  ))}
                </AppSelect>
              </label>
              <form
                className="flex min-w-0 flex-[2] gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  setSubmittedQuery(query.trim());
                }}
              >
                <label className="sr-only" htmlFor="trpg-reference-query">資料を検索</label>
                <input
                  id="trpg-reference-query"
                  aria-label="資料を検索"
                  className="h-9 min-w-0 flex-1 rounded-[4px] border border-input bg-background px-3 text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-primary focus:ring-1 focus:ring-primary"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="ルール・クリーチャーを検索"
                />
                <Button type="submit" size="sm">
                  <Search className="size-3.5" aria-hidden="true" />
                  検索
                </Button>
              </form>
            </div>
            {activeRuleset ? (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-4 py-3 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{rulesetName(activeRuleset)}</span>
                {rulesetMeta(activeRuleset) ? <span>{rulesetMeta(activeRuleset)}</span> : null}
                {textValue(activeRuleset.description) ? <span className="basis-full lg:basis-auto">{activeRuleset.description}</span> : null}
              </div>
            ) : null}
          </section>

          {error ? (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive" role="alert">
              <span>{error}</span>
              <Button type="button" variant="outline" size="sm" onClick={retry}>
                <RefreshCcw className="size-3.5" aria-hidden="true" />
                再読み込み
              </Button>
            </div>
          ) : null}

          <div className="mt-4 space-y-4" aria-busy={loading}>
            {loading ? (
              <div className="flex items-center justify-center gap-2 rounded-lg border border-border bg-card px-4 py-12 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                資料を読み込んでいます…
              </div>
            ) : null}

            {!loading && !error && rulesetKey === "" ? (
              <div className="rounded-lg border border-dashed border-border bg-card px-4 py-12 text-center">
                <BookOpen className="mx-auto size-8 text-muted-foreground/60" aria-hidden="true" />
                <h2 className="mt-3 text-sm font-semibold">利用できるルールセットがありません</h2>
                <p className="mt-1.5 text-xs leading-5 text-muted-foreground">ルールセットが登録されると、ここから資料を参照できます。</p>
              </div>
            ) : null}

            {!loading && rulesetKey !== "" ? (
              <div className="grid gap-4 xl:grid-cols-2">
                {sections.map(({ key, label, icon: Icon, description, items }) => (
                  <section key={key} className="overflow-hidden rounded-lg border border-border bg-card" aria-labelledby={`trpg-section-${key}`}>
                    <div className="flex items-center justify-between gap-3 border-b border-border bg-muted/25 px-4 py-3">
                      <div className="flex min-w-0 items-center gap-2">
                        <Icon className="size-4 shrink-0 text-primary" aria-hidden="true" />
                        <div className="min-w-0">
                          <h2 id={`trpg-section-${key}`} className="truncate text-sm font-semibold">{label}</h2>
                          <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{description}</p>
                        </div>
                      </div>
                      <span className="rounded-[4px] border border-border bg-background px-2 py-1 text-[10px] font-medium tabular-nums text-muted-foreground">
                        {items.length}
                      </span>
                    </div>
                    {items.length ? (
                      <div className="grid gap-2 p-3 sm:grid-cols-2">
                        {items.map((item, index) => (
                          <ReferenceItemCard key={`${key}-${String(item.id ?? item.key ?? index)}`} item={item} sectionKey={key} />
                        ))}
                      </div>
                    ) : (
                      <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                        該当する資料はありません。
                      </div>
                    )}
                  </section>
                ))}
              </div>
            ) : null}
          </div>

          <footer className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <p className="text-xs text-muted-foreground">資料の編集とルールブック管理は各専用画面で行います。</p>
            <div className="flex flex-wrap gap-2">
              <Button nativeButton={false} variant="outline" size="sm" render={<Link href="/scenarios/library?tab=rules" />}>
                <BookOpen className="size-3.5" aria-hidden="true" />
                共有ルールブック
              </Button>
              <Button nativeButton={false} variant="ghost" size="sm" render={<Link href="/scenarios?kind=trpg" />}>
                <FileText className="size-3.5" aria-hidden="true" />
                Story Studio
              </Button>
            </div>
          </footer>
        </div>
      </div>
    </TrpgWorkspaceShell>
  );
}
