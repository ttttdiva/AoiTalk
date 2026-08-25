"use client";

// 画面録画 + 音声説明から Skill を自動生成するダイアログ。
// 同意 → 録画 → プレビュー/タイトル入力 → アップロード → 解析ポーリング →
// ドラフトレビュー、という一連のフローを管理する。

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Circle,
  Loader2,
  MonitorPlay,
  Mic,
  Sparkles,
  Square,
  RotateCcw,
  AlertTriangle,
} from "lucide-react";
import { SkillRecordingReview } from "@/components/skills/skill-recording-review";
import {
  useSkillRecorder,
  formatDuration,
  uploadSkillRecording,
  analyzeSkillRecording,
  getSkillRecording,
  getSkillRecordingDraft,
  deleteSkillRecording,
  type SkillRecordingDraft,
} from "@/lib/skill-recording";

type FlowStep =
  | "consent"
  | "recording"
  | "preview"
  | "uploading"
  | "analyzing"
  | "review"
  | "failed";

const POLL_INTERVAL_MS = 2500;
const MAX_POLL_ATTEMPTS = 240; // 約 10 分

interface SkillRecorderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 呼び出し元のプロジェクト文脈（保存先の初期値に使う）。 */
  contextProjectId?: string | null;
  /** 保存成功時に Skill 一覧を更新するためのコールバック。 */
  onSaved: () => void;
}

export function SkillRecorderDialog({
  open,
  onOpenChange,
  contextProjectId,
  onSaved,
}: SkillRecorderDialogProps) {
  const recorder = useSkillRecorder();
  const [step, setStep] = useState<FlowStep>("consent");
  const [title, setTitle] = useState("");
  const [uploadPercent, setUploadPercent] = useState(0);
  const [recordingId, setRecordingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<SkillRecordingDraft | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const videoRef = useRef<HTMLVideoElement>(null);
  const pollTimerRef = useRef<number | null>(null);
  const recordingIdRef = useRef<string | null>(null);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // ダイアログ全体のリセット（クローズ時・再試行時に使う）。
  const resetAll = useCallback(() => {
    clearPoll();
    recorder.reset();
    setStep("consent");
    setTitle("");
    setUploadPercent(0);
    setDraft(null);
    setErrorMessage(null);
    setRecordingId(null);
    recordingIdRef.current = null;
  }, [clearPoll, recorder]);

  // ダイアログを開くたびに初期化する。
  useEffect(() => {
    if (open) {
      resetAll();
    }
    // resetAll を依存に入れると recorder の再生成で無限ループするため open のみ監視
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 録画停止（blob 生成）を検知して自動的にプレビュー段階へ進める。
  useEffect(() => {
    if (step === "recording" && recorder.phase === "stopped" && recorder.blob) {
      setStep("preview");
    }
  }, [step, recorder.phase, recorder.blob]);

  // 録画中プレビュー：live ストリームを video に流す。
  useEffect(() => {
    if (step === "recording" && recorder.liveStream && videoRef.current) {
      videoRef.current.srcObject = recorder.liveStream;
      videoRef.current.play().catch(() => {
        // 自動再生がブロックされても致命ではない
      });
    }
  }, [step, recorder.liveStream]);

  // 録画エラーはダイアログ上に反映する。
  useEffect(() => {
    if (recorder.error) {
      setErrorMessage(recorder.error);
    }
  }, [recorder.error]);

  const handleStartRecording = useCallback(async () => {
    setErrorMessage(null);
    setStep("recording");
    await recorder.start();
  }, [recorder]);

  // recorder.start がエラーで idle のままなら consent に戻す。
  useEffect(() => {
    if (step === "recording" && recorder.phase === "idle" && recorder.error) {
      setStep("consent");
    }
  }, [step, recorder.phase, recorder.error]);

  const pollStatus = useCallback(
    (id: string, attempt: number) => {
      clearPoll();
      pollTimerRef.current = window.setTimeout(async () => {
        if (recordingIdRef.current !== id) return;
        try {
          const record = await getSkillRecording(id);
          if (recordingIdRef.current !== id) return;
          if (record.status === "draft_ready") {
            const loadedDraft = await getSkillRecordingDraft(id);
            if (recordingIdRef.current !== id) return;
            setDraft(loadedDraft);
            setStep("review");
            return;
          }
          if (record.status === "failed") {
            setErrorMessage(record.error || "解析に失敗しました。");
            setStep("failed");
            return;
          }
          if (attempt >= MAX_POLL_ATTEMPTS) {
            setErrorMessage("解析がタイムアウトしました。");
            setStep("failed");
            return;
          }
          pollStatus(id, attempt + 1);
        } catch (err) {
          if (recordingIdRef.current !== id) return;
          setErrorMessage(
            err instanceof Error ? err.message : "解析状況の取得に失敗しました。",
          );
          setStep("failed");
        }
      }, POLL_INTERVAL_MS);
    },
    [clearPoll],
  );

  const handleAnalyze = useCallback(async () => {
    if (!recorder.blob) return;
    setErrorMessage(null);
    setUploadPercent(0);
    setStep("uploading");
    try {
      const uploaded = await uploadSkillRecording(recorder.blob, {
        title: title.trim() || undefined,
        projectId: contextProjectId ?? undefined,
        onProgress: setUploadPercent,
      });
      setRecordingId(uploaded.id);
      recordingIdRef.current = uploaded.id;
      setStep("analyzing");
      await analyzeSkillRecording(uploaded.id);
      pollStatus(uploaded.id, 0);
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : "アップロードに失敗しました。",
      );
      setStep("failed");
    }
  }, [recorder.blob, title, contextProjectId, pollStatus]);

  // 失敗時などに録画データが残っていれば削除する。
  const discardRecordingIfAny = useCallback(async () => {
    const id = recordingIdRef.current;
    if (id) {
      try {
        await deleteSkillRecording(id);
      } catch {
        // 破棄失敗は致命ではない
      }
    }
  }, []);

  const handleClose = useCallback(() => {
    clearPoll();
    onOpenChange(false);
  }, [clearPoll, onOpenChange]);

  const handleSaved = useCallback(() => {
    onSaved();
    handleClose();
  }, [onSaved, handleClose]);

  const handleDiscarded = useCallback(() => {
    handleClose();
  }, [handleClose]);

  // アンマウント時にポーリングを止める。
  useEffect(() => clearPoll, [clearPoll]);

  const renderBody = () => {
    switch (step) {
      case "consent":
        return (
          <div className="space-y-3">
            <div className="space-y-2 text-sm text-muted-foreground">
              <p className="flex items-start gap-2">
                <MonitorPlay className="mt-0.5 size-4 shrink-0" />
                指定したウィンドウまたは画面を録画します。
              </p>
              <p className="flex items-start gap-2">
                <Mic className="mt-0.5 size-4 shrink-0" />
                同時にマイク音声（操作の説明）を録音します。
              </p>
              <p className="flex items-start gap-2 text-amber-600 dark:text-amber-500">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                録画した映像と音声は、解析のため設定済みの AI
                プロバイダへ送信されます。機密情報が映らないようご注意ください。
              </p>
            </div>
            {errorMessage && (
              <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
                {errorMessage}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={handleClose}>
                キャンセル
              </Button>
              <Button size="sm" onClick={handleStartRecording}>
                <Circle className="size-3 fill-current mr-1" />
                同意して録画を開始
              </Button>
            </div>
          </div>
        );

      case "recording":
        return (
          <div className="space-y-3">
            <div className="flex items-center justify-between rounded-md border px-2.5 py-2">
              <span className="flex items-center gap-2 text-sm font-medium">
                <span className="relative flex size-2.5">
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-red-500 opacity-75" />
                  <span className="relative inline-flex size-2.5 rounded-full bg-red-600" />
                </span>
                録画中
              </span>
              <span className="font-mono text-sm tabular-nums">
                {formatDuration(recorder.durationSec)}
              </span>
            </div>
            <video
              ref={videoRef}
              muted
              playsInline
              className="aspect-video w-full rounded-md border bg-black"
            />
            <div className="flex justify-end">
              <Button size="sm" onClick={recorder.stop}>
                <Square className="size-3 fill-current mr-1" />
                録画を停止
              </Button>
            </div>
          </div>
        );

      case "preview":
        return (
          <div className="space-y-3">
            {recorder.previewUrl && (
              <video
                src={recorder.previewUrl}
                controls
                playsInline
                className="aspect-video w-full rounded-md border bg-black"
              />
            )}
            <p className="text-xs text-muted-foreground">
              録画時間: {formatDuration(recorder.durationSec)}
            </p>
            <div className="space-y-1">
              <Label className="text-xs">タイトル（任意）</Label>
              <Input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="例: 請求書の登録手順"
              />
            </div>
            {errorMessage && (
              <p className="rounded-md bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
                {errorMessage}
              </p>
            )}
            <div className="flex items-center justify-between">
              <Button variant="outline" size="sm" onClick={resetAll}>
                <RotateCcw className="size-3 mr-1" />
                撮り直す
              </Button>
              <Button size="sm" onClick={handleAnalyze}>
                <Sparkles className="size-3 mr-1" />
                解析を開始
              </Button>
            </div>
          </div>
        );

      case "uploading":
        return (
          <div className="space-y-3 py-2">
            <p className="flex items-center gap-2 text-sm">
              <Loader2 className="size-4 animate-spin" />
              録画をアップロードしています... {uploadPercent}%
            </p>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${uploadPercent}%` }}
              />
            </div>
          </div>
        );

      case "analyzing":
        return (
          <div className="space-y-2 py-4 text-center">
            <Loader2 className="mx-auto size-6 animate-spin text-muted-foreground" />
            <p className="text-sm text-muted-foreground">
              AI が録画内容を解析しています...
            </p>
            <p className="text-xs text-muted-foreground">
              しばらくお待ちください。この画面を閉じずにお待ちください。
            </p>
          </div>
        );

      case "review":
        return draft && recordingId ? (
          <SkillRecordingReview
            recordingId={recordingId}
            draft={draft}
            contextProjectId={contextProjectId}
            onSaved={handleSaved}
            onDiscarded={handleDiscarded}
          />
        ) : null;

      case "failed":
        return (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md bg-destructive/10 px-2.5 py-2 text-sm text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <span>{errorMessage || "処理に失敗しました。"}</span>
            </div>
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={async () => {
                  await discardRecordingIfAny();
                  handleClose();
                }}
              >
                破棄して閉じる
              </Button>
              <Button
                size="sm"
                onClick={async () => {
                  await discardRecordingIfAny();
                  resetAll();
                }}
              >
                <RotateCcw className="size-3 mr-1" />
                最初からやり直す
              </Button>
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  // 録画中・アップロード・解析中は外側クリックでの誤クローズを防ぐ。
  const dismissible =
    step === "consent" || step === "preview" || step === "review" || step === "failed";

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !dismissible) return;
        onOpenChange(next);
      }}
    >
      <DialogContent size="lg" className="max-h-[85vh] overflow-auto">
        <DialogHeader>
          <DialogTitle>録画してスキルを作成</DialogTitle>
          <DialogDescription>
            操作を録画し、AI が手順を Skill 化します。
          </DialogDescription>
        </DialogHeader>
        {renderBody()}
      </DialogContent>
    </Dialog>
  );
}
