import React, { useCallback, useState } from "react";
import { StyleSheet } from "react-native";
import { TextInput as PaperTextInput, useTheme } from "react-native-paper";

type PaperTextInputProps = React.ComponentProps<typeof PaperTextInput>;

type InputThemeColors = {
  primary?: string;
  secondary?: string;
  error?: string;
  surface?: string;
  background?: string;
  onSurface?: string;
  onSurfaceVariant?: string;
  onBackground?: string;
};

export type InputVisualState = {
  focused?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  error?: boolean;
};

export type InputResolvedColors = {
  background: string;
  caret: string;
  selection: string;
  text: string;
  placeholder: string;
};

function parseColor(value: string | undefined): [number, number, number] | null {
  if (!value) return null;
  const normalized = value.trim();
  // Paper's built-in theme tokens use opaque rgb()/rgba(), while app overrides use hex.
  const rgb = /^rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*1(?:\.0+)?\s*)?\)$/i.exec(normalized);
  if (rgb) {
    const channels = rgb.slice(1).map(Number) as [number, number, number];
    return channels.every((channel) => channel <= 255) ? channels : null;
  }
  const short = /^#([0-9a-f]{3})$/i.exec(normalized);
  if (short) {
    return short[1].split("").map((part) => Number.parseInt(`${part}${part}`, 16)) as [
      number,
      number,
      number,
    ];
  }
  const full = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(normalized);
  if (!full) return null;
  return [
    Number.parseInt(full[1].slice(0, 2), 16),
    Number.parseInt(full[1].slice(2, 4), 16),
    Number.parseInt(full[1].slice(4, 6), 16),
  ];
}

function relativeLuminance(color: string): number | null {
  const rgb = parseColor(color);
  if (!rgb) return null;
  const [red, green, blue] = rgb.map((channel) => {
    const value = channel / 255;
    return value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

/** WCAG contrast ratio. Theme regression tests use this for caret/background pairs. */
export function colorContrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = relativeLuminance(foreground);
  const backgroundLuminance = relativeLuminance(background);
  if (foregroundLuminance === null || backgroundLuminance === null) return 1;
  const lighter = Math.max(foregroundLuminance, backgroundLuminance);
  const darker = Math.min(foregroundLuminance, backgroundLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

function mostVisibleToken(
  candidates: Array<string | undefined>,
  background: string,
  minimumContrast: number,
): string {
  const unique = Array.from(new Set(candidates.filter((value): value is string => Boolean(value))));
  const passing = unique.find(
    (candidate) => colorContrastRatio(candidate, background) >= minimumContrast,
  );
  if (passing) return passing;
  return unique.reduce(
    (best, candidate) =>
      colorContrastRatio(candidate, background) > colorContrastRatio(best, background)
        ? candidate
        : best,
    unique[0] ?? background,
  );
}

function rgba(color: string, alpha: number): string {
  const rgb = parseColor(color);
  if (!rgb) return color;
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

/**
 * Theme tokenから、背景と同化しないcaret/selection/text色を導出する。
 * 黒・白の固定値には落とさず、light/dark/high-contrast themeのtokenを優先する。
 */
export function resolveInputColors(
  colors: InputThemeColors,
  backgroundOverride?: string,
  state: InputVisualState = {},
): InputResolvedColors {
  const background =
    backgroundOverride || colors.surface || colors.background || colors.onSurface || "transparent";
  const regularCandidates = state.focused
    ? [colors.primary, colors.secondary, colors.onSurface, colors.onBackground, colors.error]
    : [colors.onSurface, colors.primary, colors.secondary, colors.onBackground, colors.error];
  const caretCandidates = state.error
    ? [colors.error, ...regularCandidates]
    : state.disabled || state.readOnly
      ? [colors.onSurface, colors.onSurfaceVariant, ...regularCandidates]
      : regularCandidates;
  // A fixed dark surface can also be used under a light theme. Its foreground
  // tokens may all be dark, so allow contrasting surface tokens as a last resort.
  const surfaceFallbacks = [colors.surface, colors.background];
  const caret = mostVisibleToken([...caretCandidates, ...surfaceFallbacks], background, 3);
  const text = mostVisibleToken(
    [colors.onSurface, colors.onBackground, colors.primary, colors.secondary, colors.error, ...surfaceFallbacks],
    background,
    4.5,
  );
  const placeholder = mostVisibleToken(
    [colors.onSurfaceVariant, colors.onSurface, colors.onBackground, colors.primary, ...surfaceFallbacks],
    background,
    3,
  );

  return {
    background,
    caret,
    selection: rgba(caret, 0.38),
    text,
    placeholder,
  };
}

type ThemedTextInputProps = PaperTextInputProps & {
  inputBackgroundColor?: string;
};

/** 背景とのcontrastからcaret/selection色を選ぶ共有TextInput。 */
export function ThemedTextInput({
  inputBackgroundColor,
  onFocus,
  onBlur,
  style,
  ...props
}: ThemedTextInputProps) {
  const theme = useTheme();
  const [focused, setFocused] = useState(false);
  const flattenedStyle = StyleSheet.flatten(style) as
    | { backgroundColor?: unknown }
    | undefined;
  const styleBackground =
    typeof flattenedStyle?.backgroundColor === "string"
      ? flattenedStyle.backgroundColor
      : undefined;
  const resolved = resolveInputColors(
    theme.colors,
    inputBackgroundColor || styleBackground,
    {
      focused,
      disabled: Boolean(props.disabled),
      readOnly: Boolean(props.readOnly),
      error: Boolean(props.error),
    },
  );
  const handleFocus = useCallback<NonNullable<PaperTextInputProps["onFocus"]>>(
    (event) => {
      setFocused(true);
      onFocus?.(event);
    },
    [onFocus],
  );
  const handleBlur = useCallback<NonNullable<PaperTextInputProps["onBlur"]>>(
    (event) => {
      setFocused(false);
      onBlur?.(event);
    },
    [onBlur],
  );

  return (
    <PaperTextInput
      {...props}
      style={style}
      cursorColor={props.cursorColor ?? resolved.caret}
      selectionColor={props.selectionColor ?? resolved.selection}
      textColor={props.textColor ?? resolved.text}
      placeholderTextColor={props.placeholderTextColor ?? resolved.placeholder}
      onFocus={handleFocus}
      onBlur={handleBlur}
    />
  );
}
