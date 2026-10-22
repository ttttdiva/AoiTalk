import React from "react";
import { ScrollView, View } from "react-native";
import { Button, Dialog, RadioButton, Text } from "react-native-paper";
import type { useChatIntent } from "../use-chat-intent";
import { chatScreenStyles as styles } from "./chat-screen.styles";

export function ChatIntentDialog({ intent }: { intent: ReturnType<typeof useChatIntent> }) {
  if (!intent.visible) return null;
  return (
    <Dialog visible dismissable={!intent.sending} onDismiss={intent.close} style={styles.dialog}>
      <Dialog.Title style={styles.dialogTitle}>どの意図で伝えますか？</Dialog.Title>
      <Dialog.Content>
        <Text style={styles.dialogHelp}>
          AI推定です。実際の意図を選んでください。本文は変えず、意図タグを添えます。
        </Text>
        {intent.loading && <Text accessibilityRole="text">意図を推定中…</Text>}
        {intent.error && <Text accessibilityRole="alert" style={{ color: "#f38ba8" }}>{intent.error}</Text>}
      </Dialog.Content>
      <Dialog.ScrollArea style={[styles.dialogScrollArea, { maxHeight: 300 }]}>
        <ScrollView>
          {intent.result?.candidates.map((candidate) => (
            <View key={candidate.id}>
              <RadioButton.Item label={`${candidate.label}  ${Math.round(candidate.score * 100)}%`}
                accessibilityLabel={`${candidate.label} AI推定 ${Math.round(candidate.score * 100)}%`}
                value={candidate.id} status={intent.selectedId === candidate.id ? "checked" : "unchecked"}
                disabled={intent.sending} onPress={() => intent.select(candidate.id)}
                style={{ paddingVertical: 0, minHeight: 40 }}
                labelStyle={{ color: "#cdd6f4", fontSize: 14 }} color="#89b4fa" />
            </View>
          ))}
        </ScrollView>
      </Dialog.ScrollArea>
      <Dialog.Actions>
        <Button onPress={intent.close} disabled={intent.sending}>キャンセル</Button>
        {intent.error && !intent.result && <Button onPress={intent.open} disabled={intent.loading || !intent.canRetry}>再試行</Button>}
        <Button onPress={() => void intent.send()} disabled={!intent.canSend || !intent.selectedId || intent.loading || intent.sending}>
          {intent.sending ? "送信中…" : "この意図で送信"}
        </Button>
      </Dialog.Actions>
    </Dialog>
  );
}
