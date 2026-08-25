"use client";

import { useRouter } from "next/navigation";
import { useCallback, type ReactNode } from "react";
import { toast } from "sonner";
import { useStoryWorkContext } from "@/components/story/shell/story-workspace-shell";

export type StoryKnowledgeNavTab = "cast" | "rules" | "notes";

const tabs: Array<{ id: StoryKnowledgeNavTab; label: string; href: (workId: string) => string }> = [
  { id: "cast", label: "登場人物", href: (workId) => `/scenarios/${encodeURIComponent(workId)}/cast` },
  { id: "rules", label: "ルールブック", href: (workId) => `/scenarios/${encodeURIComponent(workId)}/rules` },
  { id: "notes", label: "設定資料", href: (workId) => `/scenarios/${encodeURIComponent(workId)}/settings?tab=notes` },
];

type StoryKnowledgeNavProps = {
  workId: string;
  active: StoryKnowledgeNavTab | null;
  actions?: ReactNode;
};

/** 作品内 Knowledge（登場人物 / ルールブック / 設定資料）の共通ローカルナビ。 */
export function StoryKnowledgeNav({ workId, active, actions }: StoryKnowledgeNavProps) {
  const router = useRouter();
  const { flushAllScopes } = useStoryWorkContext();

  const handleNavigate = useCallback(
    async (href: string) => {
      const ok = await flushAllScopes();
      if (!ok) {
        toast.error("未保存の変更を保存できませんでした。内容を確認してから再度お試しください。");
        return;
      }
      router.push(href);
    },
    [flushAllScopes, router],
  );

  return (
    <div
      className="flex shrink-0 items-center justify-between gap-4 border-b border-border-subtle bg-background px-6 py-2"
      data-testid="story-knowledge-nav"
    >
      <div className="flex h-9 items-center gap-6" role="tablist" aria-label="作品ナレッジ">
        {tabs.map((tab) => {
          const isActive = active === tab.id;
          const className = `flex h-full items-center border-b-2 px-1 text-xs transition-colors ${
            isActive
              ? "border-primary font-medium text-primary"
              : "border-transparent text-on-surface-variant hover:text-on-surface"
          }`;
          if (isActive) {
            return (
              <span key={tab.id} role="tab" aria-selected className={className}>
                {tab.label}
              </span>
            );
          }
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={false}
              className={className}
              onClick={() => void handleNavigate(tab.href(workId))}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}
