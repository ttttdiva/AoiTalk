import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Keyboard, Pressable, StyleSheet, TextInput, View } from "react-native";
import * as Clipboard from "expo-clipboard";
import Markdown from "react-native-markdown-display";
import { Button, IconButton, Text } from "react-native-paper";
import type { DocsNode } from "../../types/api";
import { DocBlockDraft, readDocBlock } from "./doc-block-drafts";
import { showOutlineInputKeyboard } from "./outline-native-keyboard";

export { readDocBlock } from "./doc-block-drafts";
export type { DocBlock, DocBlockType } from "./doc-block-drafts";
export function isDocBlockNode(node: DocsNode): boolean { return readDocBlock(node) !== null; }

type Props = {
  node: DocsNode;
  testIdPrefix?: string;
  onSaved?: (node: DocsNode) => void;
  readOnly?: boolean;
  draftSession?: DocBlockDraft;
  onEdit?: () => void;
};

/** Keep a replaced node's async work and input lifetime separate. */
export function DocBlockEditor(props: Props) {
  return <DocBlockBody key={`${props.node.workspace_id}:${props.node.project_id}:${props.node.id}`} {...props} />;
}

function DocBlockBody({ node, testIdPrefix = "docs-block", onSaved, readOnly = false, draftSession, onEdit }: Props) {
  const [ownedDraft] = useState(() => new DocBlockDraft(node));
  const draft = draftSession ?? ownedDraft;
  const [, redraw] = useState(0);
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState("");
  const inputRef = useRef<TextInput | null>(null);
  const focusPending = useRef(false);
  const nativeFocusIssued = useRef(false);
  const lastEditRequest = useRef(0);
  const keyboardCancel = useRef<(() => void) | null>(null);
  const mounted = useRef(true);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;
  const canEdit = draft.canEdit && !readOnly && node.access !== "read" && !node.read_only && !node.archived_at;
  const canEditRef = useRef(canEdit);
  canEditRef.current = canEdit;
  const block = readDocBlock(node);

  useLayoutEffect(() => {
    draft.update(node, readOnly);
  }, [draft, node, readOnly]);
  useEffect(() => {
    const unsubscribe = draft.subscribe(() => redraw((value) => value + 1));
    const unsubscribeSaved = draft.subscribeSaved((updated) => savedCallback.current?.(updated));
    return () => { unsubscribe(); unsubscribeSaved(); };
  }, [draft]);
  useEffect(() => {
    mounted.current = true;
    const hide = Keyboard.addListener("keyboardDidHide", () => {
      keyboardCancel.current?.();
      keyboardCancel.current = null;
    });
    return () => {
      mounted.current = false;
      focusPending.current = false;
      keyboardCancel.current?.();
      keyboardCancel.current = null;
      hide.remove();
      // The screen owns shared sessions, including failures while a row is
      // virtualized. A later flush/remount can still retry the same raw draft.
      void draft.flush().catch(() => undefined);
    };
  }, [draft]);
  useEffect(() => {
    if (canEdit) return;
    focusPending.current = false;
    keyboardCancel.current?.();
    keyboardCancel.current = null;
  }, [canEdit]);

  useEffect(() => {
    if (draft.editRequest <= lastEditRequest.current) return;
    lastEditRequest.current = draft.editRequest;
    beginEditing();
  }, [draft, draft.editRequest]);

  const beginEditing = () => {
    if (!canEditRef.current) return;
    onEdit?.();
    focusPending.current = true;
    nativeFocusIssued.current = false;
    setEditing(true);
    // An already mounted editor can have lost focus after a native selection
    // or Android Back. Do not reset its value/selection to reopen the keyboard.
    if (inputRef.current) {
      if (inputRef.current.isFocused?.()) {
        focusPending.current = false;
        keyboardCancel.current?.();
        keyboardCancel.current = showOutlineInputKeyboard(inputRef.current);
      } else { nativeFocusIssued.current = true; inputRef.current.focus(); }
    }
  };
  const save = async () => {
    try {
      await draft.flush();
      if (mounted.current && !draft.pending) setEditing(false);
    } catch { /* The session retains the raw draft and displays its error. */ }
  };
  const copy = async () => {
    try {
      await Clipboard.setStringAsync(draft.draft);
      if (mounted.current) setNotice("本文をコピーしました");
    } catch {
      if (mounted.current) setNotice("コピーに失敗しました。もう一度お試しください");
    }
  };

  if (!block) return null;
  return (
    <View style={styles.container} testID={`${testIdPrefix}-${node.id}`}>
      <View style={styles.bodyRow}>
        {editing ? (
          <TextInput
            ref={inputRef}
            testID={`${testIdPrefix}-editor-${node.id}`}
            accessibilityLabel={`${block.label}${canEdit ? "本文を編集" : "本文"}`}
            value={draft.draft}
            onChangeText={(value) => { setNotice(""); draft.change(value); }}
            onBlur={() => {
              focusPending.current = false;
              nativeFocusIssued.current = false;
              keyboardCancel.current?.();
              keyboardCancel.current = null;
              void draft.flush().catch(() => undefined);
            }}
            onLayout={() => {
              if (focusPending.current && !nativeFocusIssued.current && canEditRef.current && inputRef.current) {
                nativeFocusIssued.current = true;
                inputRef.current.focus();
              }
            }}
            onFocus={() => {
              if (!focusPending.current || !canEditRef.current || !inputRef.current) return;
              focusPending.current = false;
              keyboardCancel.current?.();
              keyboardCancel.current = showOutlineInputKeyboard(inputRef.current);
            }}
            editable={canEdit}
            multiline
            scrollEnabled={false}
            rejectResponderTermination={false}
            style={[styles.editor, block.type === "code" ? styles.code : null]}
            textAlignVertical="top"
            placeholder="本文を入力"
            placeholderTextColor="#6c7086"
            underlineColorAndroid="transparent"
            selectionColor="#89b4fa"
            autoCapitalize="none"
            autoCorrect={false}
          />
        ) : (
          <Pressable
            testID={`${testIdPrefix}-display-${node.id}`}
            accessibilityRole={canEdit ? "button" : undefined}
            accessibilityLabel={`${block.label}${canEdit ? "本文を編集" : "本文"}`}
            onPress={canEdit ? beginEditing : undefined}
            style={styles.display}
          >
            {draft.draft === "" ? <Text style={styles.placeholder}>本文を入力</Text>
              : block.type === "markdown" ? <Markdown style={markdownStyles} onLinkPress={canEdit ? () => { beginEditing(); return false; } : undefined}>{draft.draft}</Markdown>
                : <Text selectable style={styles.code}>{draft.draft}</Text>}
          </Pressable>
        )}
        <IconButton
          icon="content-copy"
          size={17}
          iconColor="#a6adc8"
          style={styles.copy}
          testID={`${testIdPrefix}-copy-${node.id}`}
          accessibilityLabel="本文をコピー"
          onPress={() => void copy()}
        />
      </View>
      {editing ? (
        <View style={styles.actions}>
          <Button compact mode="text" disabled={!canEdit || draft.saving} testID={`${testIdPrefix}-save-${node.id}`} onPress={() => void save()}>
            {draft.saving ? "保存中…" : draft.error ? "保存を再試行" : "保存"}
          </Button>
          <Button compact mode="text" disabled={!canEdit || draft.draft === ""} testID={`${testIdPrefix}-clear-${node.id}`} onPress={() => {
            draft.change("");
            setNotice("");
            beginEditing();
            void draft.flush().catch(() => undefined);
          }}>本文を消去</Button>
          <Button compact mode="text" disabled={draft.saving || !draft.dirty} testID={`${testIdPrefix}-cancel-${node.id}`} onPress={() => {
            draft.revert();
            setEditing(false);
            setNotice("");
          }}>未保存の変更を破棄</Button>
        </View>
      ) : null}
      {draft.error ? <Text accessibilityRole="alert" style={styles.error}>{draft.error}</Text> : null}
      {notice ? <Text accessibilityLiveRegion="polite" style={styles.notice}>{notice}</Text> : null}
    </View>
  );
}

const markdownStyles = {
  body: { color: "#cdd6f4", fontSize: 15, lineHeight: 21, marginTop: 0, marginBottom: 0 },
  heading1: { color: "#cdd6f4", fontSize: 18, marginTop: 3, marginBottom: 3 },
  heading2: { color: "#cdd6f4", fontSize: 17, marginTop: 3, marginBottom: 3 },
  heading3: { color: "#cdd6f4", fontSize: 15, marginTop: 3, marginBottom: 3 },
  paragraph: { marginTop: 0, marginBottom: 3 },
  table: { borderWidth: StyleSheet.hairlineWidth, borderColor: "#585b70" },
  th: { backgroundColor: "#313244", color: "#cdd6f4", padding: 5 },
  td: { color: "#cdd6f4", padding: 5 },
  code_block: { backgroundColor: "#181825", color: "#cdd6f4", fontFamily: "monospace" },
} as const;

const styles = StyleSheet.create({
  container: { marginBottom: 2 },
  bodyRow: { flexDirection: "row", alignItems: "flex-start" },
  display: { flex: 1, minHeight: 30, paddingVertical: 3 },
  editor: { flex: 1, minHeight: 30, margin: 0, paddingHorizontal: 0, paddingVertical: 3, color: "#cdd6f4", fontSize: 15, lineHeight: 21 },
  code: { color: "#cdd6f4", fontFamily: "monospace", fontSize: 13, lineHeight: 19 },
  placeholder: { color: "#6c7086", fontSize: 15, lineHeight: 21 },
  copy: { margin: 0, width: 28, height: 30 },
  actions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center" },
  error: { color: "#f38ba8", fontSize: 12, marginBottom: 4 },
  notice: { color: "#a6adc8", fontSize: 12, marginBottom: 4 },
});
