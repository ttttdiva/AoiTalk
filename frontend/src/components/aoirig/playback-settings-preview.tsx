"use client";

import { useEffect, useRef, useState, type ComponentProps } from "react";
import {
  createPlaybackSettingsPanel,
  type PlaybackSettings,
  type RuntimeCapabilities,
} from "@aoitalk/seethrough-rig-runtime";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { Button } from "@/components/ui/button";
import { rigRequest } from "@/lib/seethrough-rig/api";
import {
  playbackSettingsUrl,
  useRigPlaybackSettings,
  type PlaybackSettingsView,
} from "@/lib/seethrough-rig/playback-settings";
import { RigCanvas } from "./character-animation";

type Props = {
  characterId: string;
  revision: string;
  canvas: ComponentProps<typeof RigCanvas>;
};
type Draft = {
  settings: PlaybackSettings;
  base: PlaybackSettingsView;
  reset: boolean;
};

const LEFT_GROUPS = ["wind", "blink", "breath"];
const RIGHT_GROUPS = ["idle", "gaze", "physics"];

function SharedPlaybackControls({
  groups,
  label,
  settings,
  capabilities,
  disabled,
  onChange,
}: {
  groups: string[];
  label: string;
  settings: PlaybackSettings;
  capabilities: RuntimeCapabilities;
  disabled: boolean;
  onChange: (settings: PlaybackSettings) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const panel = useRef<ReturnType<typeof createPlaybackSettingsPanel> | null>(
    null,
  );
  const change = useRef(onChange);
  change.current = onChange;
  useEffect(() => {
    if (!container.current) return;
    const instance = createPlaybackSettingsPanel(container.current, {
      groups,
      settings,
      capabilities,
      disabled,
      onChange: (value) => change.current(value),
      label,
    });
    panel.current = instance;
    return () => {
      instance.dispose();
      panel.current = null;
    };
    // The panel owns its DOM; subsequent props update it without losing slider focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    panel.current?.update({ settings, capabilities, disabled });
  }, [settings, capabilities, disabled]);
  return <div ref={container} className="min-w-0" />;
}

/** Keyed ownership boundary: changing character/model/account discards the preview draft. */
export function PlaybackSettingsPreview(props: Props) {
  const owner = useCurrentUserId();
  return (
    <PlaybackSettingsPreviewSession
      key={`${owner}:${props.characterId}:${props.revision}`}
      {...props}
    />
  );
}
function PlaybackSettingsPreviewSession({
  characterId,
  revision,
  canvas,
}: Props) {
  const {
    data,
    error: loadError,
    mutate,
  } = useRigPlaybackSettings(characterId, revision);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [caps, setCaps] = useState<RuntimeCapabilities | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const settings = (draft?.settings ?? data?.effective) as
    | PlaybackSettings
    | undefined;
  const supported = Boolean(caps?.playbackSettings.length);
  const editable = Boolean(data?.editable && supported);
  async function save() {
    if (!draft || busy || !editable || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const result = await rigRequest<PlaybackSettingsView>(
        playbackSettingsUrl(characterId, revision),
        {
          method: draft.reset ? "DELETE" : "PATCH",
          signal: controller.signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            manifest_sha256: draft.base.manifest_sha256,
            expected_revision: draft.base.revision,
            ...(draft.reset ? {} : { settings: draft.settings }),
          }),
        },
      );
      if (controller.signal.aborted) return;
      await mutate(
        (current) =>
          current &&
          current.manifest_sha256 === result.manifest_sha256 &&
          current.revision > result.revision
            ? current
            : result,
        { revalidate: false },
      );
      if (controller.signal.aborted) return;
      setDraft(null);
      setSaved(true);
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : "再生設定を保存できません");
        // Keep the unsaved preview/base revision. Cancel can adopt the latest saved version.
        void mutate();
      }
    } finally {
      if (request.current === controller) request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function changeSettings(value: PlaybackSettings) {
    if (!data || !editable || busy) return;
    setDraft((previous) => ({
      settings: value,
      base: previous?.base ?? data,
      reset: false,
    }));
    setSaved(false);
    setError("");
  }
  return (
    <section aria-label="再生設定" className="min-w-0 space-y-3">
      <header className="space-y-1">
        <p className="text-sm font-semibold">再生設定</p>
        <p className="text-xs text-muted-foreground">
          変更はこのプレビューだけに反映されます。保存すると会話にも適用されます。
        </p>
      </header>
      <div
        data-testid="playback-settings-layout"
        className="grid min-w-0 gap-3 lg:grid-cols-[minmax(16rem,1fr)_minmax(0,1.5fr)_minmax(16rem,1fr)] lg:items-start"
      >
        <section
          aria-label="キャラクタープレビュー"
          className="min-w-0 self-start rounded-lg border bg-background/50 lg:sticky lg:top-2 lg:col-start-2 lg:row-start-1 lg:[&_canvas]:h-[min(34rem,60dvh)]! lg:[&_canvas]:max-h-none"
        >
          <RigCanvas
            {...canvas}
            playbackSettings={data?.editable ? settings : undefined}
            onCapabilities={(value) => {
              setCaps(value as unknown as RuntimeCapabilities);
              canvas.onCapabilities?.(value);
            }}
          />
        </section>
        <section
          aria-label="風・まばたき・呼吸の設定"
          className="min-w-0 rounded-lg border bg-background/50 px-1 py-2 sm:p-2 lg:col-start-1 lg:row-start-1 lg:max-h-[min(34rem,60dvh)] lg:overflow-y-auto lg:overscroll-contain"
        >
          {settings && caps && (
            <SharedPlaybackControls
              groups={LEFT_GROUPS}
              label="風・まばたき・呼吸"
              settings={settings}
              capabilities={caps}
              disabled={busy || !editable}
              onChange={changeSettings}
            />
          )}
        </section>
        <section
          aria-label="待機動作・視線・物理の設定"
          className="min-w-0 rounded-lg border bg-background/50 px-1 py-2 sm:p-2 lg:col-start-3 lg:row-start-1 lg:max-h-[min(34rem,60dvh)] lg:overflow-y-auto lg:overscroll-contain"
        >
          {settings && caps && (
            <SharedPlaybackControls
              groups={RIGHT_GROUPS}
              label="待機動作・視線・物理"
              settings={settings}
              capabilities={caps}
              disabled={busy || !editable}
              onChange={changeSettings}
            />
          )}
        </section>
      </div>
      <footer className="space-y-2 border-t pt-3">
        {!data && !loadError && (
          <p role="status" className="text-xs">
            再生設定を読み込み中
          </p>
        )}
        {data && caps && !editable && (
          <p role="status" className="text-xs">
            このモデルのランタイムでは再生設定を変更できません。
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            disabled={busy || !editable || !draft}
            onClick={() => void save()}
          >
            再生設定を保存
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || !draft}
            onClick={() => {
              setDraft(null);
              setError("");
              setSaved(false);
            }}
          >
            変更を取り消す
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy || !editable}
            onClick={() => {
              if (data)
                setDraft({
                  settings: data.defaults as PlaybackSettings,
                  base: draft?.base ?? data,
                  reset: true,
                });
              setSaved(false);
              setError("");
            }}
          >
            モデルの初期値に戻す
          </Button>
        </div>
        {draft && data && draft.base.revision !== data.revision && (
          <p role="status" className="text-xs">
            別の画面で設定が更新されています。変更を取り消してから調整し直してください。
          </p>
        )}
        {(draft || saved) && (
          <p role="status" className="text-xs">
            {draft ? "未保存の変更があります" : "再生設定を保存しました"}
          </p>
        )}
        {(error || loadError) && (
          <p role="alert" className="text-xs text-destructive">
            {error || "再生設定を取得できません"}{" "}
            <button type="button" onClick={() => void mutate()}>
              再読込
            </button>
          </p>
        )}
      </footer>
    </section>
  );
}
