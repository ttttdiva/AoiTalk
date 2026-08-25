"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { trpgPlayApi } from "@/lib/trpg/play-api";

export function TrpgPlayOpenButton({
  workId,
  workTitle,
}: {
  workId: string;
  workTitle?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const handleOpen = async () => {
    setBusy(true);
    try {
      const session = await trpgPlayApi.createSession({
        work_id: workId,
        gm_mode: "ai",
        title: workTitle ? `${workTitle} の卓` : undefined,
      });
      router.push(`/trpg/play/${session.id}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void handleOpen()}>
      {busy ? "作成中…" : "この作品で卓を開く"}
    </Button>
  );
}
