import React, { useCallback, useEffect, useRef, useState } from "react";
import { StyleSheet } from "react-native";
import { Button, Dialog, Text, TextInput } from "react-native-paper";

export type FileNameDialogProps = {
  visible: boolean;
  title: string;
  label: string;
  helperText?: string;
  initialValue?: string;
  submitLabel: string;
  onDismiss: () => void;
  onSubmit: (name: string) => void | Promise<void>;
};

export function FileNameDialog({
  visible,
  title,
  label,
  helperText,
  initialValue = "",
  submitLabel,
  onDismiss,
  onSubmit,
}: FileNameDialogProps) {
  const [draft, setDraft] = useState(initialValue);
  const [submitting, setSubmitting] = useState(false);
  // Android の TextInput は IME の合成中に controlled `value` を更新すると、
  // ネイティブ側の未確定文字列を一文字へ巻き戻すことがある。表示値は
  // native input に保持させ、submit 時はイベントで受け取った最新値を ref
  // から読むことで、日本語入力・貼り付けを含む入力全体を失わないようにする。
  const draftRef = useRef(initialValue);

  useEffect(() => {
    if (visible) {
      draftRef.current = initialValue;
      setDraft(initialValue);
      setSubmitting(false);
    }
  }, [initialValue, visible]);

  const trimmedDraft = draft.trim();

  const handleChangeText = useCallback((value: string) => {
    draftRef.current = value;
    // ボタンの disabled 状態だけを更新する。TextInput 自体は uncontrolled
    // のため、IME の合成イベントを React の再描画で上書きしない。
    setDraft(value);
  }, []);

  const submit = async () => {
    const name = draftRef.current.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(name);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
      <Dialog.Title style={styles.dialogTitle}>{title}</Dialog.Title>
      <Dialog.Content>
        {helperText ? (
          <Text style={styles.dialogLabel} numberOfLines={2}>
            {helperText}
          </Text>
        ) : null}
        <TextInput
          // visible/initialValue が変わったときだけ native input を再生成する。
          // 入力中の再描画では key が変わらず、入力全体と IME composition を保持する。
          key={`${visible ? "open" : "closed"}:${initialValue}`}
          mode="outlined"
          label={label}
          defaultValue={initialValue}
          onChangeText={handleChangeText}
          testID="file-name-input"
          style={styles.dialogInput}
          autoCorrect={false}
          autoCapitalize="none"
        />
      </Dialog.Content>
      <Dialog.Actions>
        <Button onPress={onDismiss} textColor="#a6adc8" disabled={submitting}>
          キャンセル
        </Button>
        <Button
          onPress={() => void submit()}
          textColor="#7c3aed"
          disabled={!trimmedDraft || submitting}
        >
          {submitLabel}
        </Button>
      </Dialog.Actions>
    </Dialog>
  );
}

const styles = StyleSheet.create({
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  dialogLabel: { color: "#a6adc8", fontSize: 12, marginBottom: 12 },
  dialogInput: { backgroundColor: "#1e1e2e" },
});
