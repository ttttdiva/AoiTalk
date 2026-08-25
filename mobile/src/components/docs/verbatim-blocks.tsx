import React, { useEffect, useRef, useState } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import Markdown from "react-native-markdown-display";
import { Button, Text, TextInput } from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import type { DocsNode } from "../../types/api";

export type DocBlockType = "markdown" | "code";

export type DocBlock = {
  type: DocBlockType;
  label: string;
  content: string;
  clipIngest?: Record<string, unknown>;
};

/** Read only the new editable block contract. Legacy verbatim keys are hidden. */
export function readDocBlock(
  node: Pick<DocsNode, "body_json" | "title">,
): DocBlock | null {
  const body = node.body_json;
  if (!body || body.format !== "doc_block") return null;
  const type = body.block_type;
  if (type !== "markdown" && type !== "code") return null;
  if (typeof body.content !== "string") return null;
  return {
    type,
    label: typeof body.label === "string" && body.label.trim()
      ? body.label
      : node.title || "本文",
    content: body.content.replace(/\r\n?/g, "\n"),
    clipIngest: body.clip_ingest && typeof body.clip_ingest === "object"
      && !Array.isArray(body.clip_ingest)
      ? body.clip_ingest as Record<string, unknown>
      : undefined,
  };
}

export function isDocBlockNode(node: DocsNode): boolean {
  return readDocBlock(node) !== null;
}

type Props = {
  node: DocsNode;
  testIdPrefix?: string;
  onSaved?: (node: DocsNode) => void;
};

/** Inline renderer/editor for a typed Docs block. */
export function DocBlockEditor({ node, testIdPrefix = "docs-block", onSaved }: Props) {
  const block = readDocBlock(node);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(block?.content ?? "");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const canEdit = node.access !== "read" && node.read_only !== true;

  useEffect(() => {
    if (!editing) setDraft(block?.content ?? "");
  }, [block?.content, editing]);

  if (!block) return null;

  const save = async () => {
    if (!canEdit || savingRef.current) return;
    const content = draft.replace(/\r\n?/g, "\n");
    const nextBody = {
      ...(node.body_json ?? {}),
      format: "doc_block",
      block_type: block.type,
      label: block.label,
      content,
    };
    savingRef.current = true;
    setSaving(true);
    try {
      const updated = await docsRepo.updateNode(node.id, { bodyJson: nextBody });
      setDraft(content);
      setEditing(false);
      onSaved?.(updated);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <View style={styles.container} testID={`${testIdPrefix}-${node.id}`}>
      <View style={styles.header}>
        <Text style={styles.label}>{block.label}</Text>
        {editing ? (
          <Button
            compact
            mode="text"
            disabled={saving}
            testID={`${testIdPrefix}-save-${node.id}`}
            onPress={() => void save()}
          >
            保存
          </Button>
        ) : (
          <Text style={styles.type}>{block.type === "markdown" ? "Markdown" : "Code"}</Text>
        )}
      </View>
      {editing ? (
        <TextInput
          testID={`${testIdPrefix}-editor-${node.id}`}
          accessibilityLabel={`${block.label}${canEdit ? "本文を編集" : "本文"}`}
          value={draft}
          onChangeText={setDraft}
          onBlur={() => void save()}
          multiline
          mode="flat"
          dense
          scrollEnabled
          style={styles.editor}
          textAlignVertical="top"
          autoCapitalize="none"
          autoCorrect={false}
        />
      ) : (
        <Pressable
          testID={`${testIdPrefix}-display-${node.id}`}
          accessibilityRole={canEdit ? "button" : undefined}
          accessibilityLabel={`${block.label}${canEdit ? "本文を編集" : "本文"}`}
          onPress={canEdit ? () => setEditing(true) : undefined}
          style={styles.display}
        >
          {block.type === "markdown" ? (
            <Markdown style={markdownStyles}>{block.content}</Markdown>
          ) : (
            <Text selectable style={styles.code}>{block.content}</Text>
          )}
        </Pressable>
      )}
    </View>
  );
}

const markdownStyles = {
  body: { color: "#cdd6f4", fontSize: 14, lineHeight: 21 },
  heading1: { color: "#cdd6f4", fontSize: 20, marginTop: 6, marginBottom: 6 },
  heading2: { color: "#cdd6f4", fontSize: 17, marginTop: 6, marginBottom: 4 },
  heading3: { color: "#cdd6f4", fontSize: 15, marginTop: 5, marginBottom: 3 },
  paragraph: { marginTop: 0, marginBottom: 6 },
  table: { borderWidth: StyleSheet.hairlineWidth, borderColor: "#585b70" },
  th: { backgroundColor: "#313244", color: "#cdd6f4", padding: 5 },
  td: { color: "#cdd6f4", padding: 5 },
  code_block: { backgroundColor: "#181825", color: "#cdd6f4", fontFamily: "monospace" },
} as const;

const styles = StyleSheet.create({
  container: {
    marginHorizontal: 10,
    marginBottom: 8,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: "#45475a",
    borderRadius: 8,
    overflow: "hidden",
    backgroundColor: "#181825",
  },
  header: {
    minHeight: 34,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingLeft: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "#45475a",
  },
  label: { color: "#bac2de", fontSize: 12, fontWeight: "600" },
  type: { color: "#7f849c", fontSize: 11, paddingHorizontal: 10 },
  display: { padding: 10 },
  code: { color: "#cdd6f4", fontFamily: "monospace", fontSize: 13, lineHeight: 19 },
  editor: {
    minHeight: 88,
    paddingHorizontal: 10,
    paddingVertical: 8,
    color: "#cdd6f4",
    backgroundColor: "#11111b",
    fontFamily: "monospace",
    fontSize: 13,
    lineHeight: 19,
  },
});
