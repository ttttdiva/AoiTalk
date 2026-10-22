"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import type { Employee } from "@/lib/agent-employees-api";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { EmployeeStatus, employeeInputClass } from "./employee-fields";
import type { EmployeeProjection } from "./employee-observability";
import { EmployeeThumbnail, EmployeeThumbnailControl } from "./employee-thumbnail";
import styles from "./employee-workspace.module.css";

const searchable = (value: string) => value.normalize("NFKC").toLocaleLowerCase("ja");

export function EmployeeDirectory({ agents, projections, selected, loading, failed, onSelect, visible = true, onShowDetail, onThumbnailChanged }: {
  agents: Employee[];
  projections: EmployeeProjection[];
  selected: string | null;
  loading: boolean;
  failed: boolean;
  onSelect: (id: string) => void;
  visible?: boolean;
  onShowDetail?: () => void;
  onThumbnailChanged?: (id: string, artifactId: string | null) => Promise<void>;
}) {
  const [search, setSearch] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const browseScroll = useRef(0);
  const currentScroll = useRef(0);
  const selectedRef = useRef<HTMLButtonElement>(null);
  const wasVisible = useRef(visible);
  const needle = searchable(search.trim());
  const byId = new Map(projections.map(projection => [projection.id, projection]));
  const filtered = agents.filter(agent => !needle || searchable([
    agent.display_name, agent.slug, agent.id, byId.get(agent.id)?.job_title ?? "",
  ].join(" ")).includes(needle));

  // Restore the gallery if it was temporarily hidden by the creation form.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (visible && list) {
      list.scrollTop = currentScroll.current;
      if (!wasVisible.current) selectedRef.current?.focus({ preventScroll: true });
    }
    wasVisible.current = visible;
  }, [visible]);
  useLayoutEffect(() => {
    if (!needle && listRef.current) {
      listRef.current.scrollTop = browseScroll.current;
      currentScroll.current = listRef.current.scrollTop;
    }
  }, [needle]);

  return <section aria-label="Agent一覧" className={`min-h-40 min-w-0 flex-1 flex-col gap-3 ${visible ? "flex" : "hidden"}`}>
    <div className="flex shrink-0 flex-wrap items-center gap-2">
      <h2 className="sr-only">Agent一覧</h2>
      <div className="relative min-w-40 flex-1 sm:max-w-sm">
        <Search aria-hidden="true" className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
        <input type="search" aria-label="Agentを検索" placeholder="名前・役職・slug・IDで検索" className={`${employeeInputClass} pl-8 pr-9 [&::-webkit-search-cancel-button]:appearance-none`} value={search} onChange={event => {
          if (!needle) browseScroll.current = listRef.current?.scrollTop ?? 0;
          setSearch(event.target.value);
        }} />
        {search && <Button type="button" size="icon-sm" variant="ghost" aria-label="検索をクリア" className="absolute right-1 top-1" onClick={() => setSearch("")}><X aria-hidden="true" className="size-4" /></Button>}
      </div>
      <HelpPopover label="Agent一覧">名前・役職・slug・IDで検索できます。カードを選ぶと詳細を開きます。「一覧に戻る」で検索と一覧の位置を引き継ぎます。</HelpPopover>
      <span role="status" className="shrink-0 text-xs text-muted-foreground">{needle ? `${filtered.length} / ${agents.length}件` : `${agents.length}件`}</span>
      {selected && onShowDetail && <Button type="button" size="sm" variant="ghost" className="ml-auto" onClick={onShowDetail}>選択中の詳細へ</Button>}
    </div>
    <div ref={listRef} role="region" aria-label="Agentの検索結果" tabIndex={0} className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1 [scrollbar-gutter:stable]" onScroll={event => {
      if (!event.currentTarget.clientHeight) return;
      currentScroll.current = event.currentTarget.scrollTop;
      if (!needle) browseScroll.current = event.currentTarget.scrollTop;
    }}>
      {loading && <p className="p-3 text-sm">Agentを読み込み中…</p>}
      <div className="grid grid-cols-2 items-start gap-3 md:grid-cols-[repeat(auto-fill,minmax(11rem,1fr))]">
        {filtered.map(agent => {
          const projection = byId.get(agent.id);
          const uncertain = projection?.employee_observability?.uncertain_action_count;
          const jobTitle = projection?.job_title?.trim();
          const showJobTitle = jobTitle && searchable(jobTitle) !== searchable(agent.display_name.trim());
          return <article key={agent.id} className={`${styles.card} relative min-w-0 bg-card`}><button type="button" ref={selected === agent.id ? selectedRef : undefined} className={`group block w-full min-w-0 overflow-hidden rounded-[1rem] text-left ring-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected === agent.id ? "ring-primary" : "ring-border/60 hover:ring-primary/50"}`} onMouseDown={event => { event.preventDefault(); event.currentTarget.focus({ preventScroll: true }); }} onClick={() => onSelect(agent.id)} aria-pressed={selected === agent.id}>
            <span className="relative block"><EmployeeThumbnail agent={agent} className="aspect-[16/10] w-full" />{agent.state !== "active" && <span className="absolute right-2 top-2 rounded-full bg-card/95"><EmployeeStatus state={agent.state} /></span>}</span>
            <span className="block min-w-0 px-2.5 py-2">
              <span className="line-clamp-2 break-words text-[13px] font-medium leading-[18px]">{agent.display_name}</span>
              {showJobTitle && <span className="mt-0.5 block truncate text-xs text-muted-foreground">{jobTitle}</span>}
              {projection?.current_work?.state && <span className="mt-1 block text-xs text-muted-foreground">現在の仕事: {projection.current_work.state}</span>}
              {agent.state === "draft" && <span className="mt-1 block text-xs text-primary">下書きの設定を再開</span>}
              {uncertain != null && uncertain > 0 && <span className="mt-1 block text-xs font-medium text-amber-700 dark:text-amber-300">要確認 {uncertain}件 — 再実行禁止</span>}
            </span>
          </button>{onThumbnailChanged && <EmployeeThumbnailControl agent={agent} onChanged={onThumbnailChanged} className="absolute left-2 top-2" />}</article>;
        })}
      </div>
      {!loading && !failed && !agents.length && <p className="p-3 text-sm text-muted-foreground">Agentはまだ登録されていません。</p>}
      {!loading && !!agents.length && !filtered.length && <p className="p-3 text-sm text-muted-foreground">一致するAgentはありません。</p>}
    </div>
  </section>;
}
