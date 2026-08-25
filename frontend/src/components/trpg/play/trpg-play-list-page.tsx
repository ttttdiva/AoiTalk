"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TrpgWorkspaceShell } from "@/components/trpg/trpg-workspace";
import { trpgPlayApi, type TrpgPlaySession } from "@/lib/trpg/play-api";

export function TrpgPlayListPage() {
  const [sessions, setSessions] = useState<TrpgPlaySession[]>([]);
  const [loading, setLoading] = useState(true);
  const [joinCode, setJoinCode] = useState("");
  const [joinSessionId, setJoinSessionId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setSessions(await trpgPlayApi.listSessions());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleJoin = async () => {
    if (!joinSessionId.trim() || !joinCode.trim()) return;
    await trpgPlayApi.joinSession(joinSessionId.trim(), {
      invite_code: joinCode.trim().toUpperCase(),
      display_name: "プレイヤー",
    });
    window.location.href = `/trpg/play/${joinSessionId.trim()}`;
  };

  return (
    <TrpgWorkspaceShell playMode>
      <div className="mx-auto max-w-[1080px] p-5 sm:p-6">
        <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">TRPG 卓一覧</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Story Studio の TRPG 作品から卓を開くか、招待コードで参加できます。
            </p>
          </div>
          <Button
            nativeButton={false}
            variant="outline"
            render={<Link href="/scenarios?kind=trpg" />}
          >
            <Plus className="mr-2 size-4" aria-hidden="true" />
            Story Studio で作品を選ぶ
          </Button>
        </header>

        <section className="mt-6 rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold">招待コードで参加</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
            <div>
              <Label htmlFor="join-session-id">卓 ID</Label>
              <Input id="join-session-id" value={joinSessionId} onChange={(e) => setJoinSessionId(e.target.value)} />
            </div>
            <div>
              <Label htmlFor="join-code">招待コード</Label>
              <Input id="join-code" value={joinCode} onChange={(e) => setJoinCode(e.target.value)} />
            </div>
            <Button className="self-end" onClick={() => void handleJoin()}>参加</Button>
          </div>
        </section>

        <section className="mt-6">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              読み込み中…
            </div>
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground">参加中の卓はまだありません。</p>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border bg-card">
              {sessions.map((session) => (
                <li key={session.id} className="flex flex-wrap items-center justify-between gap-3 p-4">
                  <div>
                    <div className="font-medium">{session.title}</div>
                    <div className="text-xs text-muted-foreground">
                      {session.status} · {session.gm_mode} GM · コード {session.invite_code}
                    </div>
                  </div>
                  <Button
                    nativeButton={false}
                    size="sm"
                    render={<Link href={`/trpg/play/${session.id}`} />}
                  >
                    卓を開く
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </TrpgWorkspaceShell>
  );
}
