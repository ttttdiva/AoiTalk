/**
 * アウトラインツリー
 *
 * 指定ノードの子孫を再帰描画（depth 上限 6）。各行で以下を操作できる:
 *  - シェブロンで折りたたみ（ローカル state 管理）
 *  - タップで詳細へ遷移
 *  - 行末 + で子ノード作成
 *  - インデント / アウトデント
 *  - overflow メニューで移動・アーカイブ
 * 折りたたみ状態はローカル UI state（サーバ placement.collapsed は読み取り専用）。
 */

import React, { useCallback, useEffect, useState } from "react";
import { StyleSheet, View } from "react-native";
import { ActivityIndicator, IconButton, Menu, Text } from "react-native-paper";
import { Pressable } from "react-native";
import { docsRepo } from "../../repositories/docs";
import type { DocsNode } from "../../types/api";
import { RichTitle } from "./rich-title";

const MAX_DEPTH = 6;

export type OutlineTreeProps = {
  parentId: string;
  depth?: number;
  showArchived: boolean;
  reloadToken: number;
  onOpen: (nodeId: string) => void;
  onMoveRequest: (nodeId: string) => void;
  onChanged: () => void;
};

function OutlineRow({
  node,
  index,
  depth,
  showArchived,
  reloadToken,
  onOpen,
  onMoveRequest,
  onChanged,
}: {
  node: DocsNode;
  index: number;
  depth: number;
  showArchived: boolean;
  reloadToken: number;
  onOpen: (nodeId: string) => void;
  onMoveRequest: (nodeId: string) => void;
  onChanged: () => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [menuVisible, setMenuVisible] = useState(false);
  const [busy, setBusy] = useState(false);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      if (busy) return;
      setBusy(true);
      try {
        await fn();
        onChanged();
      } catch {
        // LWW / オフライン時も outbox 積みのため通常は失敗しないが、防御的に無視
      } finally {
        setBusy(false);
      }
    },
    [busy, onChanged],
  );

  const canExpand = depth < MAX_DEPTH - 1;

  return (
    <View>
      <View style={[styles.row, { paddingLeft: depth * 14 }]}>
        <IconButton
          icon={collapsed ? "chevron-right" : "chevron-down"}
          size={18}
          iconColor="#a6adc8"
          style={styles.icon}
          disabled={!canExpand}
          onPress={() => setCollapsed((prev) => !prev)}
        />
        <Pressable style={styles.titlePress} onPress={() => onOpen(node.id)}>
          <RichTitle
            text={node.title || "無題"}
            numberOfLines={2}
            style={[styles.title, node.archived_at ? styles.archived : null]}
          />
        </Pressable>
        <IconButton
          icon="plus"
          size={18}
          iconColor="#89b4fa"
          style={styles.icon}
          disabled={busy}
          onPress={() =>
            void runAction(() =>
              docsRepo.createNode({ parentId: node.id, title: "" }),
            )
          }
        />
        <IconButton
          icon="format-indent-increase"
          size={18}
          iconColor={index > 0 ? "#a6adc8" : "#45475a"}
          style={styles.icon}
          disabled={busy || index === 0}
          onPress={() => void runAction(() => docsRepo.indentNode(node.id))}
        />
        <IconButton
          icon="format-indent-decrease"
          size={18}
          iconColor={depth > 0 ? "#a6adc8" : "#45475a"}
          style={styles.icon}
          disabled={busy || depth === 0}
          onPress={() => void runAction(() => docsRepo.outdentNode(node.id))}
        />
        <Menu
          visible={menuVisible}
          onDismiss={() => setMenuVisible(false)}
          anchor={
            <IconButton
              icon="dots-vertical"
              size={18}
              iconColor="#a6adc8"
              style={styles.icon}
              onPress={() => setMenuVisible(true)}
            />
          }
          contentStyle={styles.menuContent}
        >
          <Menu.Item
            leadingIcon="folder-move-outline"
            title="移動"
            onPress={() => {
              setMenuVisible(false);
              onMoveRequest(node.id);
            }}
          />
          <Menu.Item
            leadingIcon="archive-outline"
            title="アーカイブ"
            onPress={() => {
              setMenuVisible(false);
              void runAction(() => docsRepo.archiveNode(node.id));
            }}
          />
        </Menu>
      </View>
      {!collapsed && canExpand ? (
        <OutlineTree
          parentId={node.id}
          depth={depth + 1}
          showArchived={showArchived}
          reloadToken={reloadToken}
          onOpen={onOpen}
          onMoveRequest={onMoveRequest}
          onChanged={onChanged}
        />
      ) : null}
    </View>
  );
}

export function OutlineTree({
  parentId,
  depth = 0,
  showArchived,
  reloadToken,
  onOpen,
  onMoveRequest,
  onChanged,
}: OutlineTreeProps) {
  const [children, setChildren] = useState<DocsNode[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void docsRepo
      .listChildren(parentId)
      .then((rows) => {
        if (active) setChildren(rows);
      })
      .catch(() => {
        if (active) setChildren([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [parentId, reloadToken]);

  const visible = children.filter(
    (child) => showArchived || !child.archived_at,
  );

  if (loading && children.length === 0) {
    return depth === 0 ? (
      <ActivityIndicator color="#7c3aed" style={styles.loading} />
    ) : null;
  }

  if (visible.length === 0) {
    return depth === 0 ? (
      <Text style={styles.empty}>子ノードはありません</Text>
    ) : null;
  }

  return (
    <View>
      {visible.map((child, index) => (
        <OutlineRow
          key={child.id}
          node={child}
          index={index}
          depth={depth}
          showArchived={showArchived}
          reloadToken={reloadToken}
          onOpen={onOpen}
          onMoveRequest={onMoveRequest}
          onChanged={onChanged}
        />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center" },
  icon: { margin: 0 },
  titlePress: { flex: 1, paddingVertical: 6, paddingRight: 4 },
  title: { color: "#cdd6f4", fontSize: 14 },
  archived: { color: "#585b70", textDecorationLine: "line-through" },
  menuContent: { backgroundColor: "#1e1e2e" },
  loading: { marginVertical: 16 },
  empty: { color: "#585b70", fontSize: 12, paddingVertical: 8 },
});
