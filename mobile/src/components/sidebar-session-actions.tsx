import React, { useEffect, useRef, useState } from "react";
import { ScrollView, StyleSheet } from "react-native";
import { Button, Dialog, List, Portal, Text, TextInput } from "react-native-paper";
import * as Clipboard from "expo-clipboard";
import { conversationsRepo, uploadLocalSession } from "../repositories/conversations";
import { chatRepo } from "../repositories/chat";
import type { ConversationSession } from "../types/api";

type ActionPage = "menu" | "rename" | "delete" | "sync" | "fork";

/** Both sidebar grouping modes share the same durable repository operations. */
export function SidebarSessionActions({
  session,
  authenticated,
  onDismiss,
  onChanged,
  onDeleted,
  onForked,
  onPromoted,
}: {
  session: ConversationSession;
  authenticated: boolean;
  onDismiss: () => void;
  onChanged: () => void;
  onDeleted: (sessionId: string) => void;
  onForked: (session: ConversationSession) => void;
  onPromoted: (sourceId: string, targetId: string) => void;
}) {
  const [page, setPage] = useState<ActionPage>("menu");
  const [title, setTitle] = useState(session.title || "");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const localOnly = !session.user_id || session.is_local_only === true;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const showPage = (next: ActionPage) => {
    if (busyRef.current) return;
    setError(null);
    setNotice(null);
    setPage(next);
  };
  const dismiss = () => { if (!busyRef.current) onDismiss(); };
  const run = async (operation: () => Promise<void>, dismissAfter = true) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await operation();
      if (!mounted.current) return;
      if (dismissAfter) {
        onChanged();
        onDismiss();
      }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "操作に失敗しました。");
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const confirm = () => {
    if (page === "rename") return run(async () => {
      await conversationsRepo.updateTitle(session.id, title.trim(), { requireServerSuccess: true });
    });
    if (page === "delete") return run(async () => {
      await conversationsRepo.deleteSession(session.id);
      if (mounted.current) onDeleted(session.id);
    });
    if (page === "sync") return run(async () => {
      const targetId = await uploadLocalSession(session.id);
      if (mounted.current) onPromoted(session.id, targetId);
    });
    if (page === "fork") return run(async () => {
      const forked = await chatRepo.forkSessionToEnd(session.id);
      if (mounted.current) onForked(forked);
    });
  };
  const titleByPage: Record<ActionPage, string> = {
    menu: "会話の操作", rename: "タイトルを変更", delete: "会話を削除", sync: "サーバーへ同期", fork: "会話をフォーク",
  };

  return (
    <Portal>
      <Dialog visible dismissable={!busy} onDismiss={dismiss} style={styles.dialog}>
        <Dialog.Title>{titleByPage[page]}</Dialog.Title>
        <Dialog.ScrollArea>
          <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
            <Text style={styles.subtitle} numberOfLines={2}>{session.title || "無題の会話"}</Text>
            {page === "menu" ? <>
              <List.Item title="タイトルを編集" disabled={busy} left={(props) => <List.Icon {...props} icon="pencil-outline" />} onPress={() => showPage("rename")} />
              <List.Item
                title={session.is_pinned ? "ピン留めを解除" : "ピン留め"}
                description={localOnly ? "サーバー同期後に利用できます" : !authenticated ? "ログイン後に利用できます" : undefined}
                disabled={busy || localOnly || !authenticated}
                left={(props) => <List.Icon {...props} icon={session.is_pinned ? "pin-off-outline" : "pin-outline"} />}
                onPress={() => void run(async () => { await conversationsRepo.setSessionPinned(session.id, !session.is_pinned); })}
              />
              <List.Item title="フォーク" description="現在の分岐を最後まで複製" disabled={busy} left={(props) => <List.Icon {...props} icon="source-fork" />} onPress={() => showPage("fork")} />
              <List.Item title="セッションIDをコピー" disabled={busy} left={(props) => <List.Icon {...props} icon="content-copy" />} onPress={() => void run(async () => {
                await Clipboard.setStringAsync(session.id);
                if (mounted.current) setNotice("セッションIDをコピーしました");
              }, false)} />
              {localOnly ? <List.Item title="サーバーへ同期" description={!authenticated ? "ログイン後に利用できます" : undefined} disabled={busy || !authenticated} left={(props) => <List.Icon {...props} icon="cloud-upload-outline" />} onPress={() => showPage("sync")} /> : null}
              <List.Item title="削除" titleStyle={styles.error} disabled={busy} left={(props) => <List.Icon {...props} icon="delete-outline" color="#f38ba8" />} onPress={() => showPage("delete")} />
            </> : null}
            {page === "rename" ? <TextInput label="タイトル" accessibilityLabel="会話の新しいタイトル" value={title} onChangeText={setTitle} mode="outlined" maxLength={200} autoFocus disabled={busy} onSubmitEditing={() => { if (title.trim()) void confirm(); }} /> : null}
            {page === "delete" ? <Text>この会話を一覧から削除します。サーバー上の会話は再接続時に削除を同期します。ローカル専用の会話は端末内から削除します。</Text> : null}
            {page === "sync" ? <Text>このローカルチャットをサーバーへ保存し、他の端末からも参照できるようにします。</Text> : null}
            {page === "fork" ? <Text>現在の分岐を最後まで新しい会話へ複製します。サーバー上の会話は完全な履歴を確認するため接続が必要です。</Text> : null}
            {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
            {notice ? <Text accessibilityLiveRegion="polite">{notice}</Text> : null}
          </ScrollView>
        </Dialog.ScrollArea>
        <Dialog.Actions>
          <Button disabled={busy} onPress={dismiss}>{page === "menu" ? "閉じる" : "キャンセル"}</Button>
          {page !== "menu" ? <Button loading={busy} disabled={busy || (page === "rename" && !title.trim())} textColor={page === "delete" ? "#f38ba8" : undefined} onPress={() => void confirm()}>
            {page === "rename" ? "保存" : page === "delete" ? "削除する" : page === "sync" ? "同期" : "フォークする"}
          </Button> : null}
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

const styles = StyleSheet.create({
  dialog: { backgroundColor: "#1e1e2e" },
  content: { paddingVertical: 12 },
  subtitle: { color: "#a6adc8", paddingBottom: 8 },
  error: { color: "#f38ba8", paddingVertical: 8 },
});
