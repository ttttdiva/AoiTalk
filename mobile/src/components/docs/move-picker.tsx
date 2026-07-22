/**
 * 移動先ピッカー
 *
 * ノードツリー（ページ→子）を辿って移動先を選択する。
 * `leave_reference` は Switch で指定（既定 off）。
 */

import React, { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import {
  Button,
  Dialog,
  IconButton,
  Portal,
  Switch,
  Text,
} from "react-native-paper";
import { docsRepo } from "../../repositories/docs";
import type { DocsNode } from "../../types/api";

type MovePickerProps = {
  visible: boolean;
  /** 移動対象ノード（自分自身は移動先に選べない） */
  currentNodeId: string;
  onDismiss: () => void;
  onConfirm: (targetId: string, leaveReference: boolean) => void;
};

const MAX_DEPTH = 6;

function TreeRow({
  node,
  depth,
  selectedId,
  currentNodeId,
  onSelect,
}: {
  node: DocsNode;
  depth: number;
  selectedId: string | null;
  currentNodeId: string;
  onSelect: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<DocsNode[]>([]);
  const [loaded, setLoaded] = useState(false);

  const toggle = useCallback(async () => {
    if (!expanded && !loaded) {
      try {
        const rows = await docsRepo.listChildren(node.id);
        setChildren(rows.filter((row) => !row.archived_at));
      } catch {
        setChildren([]);
      }
      setLoaded(true);
    }
    setExpanded((prev) => !prev);
  }, [expanded, loaded, node.id]);

  const disabled = node.id === currentNodeId;
  const selected = selectedId === node.id;

  return (
    <View>
      <View style={[styles.row, { paddingLeft: 8 + depth * 16 }]}>
        <IconButton
          icon={expanded ? "chevron-down" : "chevron-right"}
          size={18}
          iconColor="#a6adc8"
          style={styles.chevron}
          disabled={depth >= MAX_DEPTH}
          onPress={() => void toggle()}
        />
        <Button
          mode={selected ? "contained" : "text"}
          compact
          textColor={selected ? "#f5e9ff" : disabled ? "#585b70" : "#cdd6f4"}
          buttonColor={selected ? "#7c3aed" : undefined}
          style={styles.rowButton}
          contentStyle={styles.rowButtonContent}
          disabled={disabled}
          onPress={() => onSelect(node.id)}
        >
          {node.title || "無題"}
        </Button>
      </View>
      {expanded
        ? children.map((child) => (
            <TreeRow
              key={child.id}
              node={child}
              depth={depth + 1}
              selectedId={selectedId}
              currentNodeId={currentNodeId}
              onSelect={onSelect}
            />
          ))
        : null}
    </View>
  );
}

export function MovePicker({
  visible,
  currentNodeId,
  onDismiss,
  onConfirm,
}: MovePickerProps) {
  const [pages, setPages] = useState<DocsNode[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [leaveReference, setLeaveReference] = useState(false);

  useEffect(() => {
    if (!visible) return;
    setSelectedId(null);
    setLeaveReference(false);
    let active = true;
    void docsRepo
      .listPages()
      .then((rows) => {
        if (active) setPages(rows);
      })
      .catch(() => {
        if (active) setPages([]);
      });
    return () => {
      active = false;
    };
  }, [visible]);

  return (
    <Portal>
      <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
        <Dialog.Title style={styles.title}>移動先を選択</Dialog.Title>
        <Dialog.ScrollArea style={styles.scrollArea}>
          <ScrollView>
            {pages.length === 0 ? (
              <Text style={styles.empty}>ページがありません</Text>
            ) : (
              pages.map((page) => (
                <TreeRow
                  key={page.id}
                  node={page}
                  depth={0}
                  selectedId={selectedId}
                  currentNodeId={currentNodeId}
                  onSelect={setSelectedId}
                />
              ))
            )}
          </ScrollView>
        </Dialog.ScrollArea>
        <View style={styles.switchRow}>
          <Text style={styles.switchLabel}>参照を元の場所に残す</Text>
          <Switch value={leaveReference} onValueChange={setLeaveReference} />
        </View>
        <Dialog.Actions>
          <Button onPress={onDismiss} textColor="#a6adc8">
            キャンセル
          </Button>
          <Button
            textColor="#7c3aed"
            disabled={!selectedId}
            onPress={() => {
              if (selectedId) onConfirm(selectedId, leaveReference);
            }}
          >
            移動
          </Button>
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

const styles = StyleSheet.create({
  dialog: { backgroundColor: "#1e1e2e" },
  title: { color: "#cdd6f4" },
  scrollArea: { maxHeight: 320, paddingHorizontal: 0 },
  row: { flexDirection: "row", alignItems: "center" },
  chevron: { margin: 0 },
  rowButton: { flex: 1 },
  rowButtonContent: { justifyContent: "flex-start" },
  empty: { color: "#a6adc8", textAlign: "center", paddingVertical: 24 },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 24,
    paddingVertical: 8,
  },
  switchLabel: { color: "#a6adc8", fontSize: 13 },
});
