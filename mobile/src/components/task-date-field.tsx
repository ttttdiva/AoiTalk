/**
 * タスク用のコンパクトな日付入力フィールド。
 *
 * - タップでカレンダーダイアログを開き、日付（終日でなければ時刻も）を選択できる。
 * - 値があるときは「M/d HH:mm」等の短い表記で表示する。
 * - クリア可能。終日（allDay）に対応。
 * - 入出力の文字列形式は task-datetime.ts の想定に合わせる:
 *     終日          -> "yyyy-MM-dd"
 *     終日でない場合 -> "yyyy-MM-ddTHH:mm"
 *   これは [taskId].tsx の formatTaskDateInput が生成する形式と一致するため、
 *   自動保存の未保存判定（署名比較）を崩さない。
 */
import React, { useMemo, useState } from "react";
import { StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";
import {
  Button,
  Dialog,
  IconButton,
  Portal,
  Text,
  TouchableRipple,
} from "react-native-paper";
import { Calendar } from "react-native-calendars";

type TaskDateFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  allDay: boolean;
  style?: StyleProp<ViewStyle>;
};

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function parseValue(value: string): {
  date: string;
  hour: number;
  minute: number;
} {
  if (!value) return { date: "", hour: 9, minute: 0 };
  const [datePart, timePart] = value.split("T");
  if (!timePart) return { date: datePart, hour: 9, minute: 0 };
  const [hh, mm] = timePart.split(":");
  const hour = Number(hh);
  const minute = Number(mm);
  return {
    date: datePart,
    hour: Number.isFinite(hour) ? hour : 9,
    minute: Number.isFinite(minute) ? minute : 0,
  };
}

function formatShort(value: string, allDay: boolean): string {
  if (!value) return "";
  const [datePart, timePart] = value.split("T");
  const [, month, day] = datePart.split("-");
  const md = `${Number(month)}/${Number(day)}`;
  if (allDay || !timePart) return md;
  const [hh, mm] = timePart.split(":");
  return `${md} ${hh}:${mm}`;
}

export function TaskDateField({
  label,
  value,
  onChange,
  allDay,
  style,
}: TaskDateFieldProps) {
  const [visible, setVisible] = useState(false);
  const [localDate, setLocalDate] = useState("");
  const [localHour, setLocalHour] = useState(9);
  const [localMinute, setLocalMinute] = useState(0);

  const display = useMemo(() => formatShort(value, allDay), [value, allDay]);

  const open = () => {
    const parsed = parseValue(value);
    setLocalDate(parsed.date);
    setLocalHour(parsed.hour);
    setLocalMinute(parsed.minute);
    setVisible(true);
  };

  const close = () => setVisible(false);

  const commit = () => {
    if (!localDate) {
      onChange("");
    } else if (allDay) {
      onChange(localDate);
    } else {
      onChange(`${localDate}T${pad(localHour)}:${pad(localMinute)}`);
    }
    setVisible(false);
  };

  const clear = () => {
    onChange("");
    setVisible(false);
  };

  const adjustHour = (delta: number) => {
    setLocalHour((current) => (current + delta + 24) % 24);
  };

  const adjustMinute = (delta: number) => {
    setLocalMinute((current) => {
      const snapped = Math.round(current / 5) * 5;
      return (snapped + delta + 60) % 60;
    });
  };

  return (
    <View style={style}>
      <TouchableRipple onPress={open} style={styles.field} borderless>
        <View>
          <Text style={styles.fieldLabel}>{label}</Text>
          <View style={styles.fieldValueRow}>
            <Text
              style={[
                styles.fieldValue,
                !value && styles.fieldPlaceholder,
              ]}
              numberOfLines={1}
            >
              {display || "未設定"}
            </Text>
            {value ? (
              <IconButton
                icon="close"
                size={16}
                onPress={() => onChange("")}
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
      </TouchableRipple>

      <Portal>
        <Dialog visible={visible} onDismiss={close} style={styles.dialog}>
          <Dialog.Title style={styles.dialogTitle}>{label}</Dialog.Title>
          <Dialog.Content>
            <Calendar
              current={localDate || undefined}
              markedDates={
                localDate ? { [localDate]: { selected: true } } : {}
              }
              onDayPress={(day: { dateString: string }) =>
                setLocalDate(day.dateString)
              }
              firstDay={0}
              enableSwipeMonths
              theme={{
                calendarBackground: "#1e1e2e",
                monthTextColor: "#cdd6f4",
                textMonthFontWeight: "bold",
                textMonthFontSize: 16,
                arrowColor: "#7c3aed",
                todayTextColor: "#7c3aed",
                todayBackgroundColor: "transparent",
                dayTextColor: "#cdd6f4",
                textDisabledColor: "#585b70",
                selectedDayBackgroundColor: "#4c1d95",
                selectedDayTextColor: "#cdd6f4",
                textDayFontSize: 14,
                textDayHeaderFontSize: 12,
                textSectionTitleColor: "#a6adc8",
              }}
            />
            {!allDay ? (
              <View style={styles.timeRow}>
                <Text style={styles.timeLabel}>時刻</Text>
                <View style={styles.stepper}>
                  <IconButton
                    icon="chevron-down"
                    size={18}
                    iconColor="#89b4fa"
                    onPress={() => adjustHour(-1)}
                    style={styles.stepButton}
                  />
                  <Text style={styles.timeValue}>{pad(localHour)}</Text>
                  <IconButton
                    icon="chevron-up"
                    size={18}
                    iconColor="#89b4fa"
                    onPress={() => adjustHour(1)}
                    style={styles.stepButton}
                  />
                </View>
                <Text style={styles.timeColon}>:</Text>
                <View style={styles.stepper}>
                  <IconButton
                    icon="chevron-down"
                    size={18}
                    iconColor="#89b4fa"
                    onPress={() => adjustMinute(-5)}
                    style={styles.stepButton}
                  />
                  <Text style={styles.timeValue}>{pad(localMinute)}</Text>
                  <IconButton
                    icon="chevron-up"
                    size={18}
                    iconColor="#89b4fa"
                    onPress={() => adjustMinute(5)}
                    style={styles.stepButton}
                  />
                </View>
              </View>
            ) : null}
          </Dialog.Content>
          <Dialog.Actions>
            <Button onPress={clear} textColor="#f38ba8">
              クリア
            </Button>
            <Button onPress={close} textColor="#a6adc8">
              キャンセル
            </Button>
            <Button onPress={commit} textColor="#89b4fa" disabled={!localDate}>
              決定
            </Button>
          </Dialog.Actions>
        </Dialog>
      </Portal>
    </View>
  );
}

const styles = StyleSheet.create({
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
  fieldPlaceholder: { color: "#585b70" },
  trailingButton: { margin: 0, marginRight: -6 },
  dialog: { backgroundColor: "#1e1e2e" },
  dialogTitle: { color: "#cdd6f4" },
  timeRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    marginTop: 14,
  },
  timeLabel: { color: "#a6adc8", fontSize: 14, marginRight: 8 },
  stepper: { alignItems: "center" },
  stepButton: { margin: 0 },
  timeValue: {
    color: "#cdd6f4",
    fontSize: 20,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    minWidth: 32,
    textAlign: "center",
  },
  timeColon: { color: "#cdd6f4", fontSize: 20, fontWeight: "700" },
});
