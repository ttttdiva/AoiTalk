"use client";

import Link from "next/link";
import {
  ArrowUpRight,
  BookOpen,
  FileText,
  Link2,
  LockKeyhole,
  Search,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { TrpgWorkspaceShell } from "@/components/trpg/trpg-workspace";

const referenceAreas = [
  {
    title: "ルール",
    description: "判定、技能、戦闘など、登録済みのルール項目を検索します。",
    icon: BookOpen,
  },
  {
    title: "クリーチャー",
    description: "ルールセットに紐づくクリーチャー資料を確認します。",
    icon: Search,
  },
  {
    title: "関連資料",
    description: "メカニクスに関連する資料リンクを参照します。",
    icon: Link2,
  },
];

export default function TrpgPage() {
  return (
    <TrpgWorkspaceShell>
      <div className="min-h-full bg-background text-foreground" data-trpg-capability="read-only">
        <div className="mx-auto max-w-[1480px] p-5 sm:p-6">
          <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-primary">
                TRPG · Reference Library
              </div>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-[28px]">
                TRPG資料ポータル
              </h1>
              <p className="mt-2 max-w-2xl text-sm leading-5 text-muted-foreground">
                登録済みのルールセットと参照資料を、読み取り専用で確認できます。
                作品の本文や設定の編集は Story Studio に集約されています。
              </p>
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-[4px] border border-primary/35 bg-primary/10 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">
              <LockKeyhole className="size-3.5" aria-hidden="true" />
              Read only
            </span>
          </header>

          <div className="mt-5 grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
            <section className="overflow-hidden rounded-lg border border-border bg-card" aria-labelledby="reference-areas-title">
              <div className="flex items-center justify-between gap-3 border-b border-border bg-muted/25 px-4 py-3">
                <div className="flex items-center gap-2">
                  <BookOpen className="size-4 text-primary" aria-hidden="true" />
                  <h2 id="reference-areas-title" className="text-sm font-semibold">
                    参照できる資料
                  </h2>
                </div>
                <span className="rounded-[4px] border border-border bg-background px-2 py-1 text-[10px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                  read only
                </span>
              </div>

              <div className="grid gap-px bg-border sm:grid-cols-3">
                {referenceAreas.map(({ title, description, icon: Icon }) => (
                  <Link
                    key={title}
                    href="/trpg/reference"
                    className="group min-h-36 bg-card p-4 transition-colors hover:bg-muted/35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <span className="flex size-8 items-center justify-center rounded-[4px] border border-border bg-muted/50 text-primary">
                        <Icon className="size-4" aria-hidden="true" />
                      </span>
                      <ArrowUpRight className="size-3.5 text-muted-foreground transition-colors group-hover:text-primary" aria-hidden="true" />
                    </div>
                    <h3 className="mt-4 text-sm font-semibold group-hover:text-primary">{title}</h3>
                    <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{description}</p>
                  </Link>
                ))}
              </div>
            </section>

            <aside className="rounded-lg border border-border bg-card" aria-labelledby="boundary-title">
              <div className="flex items-center gap-2 border-b border-border bg-muted/25 px-4 py-3">
                <LockKeyhole className="size-4 text-primary" aria-hidden="true" />
                <h2 id="boundary-title" className="text-sm font-semibold">この画面の境界</h2>
              </div>
              <div className="space-y-4 p-4">
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">Reference surface</p>
                  <p className="mt-1.5 text-sm leading-5 text-foreground">
                    ルールセットを選び、現在登録されている資料を検索・閲覧します。
                  </p>
                </div>
                <div className="border-t border-border pt-4">
                  <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">Writing surface</p>
                  <p className="mt-1.5 text-sm leading-5 text-muted-foreground">
                    作品の本文、分岐、設定資料は Story Studio で管理します。
                  </p>
                </div>
                <Button
                  nativeButton={false}
                  variant="outline"
                  size="sm"
                  className="w-full justify-between"
                  render={<Link href="/trpg/reference" />}
                >
                  資料を検索
                  <Search className="size-3.5" aria-hidden="true" />
                </Button>
              </div>
            </aside>
          </div>

          <section className="mt-4 grid gap-4 md:grid-cols-2" aria-label="関連する実在機能">
            <Link
              href="/scenarios?kind=trpg"
              className="group flex min-h-28 items-center justify-between gap-4 rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/50 hover:bg-muted/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex min-w-0 items-center gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-[4px] border border-border bg-muted/50 text-primary">
                  <FileText className="size-4" aria-hidden="true" />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold group-hover:text-primary">Story StudioでTRPG作品を開く</span>
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">TRPGの作品編集と履歴を、現在のStory Studioで続けます。</span>
                </span>
              </span>
              <ArrowUpRight className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" aria-hidden="true" />
            </Link>

            <Link
              href="/scenarios/library?tab=rules"
              className="group flex min-h-28 items-center justify-between gap-4 rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/50 hover:bg-muted/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="flex min-w-0 items-center gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-[4px] border border-border bg-muted/50 text-primary">
                  <BookOpen className="size-4" aria-hidden="true" />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold group-hover:text-primary">共有ルールブックを見る</span>
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">共有ライブラリにあるルールブックへ移動します。</span>
                </span>
              </span>
              <ArrowUpRight className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" aria-hidden="true" />
            </Link>
          </section>
        </div>
      </div>
    </TrpgWorkspaceShell>
  );
}
