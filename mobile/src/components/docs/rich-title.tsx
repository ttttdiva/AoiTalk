/**
 * リッチタイトル表示
 *
 * Docs 本文（タイトル）中の `[[参照]]` / `[[node:UUID]]` と `#tag` を検出し、
 * インラインで装飾表示する（表示専用。編集はプレーンテキストの TextInput 側で行う）。
 */

import React from "react";
import { StyleSheet, Text, type StyleProp, type TextStyle } from "react-native";

const TEXT = "#cdd6f4";
const LINK = "#89b4fa";
const TAG = "#c084fc";

// `[[...]]`（参照）または `#tag` を1トークンとして拾う
const TOKEN_RE = /(\[\[[^\]]+\]\]|#[^\s#]+)/g;

type Segment =
  | { kind: "text"; value: string }
  | { kind: "reference"; value: string }
  | { kind: "tag"; value: string };

function stripReferenceLabel(raw: string): string {
  // `[[node:UUID]]` → UUID 先頭8桁 / `[[ラベル]]` → ラベル
  const inner = raw.slice(2, -2).trim();
  if (inner.toLowerCase().startsWith("node:")) {
    const id = inner.slice(5).trim();
    return id.length > 8 ? id.slice(0, 8) : id;
  }
  return inner;
}

export function tokenizeRichText(text: string): Segment[] {
  const segments: Segment[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((match = TOKEN_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ kind: "text", value: text.slice(lastIndex, match.index) });
    }
    const token = match[0];
    if (token.startsWith("[[")) {
      segments.push({ kind: "reference", value: stripReferenceLabel(token) });
    } else {
      segments.push({ kind: "tag", value: token });
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) {
    segments.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return segments;
}

export function RichTitle({
  text,
  style,
  numberOfLines,
}: {
  text: string;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
}) {
  const segments = tokenizeRichText(text || "");
  return (
    <Text style={[styles.base, style]} numberOfLines={numberOfLines}>
      {segments.map((segment, index) => {
        if (segment.kind === "reference") {
          return (
            <Text key={index} style={styles.reference}>
              {`🔗 ${segment.value}`}
            </Text>
          );
        }
        if (segment.kind === "tag") {
          return (
            <Text key={index} style={styles.tag}>
              {segment.value}
            </Text>
          );
        }
        return <Text key={index}>{segment.value}</Text>;
      })}
    </Text>
  );
}

const styles = StyleSheet.create({
  base: { color: TEXT },
  reference: { color: LINK },
  tag: { color: TAG, fontWeight: "600" },
});
