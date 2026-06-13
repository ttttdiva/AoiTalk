"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { BookOpen, Database, ScrollText, Search, Shield, Skull } from "lucide-react";

type RuleReference = {
  id: string;
  title: string;
  rule_domain: string;
  mechanic_key: string;
  source_title: string;
  raw_excerpt: string;
  confidence: number;
  needs_review: boolean;
};

type CreatureReference = {
  id: string;
  name: string;
  entry_type: string;
  classification: string;
  summary: string;
  source_excerpt: string;
  confidence: string;
  ocr_status: string;
  characteristics: Record<string, number>;
  attacks: Array<Record<string, unknown>>;
  spells: Array<Record<string, unknown>>;
  san_loss: string;
  mechanic_links: string[];
  needs_review: boolean;
};

type ReferenceResponse = {
  rules: RuleReference[];
  creatures: CreatureReference[];
  count: number;
};

type ReferenceStats = {
  rule_items: number;
  creature_entries: number;
  creature_types: Record<string, number>;
  rule_domains: Record<string, number>;
  mechanics: Record<string, number>;
};

type RulesetProfile = {
  key: string;
  display_name: string;
  edition?: string;
  system_type?: string;
};

type RulesetResponse = {
  rulesets: RulesetProfile[];
  count: number;
};

type ViewMode = "creatures" | "rules" | "tomes" | "all";

const TOME_DOMAINS = new Set(["mythos_tomes", "occult_tomes"]);

const FALLBACK_RULESETS: RulesetProfile[] = [
  { key: "coc6", display_name: "クトゥルフ神話TRPG 6版", system_type: "coc" },
  { key: "coc7", display_name: "クトゥルフ神話TRPG 7版", system_type: "coc" },
  { key: "generic", display_name: "汎用TRPG", system_type: "generic" },
];

async function py<T>(path: string): Promise<T> {
  const res = await fetch(`/api/python-proxy${path}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
  return res.json();
}

function excerpt(value: string, limit = 520) {
  const text = (value || "").replace(/\n{3,}/g, "\n\n").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1).trim()}...`;
}

function statLine(stats: Record<string, number>) {
  return ["STR", "CON", "SIZ", "INT", "POW", "DEX", "APP", "EDU", "SAN"]
    .filter((key) => stats[key] !== undefined && stats[key] !== null)
    .map((key) => `${key} ${stats[key]}`)
    .join(" / ");
}

export default function TRPGReferencePage() {
  const detailScrollRef = useRef<HTMLDivElement | null>(null);
  const [rulesets, setRulesets] = useState<RulesetProfile[]>(FALLBACK_RULESETS);
  const [selectedRuleset, setSelectedRuleset] = useState("coc6");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<ViewMode>("creatures");
  const [creatureType, setCreatureType] = useState("");
  const [data, setData] = useState<ReferenceResponse>({ rules: [], creatures: [], count: 0 });
  const [stats, setStats] = useState<ReferenceStats | null>(null);
  const [selectedCreature, setSelectedCreature] = useState<CreatureReference | null>(null);
  const [selectedRule, setSelectedRule] = useState<RuleReference | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const selectedRulesetProfile = useMemo(
    () => rulesets.find((ruleset) => ruleset.key === selectedRuleset) ?? null,
    [rulesets, selectedRuleset],
  );
  const isCocRuleset =
    selectedRulesetProfile?.system_type === "coc" || selectedRuleset.startsWith("coc");
  const creatureModeLabel = isCocRuleset ? "神話生物" : "生物・データ";
  const searchPlaceholder = isCocRuleset
    ? "名称、SAN、攻撃、呪文など"
    : "名称、判定、技能、リソースなど";
  const tomeCount = (stats?.rule_domains?.mythos_tomes ?? 0) + (stats?.rule_domains?.occult_tomes ?? 0);
  const ruleOnlyCount = Math.max(0, (stats?.rule_items ?? 0) - tomeCount);

  const resetDetailScroll = useCallback(() => {
    detailScrollRef.current?.scrollTo({ top: 0 });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        query,
        kind: mode,
        limit: "40",
      });
      if (mode === "creatures" && creatureType) {
        params.set("creature_type", creatureType);
      }
      const result = await py<ReferenceResponse>(
        `/api/trpg/rulesets/${encodeURIComponent(selectedRuleset)}/references?${params.toString()}`,
      );
      setData(result);
      setSelectedCreature((current) => {
        if (current && result.creatures.some((item) => item.id === current.id)) return current;
        return result.creatures[0] ?? null;
      });
      setSelectedRule((current) => {
        if (current && result.rules.some((item) => item.id === current.id)) return current;
        return result.rules[0] ?? null;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "読み込みに失敗しました");
    } finally {
      setLoading(false);
    }
  }, [creatureType, mode, query, selectedRuleset]);

  useEffect(() => {
    py<RulesetResponse>("/api/trpg/rulesets")
      .then((result) => {
        if (result.rulesets.length > 0) {
          setRulesets(result.rulesets);
        }
      })
      .catch(() => {
        setRulesets(FALLBACK_RULESETS);
      });
  }, []);

  useEffect(() => {
    setStats(null);
    setSelectedCreature(null);
    setSelectedRule(null);
    setData({ rules: [], creatures: [], count: 0 });
    setCreatureType("");
    resetDetailScroll();
    py<ReferenceStats>(
      `/api/trpg/rulesets/${encodeURIComponent(selectedRuleset)}/reference-stats`,
    )
      .then(setStats)
      .catch((err) => setError(err instanceof Error ? err.message : "統計の取得に失敗しました"));
  }, [resetDetailScroll, selectedRuleset]);

  useEffect(() => {
    const timer = window.setTimeout(load, 180);
    return () => window.clearTimeout(timer);
  }, [load]);

  const activeDetail = useMemo(() => {
    if (mode === "rules" || mode === "tomes") return selectedRule;
    return selectedCreature ?? selectedRule;
  }, [mode, selectedCreature, selectedRule]);

  const selectCreature = useCallback(
    (item: CreatureReference) => {
      setSelectedCreature(item);
      resetDetailScroll();
    },
    [resetDetailScroll],
  );

  const selectRule = useCallback(
    (item: RuleReference) => {
      setSelectedRule(item);
      resetDetailScroll();
    },
    [resetDetailScroll],
  );

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 md:p-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-normal">
            <BookOpen className="size-6" />
            TRPG ルール資料
          </h1>
          <div className="mt-2 flex flex-wrap gap-2 text-sm">
            <Badge variant="outline">
              {selectedRulesetProfile?.display_name ?? selectedRuleset}
            </Badge>
            <Badge variant="secondary">
              ルール {stats ? ruleOnlyCount : "-"}
            </Badge>
            <Badge variant="secondary">
              書物 {stats ? tomeCount : "-"}
            </Badge>
            <Badge variant="secondary">
              {creatureModeLabel} {stats?.creature_entries ?? "-"}
            </Badge>
            {isCocRuleset && (
              <>
                <Badge variant="outline">
                  神格 {stats?.creature_types?.deity ?? 0}
                </Badge>
                <Badge variant="outline">
                  クリーチャー {stats?.creature_types?.creature ?? 0}
                </Badge>
              </>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-2 md:w-[520px]">
          <select
            value={selectedRuleset}
            onChange={(event) => setSelectedRuleset(event.target.value)}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            aria-label="TRPGシステム"
          >
            {rulesets.map((ruleset) => (
              <option key={ruleset.key} value={ruleset.key}>
                {ruleset.display_name || ruleset.key}
              </option>
            ))}
          </select>
          <div className="flex gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="pl-9"
                placeholder={searchPlaceholder}
              />
            </div>
            <Button variant="outline" onClick={load} disabled={loading}>
              <Search className="size-4" />
            </Button>
          </div>
          <div className="flex flex-wrap gap-2">
            {(["creatures", "rules", "tomes", "all"] as const).map((value) => (
              <Button
                key={value}
                type="button"
                size="sm"
                variant={mode === value ? "default" : "outline"}
                onClick={() => setMode(value)}
              >
                {value === "creatures"
                  ? creatureModeLabel
                  : value === "rules"
                    ? "ルール"
                    : value === "tomes"
                      ? "書物"
                      : "すべて"}
              </Button>
            ))}
            {mode === "creatures" && (
              <select
                value={creatureType}
                onChange={(event) => setCreatureType(event.target.value)}
                className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                aria-label="種別"
              >
                <option value="">全種別</option>
                <option value="creature">クリーチャー</option>
                <option value="deity">神格</option>
              </select>
            )}
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="grid min-h-0 gap-4 lg:h-[calc(100vh-190px)] lg:min-h-[560px] lg:grid-cols-[minmax(0,1fr)_420px]">
        <div className="flex min-h-[420px] flex-col overflow-hidden rounded-md border lg:h-full">
          <div className="flex shrink-0 items-center justify-between border-b px-3 py-2">
            <span className="text-sm text-muted-foreground">
              {loading ? "読み込み中" : `${data.count}件`}
            </span>
            <Database className="size-4 text-muted-foreground" />
          </div>
          <div className="min-h-0 flex-1 divide-y overflow-y-auto">
            {mode !== "rules" &&
              data.creatures.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => selectCreature(item)}
                  className="flex w-full flex-col gap-2 px-4 py-3 text-left hover:bg-muted/50"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Skull className="size-4 text-muted-foreground" />
                    <span className="font-medium">{item.name}</span>
                    <Badge variant={item.entry_type === "deity" ? "default" : "secondary"}>
                      {item.entry_type === "deity" ? "神格" : "クリーチャー"}
                    </Badge>
                    {item.san_loss && <Badge variant="outline">SAN {item.san_loss}</Badge>}
                    {item.needs_review && <Badge variant="outline">要確認</Badge>}
                  </div>
                  <p className="line-clamp-2 whitespace-pre-line text-sm text-muted-foreground">
                    {excerpt(item.summary || item.source_excerpt, 180)}
                  </p>
                </button>
              ))}
            {mode !== "creatures" &&
              data.rules.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => selectRule(item)}
                  className="flex w-full flex-col gap-2 px-4 py-3 text-left hover:bg-muted/50"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    {TOME_DOMAINS.has(item.rule_domain) ? (
                      <ScrollText className="size-4 text-muted-foreground" />
                    ) : (
                      <Shield className="size-4 text-muted-foreground" />
                    )}
                    <span className="font-medium">{item.title}</span>
                    <Badge variant="secondary">
                      {item.rule_domain === "mythos_tomes"
                        ? "魔道書"
                        : item.rule_domain === "occult_tomes"
                          ? "オカルト本"
                          : item.rule_domain}
                    </Badge>
                    {item.mechanic_key && <Badge variant="outline">{item.mechanic_key}</Badge>}
                    {item.needs_review && <Badge variant="outline">要確認</Badge>}
                  </div>
                  <p className="line-clamp-2 whitespace-pre-line text-sm text-muted-foreground">
                    {excerpt(item.raw_excerpt, 180)}
                  </p>
                </button>
              ))}
            {!loading && data.count === 0 && (
              <div className="px-4 py-10 text-center text-sm text-muted-foreground">
                該当なし
              </div>
            )}
          </div>
        </div>

        <Card className="min-h-[420px] gap-0 overflow-hidden py-0 lg:h-full">
          <CardContent
            ref={detailScrollRef}
            className="h-full space-y-4 overflow-y-auto p-4"
          >
            {activeDetail && "name" in activeDetail ? (
              <>
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg font-semibold">{activeDetail.name}</h2>
                    <Badge variant={activeDetail.entry_type === "deity" ? "default" : "secondary"}>
                      {activeDetail.entry_type === "deity" ? "神格" : "クリーチャー"}
                    </Badge>
                  </div>
                  {activeDetail.san_loss && <Badge variant="outline">SAN {activeDetail.san_loss}</Badge>}
                </div>
                {statLine(activeDetail.characteristics) && (
                  <div className="rounded-md bg-muted px-3 py-2 text-sm">
                    {statLine(activeDetail.characteristics)}
                  </div>
                )}
                <div className="whitespace-pre-line text-sm leading-6">
                  {excerpt(activeDetail.source_excerpt, 1400)}
                </div>
                <div className="flex flex-wrap gap-2">
                  {activeDetail.mechanic_links.map((link) => (
                    <Badge key={link} variant="outline">
                      {link}
                    </Badge>
                  ))}
                </div>
              </>
            ) : activeDetail && "title" in activeDetail ? (
              <>
                <div className="space-y-2">
                  <h2 className="text-lg font-semibold">{activeDetail.title}</h2>
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="secondary">
                      {activeDetail.rule_domain === "mythos_tomes"
                        ? "魔道書"
                        : activeDetail.rule_domain === "occult_tomes"
                          ? "オカルト本"
                          : activeDetail.rule_domain}
                    </Badge>
                    {activeDetail.mechanic_key && (
                      <Badge variant="outline">{activeDetail.mechanic_key}</Badge>
                    )}
                  </div>
                </div>
                <div className="whitespace-pre-line text-sm leading-6">
                  {excerpt(activeDetail.raw_excerpt, 1400)}
                </div>
              </>
            ) : (
              <div className="py-12 text-center text-sm text-muted-foreground">未選択</div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
