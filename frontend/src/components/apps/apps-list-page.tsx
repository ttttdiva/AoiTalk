"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useSWRConfig } from "swr";
import { toast } from "sonner";
import { ArrowLeft, Boxes, Loader2, Plus } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { AnimatedGridPattern } from "@/components/magicui/animated-grid-pattern";
import { BlurFade } from "@/components/magicui/blur-fade";
import { BorderBeam } from "@/components/magicui/border-beam";
import { appsApi } from "@/lib/apps-api";

export function AppsListPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const originProjectId = searchParams.get("project_id") || "";
  const showCreate = searchParams.get("create") === "1";
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const { mutate } = useSWRConfig();

  const createApp = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      const result = await appsApi.create({
        name: name.trim(),
        slug: slug.trim() || undefined,
        description: description.trim(),
        origin_project_id: originProjectId || null,
      });
      toast.success("Appを作成しました");
      const cacheRefreshes = await Promise.allSettled([
        mutate("/apps/workspace"),
        originProjectId ? mutate(`/apps/workspace?project_id=${encodeURIComponent(originProjectId)}`) : Promise.resolve(),
      ]);
      if (cacheRefreshes.some((result) => result.status === "rejected")) {
        toast.warning("Appは作成済みですが、一覧の更新に失敗しました。画面を再読み込みすると反映されます。");
      }
      const target = result.app.targets?.find((item) => item.target_key === result.app.default_target_key) || result.app.targets?.[0];
      const chatParams = new URLSearchParams({ app_id: result.app.id });
      if (originProjectId) chatParams.set("project_id", originProjectId);
      if (target) chatParams.set("app_target_id", target.id);
      router.push(`/chat?${chatParams.toString()}`);
    } catch (createError) {
      toast.error(createError instanceof Error ? createError.message : "Appの作成に失敗しました");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex min-h-full w-full items-start justify-center bg-background p-4 md:p-6">
      {showCreate ? (
        <BlurFade className="w-full max-w-2xl" duration={0.3} blur="4px" offset={8}>
          <Card className="relative w-full overflow-hidden border-border/80 bg-card/80">
            {creating ? (
              <BorderBeam
                className="pointer-events-none"
                duration={9}
                size={84}
                borderWidth={1}
                colorFrom="var(--primary)"
                colorTo="var(--chart-2)"
              />
            ) : null}
            <CardHeader className="flex-row items-start justify-between gap-4 space-y-0 border-b border-border/70">
              <div>
                <CardTitle className="flex items-center gap-2 text-base"><Plus className="size-4 text-primary" /> 新しいApp</CardTitle>
                <p className="mt-0.5 text-xs text-muted-foreground">用途と使い方をまとめて管理するAppを作成します。</p>
              </div>
              <Link href={originProjectId ? `/apps?project_id=${encodeURIComponent(originProjectId)}` : "/apps"} className={buttonVariants({ variant: "ghost", size: "sm" })}><ArrowLeft className="size-3.5" /> 戻る</Link>
            </CardHeader>
            <CardContent className="p-5">
              <form className="grid gap-4 md:grid-cols-2" onSubmit={createApp}>
                <div className="space-y-1.5 md:col-span-2">
                  <Label htmlFor="app-name">App名</Label>
                  <Input id="app-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例：申請ファイル変換" required autoFocus />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="app-slug">識別子（任意）</Label>
                  <Input id="app-slug" value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="request-file-converter" />
                </div>
                <div className="space-y-1.5 md:col-span-2">
                  <Label htmlFor="app-description">用途</Label>
                  <Textarea id="app-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="何を自動化するAppか" rows={4} />
                </div>
                {originProjectId && <p className="text-xs text-muted-foreground md:col-span-2">作成後はAppの詳細画面でTarget、ファイル、README、実行履歴を設定できます。Projectには必要なときだけ関連付けます。</p>}
                <div className="flex justify-end gap-2 md:col-span-2">
                  <Button type="button" variant="ghost" onClick={() => router.replace(originProjectId ? `/apps?project_id=${encodeURIComponent(originProjectId)}` : "/apps")}>キャンセル</Button>
                  <Button type="submit" disabled={creating || !name.trim()}>{creating && <Loader2 className="size-3.5 animate-spin" />} Appを作成</Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </BlurFade>
      ) : (
        <div className="relative isolate flex w-full max-w-2xl flex-col items-start justify-center overflow-hidden py-10 text-left md:py-16">
          <AnimatedGridPattern
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 z-0 h-full w-full opacity-70 [mask-image:radial-gradient(ellipse_at_center,black_0%,transparent_78%)]"
            numSquares={18}
            maxOpacity={0.05}
            duration={4.5}
            repeatDelay={1.5}
          />
          <div className="relative z-10 flex w-full flex-col items-start">
            <BlurFade duration={0.25} delay={0} blur="3px" offset={6}>
              <div className="mb-5 flex size-11 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary"><Boxes className="size-5" /></div>
            </BlurFade>
            <BlurFade duration={0.25} delay={0.04} blur="3px" offset={6}>
              <p className="text-xs font-semibold uppercase tracking-[0.12em] text-primary">Apps workspace</p>
            </BlurFade>
            <BlurFade duration={0.25} delay={0.08} blur="3px" offset={6}>
              <h1 className="mt-2 text-2xl font-semibold tracking-tight">Appを選択してください</h1>
            </BlurFade>
            <BlurFade duration={0.25} delay={0.12} blur="3px" offset={6}>
              <p className="mt-2 max-w-lg text-sm leading-5 text-muted-foreground">左の一覧からAppを選ぶと、用途、使い方、関連Project、実行履歴、リリースを確認できます。</p>
            </BlurFade>
            <BlurFade duration={0.25} delay={0.16} blur="3px" offset={6}>
              <Button size="sm" className="mt-5" onClick={() => router.push(originProjectId ? `/apps?project_id=${encodeURIComponent(originProjectId)}&create=1` : "/apps?create=1")}><Plus className="size-3.5" /> 新しいApp</Button>
            </BlurFade>
          </div>
        </div>
      )}
    </div>
  );
}
