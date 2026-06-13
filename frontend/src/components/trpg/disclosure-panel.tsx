"use client";

/* eslint-disable @next/next/no-img-element */

import {
  useCallback,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Archive, Eye, Image as ImageIcon, Lock } from "lucide-react";
import {
  generatedImageSrc,
  py,
  targetLabel,
  type Disclosure,
  type Participant,
  type Room,
} from "@/lib/trpg-room-utils";

// 右カラム: 開示情報パネル
export function DisclosurePanel({
  room,
  myParticipantId,
  disclosures,
  setDisclosures,
  loadDisclosures,
  visibleTargets,
}: {
  room: Room;
  myParticipantId: string;
  disclosures: Disclosure[];
  setDisclosures: Dispatch<SetStateAction<Disclosure[]>>;
  loadDisclosures: () => Promise<void> | void;
  visibleTargets: Participant[];
}) {
  const [disclosureFilter, setDisclosureFilter] = useState<"all" | "mine" | "images" | "items">("all");
  const [disclosureDraft, setDisclosureDraft] = useState({
    title: "",
    content: "",
    image: "",
    disclosure_type: "handout" as Disclosure["disclosure_type"],
    visibility: "public" as Disclosure["visibility"],
    target_participant_ids: [] as string[],
  });
  const [disclosureBusy, setDisclosureBusy] = useState(false);

  const filteredDisclosures = useMemo(() => {
    return disclosures.filter((item) => {
      if (disclosureFilter === "mine") {
        return item.visibility !== "public";
      }
      if (disclosureFilter === "images") {
        return item.disclosure_type === "image" || Boolean(item.image_url || item.image_path);
      }
      if (disclosureFilter === "items") {
        return item.disclosure_type === "item" || item.disclosure_type === "handout";
      }
      return true;
    });
  }, [disclosureFilter, disclosures]);

  const toggleDisclosureTarget = useCallback((targetId: string) => {
    setDisclosureDraft((prev) => {
      const exists = prev.target_participant_ids.includes(targetId);
      return {
        ...prev,
        target_participant_ids: exists
          ? prev.target_participant_ids.filter((id) => id !== targetId)
          : [...prev.target_participant_ids, targetId],
      };
    });
  }, []);

  const handleCreateDisclosure = useCallback(async () => {
    if (!room || !myParticipantId || !disclosureDraft.title.trim()) return;
    const image = disclosureDraft.image.trim();
    const payload = {
      creator_participant_id: myParticipantId,
      disclosure_type: disclosureDraft.disclosure_type,
      visibility: disclosureDraft.visibility,
      target_participant_ids:
        disclosureDraft.visibility === "private"
          ? disclosureDraft.target_participant_ids
          : [],
      title: disclosureDraft.title.trim(),
      content: disclosureDraft.content.trim(),
      image_url: /^https?:\/\//i.test(image) ? image : "",
      image_path: /^https?:\/\//i.test(image) ? "" : image,
    };
    setDisclosureBusy(true);
    try {
      const created = await py<Disclosure>(
        `/api/trpg/rooms/${room.id}/disclosures`,
        {
          method: "POST",
          body: JSON.stringify(payload),
        },
      );
      setDisclosures((prev) =>
        prev.some((item) => item.id === created.id) ? prev : [...prev, created],
      );
      setDisclosureDraft((prev) => ({
        ...prev,
        title: "",
        content: "",
        image: "",
        target_participant_ids: [],
      }));
      await loadDisclosures();
    } catch (e) {
      console.error(e);
      alert("開示情報の登録に失敗しました");
    } finally {
      setDisclosureBusy(false);
    }
  }, [disclosureDraft, loadDisclosures, myParticipantId, room, setDisclosures]);

  return (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Archive className="h-4 w-4" />
                開示情報
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs">
              <div className="grid grid-cols-4 gap-1">
                {[
                  ["all", "全体"],
                  ["mine", "個別"],
                  ["images", "画像"],
                  ["items", "品物"],
                ].map(([key, label]) => (
                  <Button
                    key={key}
                    type="button"
                    variant={disclosureFilter === key ? "default" : "outline"}
                    size="sm"
                    className="h-7 px-1 text-xs"
                    onClick={() => setDisclosureFilter(key as typeof disclosureFilter)}
                  >
                    {label}
                  </Button>
                ))}
              </div>
              <div className="max-h-72 space-y-2 overflow-auto pr-1">
                {filteredDisclosures.map((item) => {
                  const imageSrc = generatedImageSrc(item.image_url || item.image_path);
                  return (
                    <div key={item.id} className="rounded border p-2">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate font-semibold">{item.title}</div>
                          <div className="mt-1 flex flex-wrap gap-1">
                            <Badge variant="outline">
                              {item.visibility === "public" ? "全体" : item.visibility === "gm" ? "GM" : "個別"}
                            </Badge>
                            <Badge variant="secondary">
                              {item.disclosure_type === "item"
                                ? "アイテム"
                                : item.disclosure_type === "clue"
                                  ? "手掛かり"
                                  : item.disclosure_type === "image"
                                    ? "画像"
                                    : item.disclosure_type === "handout"
                                      ? "ハンドアウト"
                                      : "メモ"}
                            </Badge>
                          </div>
                        </div>
                        {item.visibility !== "public" ? (
                          <Lock className="mt-1 h-3 w-3 text-muted-foreground" />
                        ) : (
                          <Eye className="mt-1 h-3 w-3 text-muted-foreground" />
                        )}
                      </div>
                      {imageSrc && (
                        <img
                          src={imageSrc}
                          alt={item.title}
                          className="mt-2 max-h-40 w-full rounded object-contain"
                          loading="lazy"
                        />
                      )}
                      {item.content && (
                        <div className="mt-2 whitespace-pre-wrap text-muted-foreground">
                          {item.content}
                        </div>
                      )}
                      {item.target_participant_ids.length > 0 && (
                        <div className="mt-2 truncate text-[11px] text-muted-foreground">
                          宛先: {item.target_participant_ids.map((id) => targetLabel(id, room.participants)).join(", ")}
                        </div>
                      )}
                    </div>
                  );
                })}
                {filteredDisclosures.length === 0 && (
                  <div className="rounded border border-dashed p-3 text-center text-muted-foreground">
                    まだ開示情報はありません。
                  </div>
                )}
              </div>
              <div className="space-y-2 border-t pt-2">
                <div className="grid grid-cols-2 gap-2">
                  <select
                    value={disclosureDraft.disclosure_type}
                    onChange={(e) =>
                      setDisclosureDraft((prev) => ({
                        ...prev,
                        disclosure_type: e.target.value as Disclosure["disclosure_type"],
                      }))
                    }
                    className="h-8 rounded border bg-background px-2"
                    aria-label="開示情報種別"
                  >
                    <option value="handout">ハンドアウト</option>
                    <option value="item">アイテム</option>
                    <option value="clue">手掛かり</option>
                    <option value="image">画像</option>
                    <option value="note">メモ</option>
                  </select>
                  <select
                    value={disclosureDraft.visibility}
                    onChange={(e) =>
                      setDisclosureDraft((prev) => ({
                        ...prev,
                        visibility: e.target.value as Disclosure["visibility"],
                        target_participant_ids:
                          e.target.value === "private" ? prev.target_participant_ids : [],
                      }))
                    }
                    className="h-8 rounded border bg-background px-2"
                    aria-label="開示範囲"
                  >
                    <option value="public">全体開示</option>
                    <option value="private">個別開示</option>
                    <option value="gm">GM用</option>
                  </select>
                </div>
                {disclosureDraft.visibility === "private" && (
                  <div className="grid grid-cols-2 gap-1">
                    {visibleTargets.map((participant) => (
                      <label key={participant.id} className="flex items-center gap-2 rounded border px-2 py-1">
                        <Checkbox
                          checked={disclosureDraft.target_participant_ids.includes(participant.id)}
                          onCheckedChange={() => toggleDisclosureTarget(participant.id)}
                        />
                        <span className="min-w-0 truncate">{participant.display_name}</span>
                      </label>
                    ))}
                  </div>
                )}
                <Input
                  value={disclosureDraft.title}
                  onChange={(e) =>
                    setDisclosureDraft((prev) => ({ ...prev, title: e.target.value }))
                  }
                  className="h-8"
                  placeholder="タイトル"
                />
                <Textarea
                  value={disclosureDraft.content}
                  onChange={(e) =>
                    setDisclosureDraft((prev) => ({ ...prev, content: e.target.value }))
                  }
                  rows={3}
                  className="resize-none"
                  placeholder="開示本文"
                />
                <div className="grid grid-cols-[1fr_auto] gap-2">
                  <Input
                    value={disclosureDraft.image}
                    onChange={(e) =>
                      setDisclosureDraft((prev) => ({ ...prev, image: e.target.value }))
                    }
                    className="h-8"
                    placeholder="画像URLまたは生成画像パス"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={handleCreateDisclosure}
                    disabled={
                      disclosureBusy ||
                      !myParticipantId ||
                      !disclosureDraft.title.trim() ||
                      (!disclosureDraft.content.trim() && !disclosureDraft.image.trim()) ||
                      (disclosureDraft.visibility === "private" &&
                        disclosureDraft.target_participant_ids.length === 0)
                    }
                  >
                    <ImageIcon className="mr-1 h-3 w-3" />
                    保存
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
  );
}
