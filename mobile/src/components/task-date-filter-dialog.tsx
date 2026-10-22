/**
 * Task一覧の日付条件ダイアログ。
 * Web の Filter（期限超過のみ / 開始日・期限の条件）に相当する条件を指定する。
 */
import React from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { Button, Chip, Dialog, IconButton, Portal, Switch, Text } from "react-native-paper";
import { TaskDateField } from "./task-date-field";
import {
  EMPTY_TASK_DATE_FILTER,
  TASK_DATE_FIELD_LABELS,
  TASK_DATE_OPS,
  TASK_DATE_OP_LABELS,
  TASK_RELATIVE_DATE_LABELS,
  resolveTaskDateValue,
  taskDateOpNeedsValue,
  type TaskDateField as TaskDateFieldName,
  type TaskDateFilter,
  type TaskDateOp,
  type TaskDateRule,
  type TaskRelativeDate,
} from "../lib/task-date-filter";

const FIELDS = Object.keys(TASK_DATE_FIELD_LABELS) as TaskDateFieldName[];
const RELATIVE = Object.keys(TASK_RELATIVE_DATE_LABELS) as TaskRelativeDate[];

let ruleSeq = 0;
function newRule(): TaskDateRule {
  ruleSeq += 1;
  return { id: `rule-${Date.now()}-${ruleSeq}`, field: "end_at", op: "on_or_before", value: "today" };
}

export function TaskDateFilterDialog({
  visible,
  filter,
  onChange,
  onDismiss,
}: {
  visible: boolean;
  filter: TaskDateFilter;
  onChange: (next: TaskDateFilter) => void;
  onDismiss: () => void;
}) {
  const updateRule = (id: string, patch: Partial<TaskDateRule>) =>
    onChange({
      ...filter,
      rules: filter.rules.map((rule) => {
        if (rule.id !== id) return rule;
        const next = { ...rule, ...patch };
        if (patch.op && !taskDateOpNeedsValue(patch.op)) next.value = "";
        if (patch.op && taskDateOpNeedsValue(patch.op) && !rule.value) next.value = "today";
        return next;
      }),
    });

  return (
    <Portal>
      <Dialog visible={visible} onDismiss={onDismiss} style={styles.dialog}>
        <Dialog.Title>日付・期限の条件</Dialog.Title>
        <Dialog.ScrollArea style={styles.scrollArea}>
          <ScrollView keyboardShouldPersistTaps="handled">
            <View style={styles.switchRow}>
              <Text style={styles.label}>期限超過のみ</Text>
              <Switch
                value={filter.overdueOnly}
                onValueChange={(overdueOnly) => onChange({ ...filter, overdueOnly })}
                accessibilityLabel="期限超過のみ"
              />
            </View>
            {filter.rules.length > 1 ? (
              <View style={styles.chipRow}>
                <Text style={styles.label}>複数条件:</Text>
                {(["and", "or"] as const).map((logic) => (
                  <Chip
                    key={logic}
                    compact
                    selected={filter.logic === logic}
                    onPress={() => onChange({ ...filter, logic })}
                  >
                    {logic === "and" ? "すべて満たす" : "いずれか"}
                  </Chip>
                ))}
              </View>
            ) : null}
            {filter.rules.map((rule) => {
              const needsValue = taskDateOpNeedsValue(rule.op);
              const isRelative = rule.value in TASK_RELATIVE_DATE_LABELS;
              return (
                <View key={rule.id} style={styles.rule} testID={`date-rule-${rule.id}`}>
                  <View style={styles.chipRow}>
                    {FIELDS.map((field) => (
                      <Chip
                        key={field}
                        compact
                        selected={rule.field === field}
                        onPress={() => updateRule(rule.id, { field })}
                      >
                        {TASK_DATE_FIELD_LABELS[field]}
                      </Chip>
                    ))}
                    <IconButton
                      icon="delete-outline"
                      size={20}
                      style={styles.removeButton}
                      accessibilityLabel="この条件を削除"
                      onPress={() =>
                        onChange({ ...filter, rules: filter.rules.filter((r) => r.id !== rule.id) })
                      }
                    />
                  </View>
                  <View style={styles.chipRow}>
                    {TASK_DATE_OPS.map((op: TaskDateOp) => (
                      <Chip
                        key={op}
                        compact
                        selected={rule.op === op}
                        onPress={() => updateRule(rule.id, { op })}
                      >
                        {TASK_DATE_OP_LABELS[op]}
                      </Chip>
                    ))}
                  </View>
                  {needsValue ? (
                    <View style={styles.chipRow}>
                      {RELATIVE.map((value) => (
                        <Chip
                          key={value}
                          compact
                          selected={rule.value === value}
                          onPress={() => updateRule(rule.id, { value })}
                        >
                          {TASK_RELATIVE_DATE_LABELS[value]}
                        </Chip>
                      ))}
                      <TaskDateField
                        label="日付指定"
                        compact
                        allDay
                        style={styles.dateField}
                        value={!isRelative && resolveTaskDateValue(rule.value) ? rule.value : ""}
                        onChange={(value) => updateRule(rule.id, { value: value || "today" })}
                      />
                    </View>
                  ) : null}
                </View>
              );
            })}
            <Button
              icon="plus"
              mode="text"
              onPress={() => onChange({ ...filter, rules: [...filter.rules, newRule()] })}
              accessibilityLabel="日付条件を追加"
            >
              条件を追加
            </Button>
          </ScrollView>
        </Dialog.ScrollArea>
        <Dialog.Actions>
          <Button
            onPress={() => onChange(EMPTY_TASK_DATE_FILTER)}
            disabled={!filter.overdueOnly && filter.rules.length === 0}
          >
            クリア
          </Button>
          <Button onPress={onDismiss}>閉じる</Button>
        </Dialog.Actions>
      </Dialog>
    </Portal>
  );
}

const styles = StyleSheet.create({
  dialog: { maxHeight: "88%" },
  scrollArea: { paddingHorizontal: 16 },
  switchRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: 4 },
  label: { color: "#cdd6f4" },
  chipRow: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: 6, paddingVertical: 4 },
  rule: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: "#45475a", paddingVertical: 6 },
  removeButton: { margin: 0, marginLeft: "auto" },
  // 折り返し行内では幅が決まらず、compact表示の値(flex:1)が0幅に潰れるため最小幅を確保する。
  dateField: { minWidth: 150 },
});
