import React, { useEffect, useRef, useState, type ComponentProps } from "react";
import { View } from "react-native";
import { Button, Switch, Text, TextInput, useTheme } from "react-native-paper";
import {
  playbackSettingsFields,
  updatePlaybackSetting,
  type PlaybackSettings,
  type PlaybackSettingsGroup,
  type RuntimeCapabilities,
} from "@aoitalk/seethrough-rig-runtime";
import { NativeRigCanvas } from "./rig-canvas";
import { NativeRigKeyPreviewControls } from "./rig-key-preview-controls";
import { EMPTY_RIG_KEY_PREVIEW, type RigKeyPreview } from "./rig-key-preview";
import {
  savePlaybackSettings,
  useRigPlaybackSettings,
  type PlaybackSettingsView,
} from "./rig-playback-settings";

type CanvasProps = ComponentProps<typeof NativeRigCanvas>;
type Props = { canvas: CanvasProps; keyPreviewResetKey?: number };
type Draft = {
  settings: PlaybackSettings;
  base: PlaybackSettingsView;
  reset: boolean;
};
type Field = ReturnType<
  typeof playbackSettingsFields
>[number]["fields"][number];
const at = (settings: PlaybackSettings, path: string) =>
  path
    .split(".")
    .reduce<unknown>(
      (value, key) => (value as Record<string, unknown>)[key],
      settings,
    );

/** Native accessible touch slider, driven entirely by the canonical field metadata. */
function SettingsSlider({
  field,
  value,
  disabled,
  onChange,
}: {
  field: Field;
  value: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  const [width, setWidth] = useState(1);
  const theme = useTheme();
  const min = field.min ?? 0,
    max = field.max ?? 1,
    step = field.step ?? 0.01;
  const update = (next: number) =>
    onChange(
      Number(
        Math.max(
          min,
          Math.min(max, min + Math.round((next - min) / step) * step),
        ).toFixed(6),
      ),
    );
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const progress = (value - min) / (max - min);
  return (
    <View style={{ gap: 4 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Button
          compact
          disabled={disabled || value <= min}
          accessibilityLabel={field.label + "を減らす"}
          onPress={() => update(value - step)}
        >
          −
        </Button>
        <View
          accessible
          accessibilityRole="adjustable"
          accessibilityLabel={field.label}
          accessibilityState={{ disabled }}
          accessibilityValue={{
            min,
            max,
            now: value,
            text: String(value) + (field.unit ?? ""),
          }}
          accessibilityActions={[{ name: "increment" }, { name: "decrement" }]}
          onAccessibilityAction={(event) => {
            if (!disabled)
              update(
                value +
                  (event.nativeEvent.actionName === "increment" ? step : -step),
              );
          }}
          onLayout={(event) =>
            setWidth(Math.max(1, event.nativeEvent.layout.width))
          }
          onStartShouldSetResponder={() => !disabled}
          onMoveShouldSetResponder={() => !disabled}
          onResponderGrant={(event) =>
            update(min + (event.nativeEvent.locationX / width) * (max - min))
          }
          onResponderMove={(event) =>
            update(min + (event.nativeEvent.locationX / width) * (max - min))
          }
          style={{
            flex: 1,
            height: 44,
            justifyContent: "center",
            opacity: disabled ? 0.4 : 1,
          }}
        >
          <View
            pointerEvents="none"
            style={{
              height: 4,
              borderRadius: 2,
              backgroundColor: theme.colors.surfaceVariant,
            }}
          >
            <View
              style={{
                height: 4,
                width: `${Math.max(0, Math.min(1, progress)) * 100}%`,
                backgroundColor: theme.colors.primary,
              }}
            />
          </View>
          <View
            pointerEvents="none"
            style={{
              position: "absolute",
              left: `${Math.max(0, Math.min(1, progress)) * 100}%`,
              marginLeft: -9,
              width: 18,
              height: 18,
              borderRadius: 9,
              backgroundColor: theme.colors.primary,
            }}
          />
        </View>
        <Button
          compact
          disabled={disabled || value >= max}
          accessibilityLabel={field.label + "を増やす"}
          onPress={() => update(value + step)}
        >
          ＋
        </Button>
      </View>
      <TextInput
        dense
        mode="outlined"
        accessibilityLabel={field.label + "（数値）"}
        disabled={disabled}
        value={text}
        keyboardType="decimal-pad"
        right={field.unit ? <TextInput.Affix text={field.unit} /> : undefined}
        onChangeText={(next) => {
          setText(next);
          if (!/^-?\d+(?:\.\d+)?$/.test(next)) return;
          const number = Number(next);
          if (Number.isFinite(number) && number >= min && number <= max)
            onChange(number);
        }}
        onEndEditing={() => setText(String(value))}
      />
      <Text variant="bodySmall">
        {min}〜{max}
        {field.unit ?? ""}
      </Text>
    </View>
  );
}
export function NativePlaybackEditor({ canvas, keyPreviewResetKey }: Props) {
  return (
    <NativePlaybackEditorSession
      key={JSON.stringify([
        canvas.scope.server,
        canvas.scope.authScope,
        canvas.character,
        canvas.revision,
      ])}
      canvas={canvas}
      keyPreviewResetKey={keyPreviewResetKey}
    />
  );
}
function NativePlaybackEditorSession({ canvas, keyPreviewResetKey }: Props) {
  const {
    data,
    error: loadError,
    reload,
  } = useRigPlaybackSettings(canvas.scope, canvas.character, canvas.revision);
  const [draft, setDraft] = useState<Draft | null>(null),
    [caps, setCaps] = useState<RuntimeCapabilities | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  const [keyPreview, setKeyPreview] = useState<RigKeyPreview>({ ...EMPTY_RIG_KEY_PREVIEW });
  useEffect(() => {
    // Also reset when the already-selected named expression is pressed again.
    setKeyPreview((current) => current.eyes === 'jitome' ? { ...current, eyes: null } : current);
  }, [canvas.expression, keyPreviewResetKey]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    wind: true,
    blink: true,
  });
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const settings = (draft?.settings ?? data?.effective) as
    | PlaybackSettings
    | undefined;
  const editable = Boolean(data?.editable && caps?.playbackSettings.length);
  function change(
    group: PlaybackSettingsGroup,
    field: Field,
    value: number | string | boolean,
  ) {
    if (
      !settings ||
      !data ||
      busy ||
      !editable ||
      !field.supported ||
      request.current ||
      Object.entries(field.enabledWhen ?? {}).some(
        ([path, expected]) => at(settings, path) !== expected,
      )
    )
      return;
    try {
      const next = updatePlaybackSetting(settings, group, field.id, value);
      setDraft((previous) => ({
        settings: next,
        base: previous?.base ?? data,
        reset: false,
      }));
      setError("");
      setSaved(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "設定値を確認してください");
    }
  }
  async function save() {
    if (!draft || busy || !editable || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      await savePlaybackSettings(
        canvas.scope,
        canvas.character,
        canvas.revision,
        {
          manifest_sha256: draft.base.manifest_sha256,
          expected_revision: draft.base.revision,
          ...(draft.reset ? {} : { settings: draft.settings }),
        },
        draft.reset,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setDraft(null);
        setSaved(true);
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(e instanceof Error ? e.message : "再生設定を保存できません");
        reload();
      }
    } finally {
      if (request.current === controller) request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <View style={{ gap: 10 }}>
      <NativeRigCanvas
        {...canvas}
        keyPreview={keyPreview}
        playbackSettings={data?.editable ? settings : undefined}
        onCapabilities={(value) => {
          setCaps(value as unknown as RuntimeCapabilities);
          canvas.onCapabilities?.(value);
        }}
      />
      <NativeRigKeyPreviewControls capabilities={caps} value={keyPreview} onChange={setKeyPreview} />
      <Text variant="titleSmall">再生設定</Text>
      <Text variant="bodySmall">
        変更はこのプレビューだけに反映されます。保存すると会話にも適用されます。
      </Text>
      {settings &&
        playbackSettingsFields(caps).map((group) => (
          <View key={group.id} style={{ gap: 8, paddingVertical: 8 }}>
            <Button
              contentStyle={{ justifyContent: "flex-start" }}
              icon={expanded[group.id] ? "chevron-down" : "chevron-right"}
              accessibilityLabel={group.label + "の設定"}
              accessibilityState={{ expanded: Boolean(expanded[group.id]) }}
              onPress={() =>
                setExpanded((current) => ({
                  ...current,
                  [group.id]: !current[group.id],
                }))
              }
            >
              {group.label}
            </Button>
            {expanded[group.id] &&
              group.fields.map((field) => {
                const value = at(settings, group.id + "." + field.id);
                const disabled =
                  busy ||
                  !editable ||
                  !field.supported ||
                  Object.entries(field.enabledWhen ?? {}).some(
                    ([path, expected]) => at(settings, path) !== expected,
                  );
                return (
                  <View
                    key={field.id}
                    style={{ gap: 2, opacity: field.supported ? 1 : 0.5 }}
                  >
                    <Text variant="bodyMedium">{field.label}</Text>
                    {field.type === "boolean" ? (
                      <Switch
                        accessibilityLabel={field.label}
                        disabled={disabled}
                        value={value === true}
                        onValueChange={(next) => change(group.id, field, next)}
                      />
                    ) : field.type === "enum" ? (
                      <View style={{ flexDirection: "row", flexWrap: "wrap" }}>
                        {field.options?.map((option) => (
                          <Button
                            key={option.value}
                            compact
                            disabled={disabled}
                            mode={
                              value === option.value ? "contained" : "outlined"
                            }
                            onPress={() =>
                              change(group.id, field, option.value)
                            }
                          >
                            {option.label}
                          </Button>
                        ))}
                      </View>
                    ) : (
                      <SettingsSlider
                        field={field}
                        value={Number(value)}
                        disabled={disabled}
                        onChange={(next) => change(group.id, field, next)}
                      />
                    )}
                    {field.description && (
                      <Text variant="bodySmall">{field.description}</Text>
                    )}
                    {!field.supported && (
                      <Text variant="bodySmall">
                        このモデルでは利用できません
                      </Text>
                    )}
                  </View>
                );
              })}
          </View>
        ))}
      {!data && !loadError && (
        <Text accessibilityLiveRegion="polite">再生設定を読み込み中</Text>
      )}
      {data && caps && !editable && (
        <Text variant="bodySmall">
          このモデルのランタイムでは再生設定を変更できません。
        </Text>
      )}
      <View style={{ flexDirection: "row", flexWrap: "wrap" }}>
        <Button
          disabled={busy || !editable || !draft}
          onPress={() => void save()}
        >
          再生設定を保存
        </Button>
        <Button
          disabled={busy || !draft}
          onPress={() => {
            setDraft(null);
            setKeyPreview((current) => current.eyes === 'jitome' ? { ...current, eyes: null } : current);
            setError("");
            setSaved(false);
          }}
        >
          変更を取り消す
        </Button>
        <Button
          disabled={busy || !editable}
          onPress={() => {
            setKeyPreview((current) => current.eyes === 'jitome' ? { ...current, eyes: null } : current);
            if (data)
              setDraft({
                settings: data.defaults as PlaybackSettings,
                base: draft?.base ?? data,
                reset: true,
              });
            setError("");
            setSaved(false);
          }}
        >
          モデルの初期値に戻す
        </Button>
      </View>
      {draft && data && draft.base.revision !== data.revision && (
        <Text accessibilityLiveRegion="polite">
          別の画面で設定が更新されています。変更を取り消してから調整し直してください。
        </Text>
      )}
      {(draft || saved) && (
        <Text accessibilityLiveRegion="polite">
          {draft ? "未保存の変更があります" : "再生設定を保存しました"}
        </Text>
      )}
      {(error || loadError) && (
        <>
          <Text accessibilityLiveRegion="polite" style={{ color: "#f38ba8" }}>
            {error || loadError}
          </Text>
          <Button onPress={reload}>再読込</Button>
        </>
      )}
    </View>
  );
}
