/**
 * タスクの開始日時・期限を入力するフィールド。
 *
 * 入力値の解釈は PC と共有する pure parser に任せ、保存値は
 * `yyyy-MM-dd` または `yyyy-MM-ddTHH:mm` の wall-clock のまま保持する。
 * ダイアログ内では編集中の値だけを保持し、決定するまで親へ通知しない。
 */
import React, { useMemo, useState } from "react";
import {
  ScrollView,
  StyleSheet,
  useWindowDimensions,
  View,
  type StyleProp,
  type ViewStyle,
} from "react-native";
import {
  Button,
  Dialog,
  Icon,
  IconButton,
  Switch,
  Portal,
  Text,
  TextInput,
  TouchableRipple,
} from "react-native-paper";
import { parseFlexibleTaskDate } from "../../../shared/task-date-parser";

type TaskDateFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  allDay: boolean;
  style?: StyleProp<ViewStyle>;
  compact?: boolean;
  onAllDayChange?: (value: boolean) => void;
  onOpenRecurrence?: () => void;
  recurrenceLabel?: string;
  recurrenceDisabled?: boolean;
};

type ParsedTaskDate = {
  value: string;
  dateOnly: boolean;
};

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatShort(value: string, allDay: boolean): string {
  if (!value) return "";
  const [datePart, timePart] = value.split("T");
  const [, month, day] = datePart.split("-");
  if (!month || !day) return value;
  const md = `${Number(month)}/${Number(day)}`;
  if (allDay || !timePart) return md;
  const [hh, mm] = timePart.split(":");
  return `${md} ${hh}:${mm}`;
}

function localTimezoneLabel(value: string): string {
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/,
  );
  const date = match
    ? new Date(
        Number(match[1]),
        Number(match[2]) - 1,
        Number(match[3]),
        Number(match[4] ?? 0),
        Number(match[5] ?? 0),
      )
    : new Date();
  const timezone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "ローカル";
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absoluteOffset = Math.abs(offsetMinutes);
  return `${timezone} (UTC${sign}${pad(Math.floor(absoluteOffset / 60))}:${pad(
    absoluteOffset % 60,
  )})`;
}

function resolveForMode(
  raw: string,
  now: Date,
  allDay: boolean,
): ParsedTaskDate | null {
  const parsed = parseFlexibleTaskDate(raw, now);
  if (!parsed) return null;

  // allDay is a field-level contract. Do not silently discard a typed time;
  // ask the user to switch off all-day before saving a timed value.
  if (allDay && !parsed.dateOnly) {
    return null;
  }
  // The shared parser keeps the resolved civil time at midnight for date-only
  // expressions so it can resolve weekdays uniformly. The task contract stores
  // date-only values without a synthetic T00:00 suffix.
  if (parsed.dateOnly) {
    return { value: parsed.value.slice(0, 10), dateOnly: true };
  }
  return parsed;
}

export function TaskDateField({
  label,
  value,
  onChange,
  allDay,
  style,
  compact = false,
  onAllDayChange,
  onOpenRecurrence,
  recurrenceLabel = "なし",
  recurrenceDisabled = false,
}: TaskDateFieldProps) {
  const [visible, setVisible] = useState(false);
  const [localAllDay, setLocalAllDay] = useState(allDay);
  const [draftText, setDraftText] = useState("");
  const [parseNow, setParseNow] = useState<Date>(() => new Date());
  const { height } = useWindowDimensions();

  const display = useMemo(() => formatShort(value, allDay), [value, allDay]);
  const resolved = useMemo(
    () => resolveForMode(draftText, parseNow, localAllDay),
    [draftText, localAllDay, parseNow],
  );

  const open = () => {
    setDraftText(value);
    setLocalAllDay(allDay);
    setParseNow(new Date());
    setVisible(true);
  };

  const cancel = () => {
    setVisible(false);
  };

  const commit = () => {
    if (!resolved) return;
    if (localAllDay !== allDay) onAllDayChange?.(localAllDay);
    onChange(resolved.value);
    setVisible(false);
  };

  const clear = () => {
    onChange("");
    setVisible(false);
  };

  const openRecurrence = () => {
    // Recurrence is a separate editor. Keep a valid date draft before
    // entering it, while an unfinished/invalid text draft is discarded.
    if (resolved) {
      commit();
    } else {
      cancel();
    }
    onOpenRecurrence?.();
  };

  return (
    <View style={style}>
      <TouchableRipple
        onPress={open}
        style={[styles.field, compact && styles.compactField]}
        borderless
        accessibilityRole="button"
        accessibilityLabel={`${label}: ${display || "未設定"}`}
      >
        {compact ? (
          <View style={styles.compactRow}>
            <Icon source="calendar-blank-outline" size={16} color="#9399b2" />
            <Text style={styles.fieldLabel}>{label}</Text>
            <Text
              style={[styles.compactValue, !value && styles.fieldPlaceholder]}
              numberOfLines={1}
            >
              {display || "未設定"}
            </Text>
          </View>
        ) : (
          <View>
            <Text style={styles.fieldLabel}>{label}</Text>
            <View style={styles.fieldValueRow}>
              <Text
                style={[styles.fieldValue, !value && styles.fieldPlaceholder]}
                numberOfLines={1}
              >
                {display || "未設定"}
              </Text>
              {value ? (
                <IconButton
                  icon="close"
                  size={16}
                  accessibilityLabel={`${label}をクリア`}
                  onPress={(event) => {
                    event.stopPropagation();
                    onChange("");
                  }}
                  style={styles.trailingButton}
                  iconColor="#9399b2"
                />
              ) : (
                <IconButton
                  icon="calendar-blank-outline"
                  size={16}
                  disabled
                  style={styles.trailingButton}
                  iconColor="#585b70"
                />
              )}
            </View>
          </View>
        )}
      </TouchableRipple>

      <Portal>
        <Dialog visible={visible} onDismiss={cancel} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>{label}</Dialog.Title>
          <Dialog.ScrollArea style={{ maxHeight: height * 0.65 }}>
            <ScrollView
              contentContainerStyle={styles.dialogScroll}
              keyboardShouldPersistTaps="handled"
            >
              {onAllDayChange ? (
                <View style={styles.scheduleRow}>
                  <Text style={styles.timeLabel}>終日</Text>
                  <Switch
                    accessibilityLabel="終日"
                    value={localAllDay}
                    onValueChange={setLocalAllDay}
                  />
                </View>
              ) : null}

              <TextInput
                accessibilityLabel={`${label}の日付/時刻入力`}
                testID={`${label}-date-input`}
                mode="outlined"
                label="日付 / 時刻"
                placeholder="Tomorrow 12:00 / Friday / 3 days"
                value={draftText}
                onChangeText={setDraftText}
                autoFocus
                autoCorrect={false}
                autoCapitalize="none"
                style={styles.input}
              />
              <Text style={styles.inputHint}>
                例: Tomorrow 12:00、Friday、3 days、2026-08-04T09:30
              </Text>

              <View
                style={[styles.preview, !resolved && styles.previewInvalid]}
                accessibilityLiveRegion="polite"
              >
                <Text style={styles.previewTitle}>解釈結果</Text>
                {resolved ? (
                  <>
                    <Text
                      testID={`${label}-date-preview`}
                      style={styles.previewValue}
                    >
                      {resolved.value}
                    </Text>
                    <Text style={styles.previewMeta}>
                      {resolved.dateOnly ? "日付のみ" : "年月日と時刻"} ・ 端末のローカル時刻として保存
                    </Text>
                    <Text
                      testID={`${label}-timezone-preview`}
                      style={styles.previewMeta}
                    >
                      ローカルタイムゾーン: {localTimezoneLabel(resolved.value)}
                    </Text>
                  </>
                ) : (
                  <Text style={styles.invalidText}>
                    入力を解釈できません。日付/時刻を入力してください。
                  </Text>
                )}
              </View>

              {onOpenRecurrence ? (
                <TouchableRipple
                  accessibilityRole="button"
                  accessibilityLabel={`繰り返し: ${recurrenceLabel}`}
                  disabled={recurrenceDisabled}
                  onPress={openRecurrence}
                >
                  <View style={styles.scheduleRow}>
                    <Icon source="repeat" size={18} color="#a6adc8" />
                    <Text style={styles.scheduleTitle}>繰り返し</Text>
                    <Text style={styles.timeLabel}>{recurrenceLabel}</Text>
                    <Icon source="chevron-right" size={18} color="#9399b2" />
                  </View>
                </TouchableRipple>
              ) : null}
            </ScrollView>
          </Dialog.ScrollArea>
          <Dialog.Actions>
            <Button onPress={clear} textColor="#f38ba8">
              クリア
            </Button>
            <Button onPress={cancel} textColor="#a6adc8">
              キャンセル
            </Button>
            <Button
              onPress={commit}
              textColor="#89b4fa"
              disabled={!resolved}
            >
              決定
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
  compactField: {
    minHeight: 44,
    paddingVertical: 0,
    borderWidth: 0,
    borderRadius: 8,
  },
  compactRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  compactValue: { flex: 1, minWidth: 0, color: "#cdd6f4", fontSize: 13 },
  dialogScroll: { paddingHorizontal: 16, paddingBottom: 8 },
  scheduleRow: {
    minHeight: 44,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  scheduleTitle: { flex: 1, color: "#cdd6f4", fontSize: 14 },
  field: {
    borderWidth: 1,
    borderColor: "#313244",
    borderRadius: 8,
    backgroundColor: "#181825",
    paddingHorizontal: 10,
    paddingVertical: 6,
    minHeight: 52,
    justifyContent: "center",
  },
  fieldLabel: { color: "#9399b2", fontSize: 11 },
  fieldValueRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  fieldValue: { color: "#cdd6f4", fontSize: 15, flex: 1 },
  fieldPlaceholder: { color: "#9399b2" },
  trailingButton: { margin: 0, marginRight: -6 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  input: { marginTop: 6, backgroundColor: "#1e1e2e" },
  inputHint: { color: "#9399b2", fontSize: 12, marginTop: 6 },
  preview: {
    borderWidth: 1,
    borderColor: "#45475a",
    borderRadius: 8,
    padding: 10,
    marginTop: 14,
    gap: 4,
  },
  previewInvalid: { borderColor: "#6c3448" },
  previewTitle: { color: "#a6adc8", fontSize: 12, fontWeight: "700" },
  previewValue: {
    color: "#cdd6f4",
    fontSize: 16,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  previewMeta: { color: "#a6adc8", fontSize: 12 },
  invalidText: { color: "#f38ba8", fontSize: 13 },
  timeLabel: { color: "#a6adc8", fontSize: 14, marginRight: 8 },
});
