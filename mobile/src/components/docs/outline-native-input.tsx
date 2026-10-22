import React, { forwardRef, useImperativeHandle, useRef } from "react";
import { requireNativeView } from "expo";
import { Platform, type ViewProps } from "react-native";
import { getDocsKeyboardModule } from "../../../modules/docs-keyboard";
import type { NativeOutlineValue, NativeOutlineSubmit } from "./outline-native-session";

export type OutlineNativeInputHandle = {
  focus(): void;
  blur(): void;
  cancelFocus(): void;
  isFocused(): boolean;
  getNativeTag(): number | null;
};
type Props = ViewProps & {
  value: NativeOutlineValue;
  scope: number;
  active: boolean;
  onChangeValue(value: NativeOutlineValue): void;
  onSubmitValue(value: NativeOutlineSubmit): void;
  onFocusValue(value: { epoch: number; generation: number; focused: boolean }): void;
  onSizeValue(value: { epoch: number; generation: number; height: number }): void;
};
type NativeHandle = { nativeTag: number | null; focus(epoch: number): Promise<void>;
  cancelFocus(epoch: number): Promise<void>; blur(): Promise<void> };
type NativeEvent<T> = { nativeEvent: T };
type NativeProps = Omit<ViewProps, "onFocus"> & {
  ref?: React.Ref<NativeHandle>;
  value: NativeOutlineValue & { scope: number };
  active: boolean;
  diagnostics: boolean;
  onChange(event: NativeEvent<NativeOutlineValue>): void;
  onSubmit(event: NativeEvent<NativeOutlineSubmit>): void;
  onFocus(event: NativeEvent<{ epoch: number; generation: number; focused: boolean }>): void;
  onContentSizeChange(event: NativeEvent<{ epoch: number; generation: number; height: number }>): void;
};

let NativeView: React.ComponentType<NativeProps> | null | undefined;
export function outlineNativeInputAvailable(): boolean {
  if (NativeView !== undefined) return NativeView !== null;
  NativeView = null;
  if (Platform.OS === "android") {
    try {
      if (getDocsKeyboardModule().supportsOutlineInput) {
        NativeView = requireNativeView<NativeProps>("DocsKeyboard", "OutlineInput");
      }
    } catch { /* Older development clients retain the RN editor until rebuilt. */ }
  }
  return NativeView !== null;
}

/** This component stays mounted while the logical row changes. */
export const OutlineNativeInput = forwardRef<OutlineNativeInputHandle, Props>(
  function OutlineNativeInput({ value, scope, active, onChangeValue, onSubmitValue, onFocusValue,
    onSizeValue, ...viewProps }, ref) {
    const native = useRef<NativeHandle | null>(null);
    const focused = useRef(false);
    const latestValue = useRef(value);
    latestValue.current = value;
    const command = (action: number, run: () => Promise<void> | undefined) => {
      const epoch = latestValue.current.epoch;
      const report = (outcome: number) => {
        if (process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1") {
          console.info("[DocsOutlineCommand]", { action, epoch, outcome });
        }
      };
      try { void run()?.then(() => report(1), () => report(0)); }
      catch { report(0); }
    };
    useImperativeHandle(ref, () => ({
      focus() { command(1, () => native.current?.focus(latestValue.current.epoch)); },
      blur() { void native.current?.blur().catch(() => undefined); },
      cancelFocus() { command(2, () => native.current?.cancelFocus(latestValue.current.epoch)); },
      isFocused: () => focused.current,
      getNativeTag: () => native.current?.nativeTag ?? null,
    }), []);
    if (!NativeView) return null;
    return <NativeView {...viewProps} ref={native} value={{ ...value, scope }} active={active}
      diagnostics={process.env.EXPO_PUBLIC_DOCS_INPUT_METRICS === "1"}
      onChange={({ nativeEvent }) => onChangeValue(nativeEvent)}
      onSubmit={({ nativeEvent }) => onSubmitValue(nativeEvent)}
      onFocus={({ nativeEvent }) => { focused.current = nativeEvent.focused; onFocusValue(nativeEvent); }}
      onContentSizeChange={({ nativeEvent }) => onSizeValue(nativeEvent)} />;
  },
);
