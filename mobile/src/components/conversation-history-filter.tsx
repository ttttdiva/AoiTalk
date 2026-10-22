import React, { useMemo, useState } from "react";
import { IconButton, Menu } from "react-native-paper";
import {
  CHAT_HISTORY_FILTER_OPTIONS,
  chatHistoryFilterLabel,
  type ChatHistoryFilter,
} from "../lib/chat-session-view";

type ConversationHistoryFilterProps = {
  value: ChatHistoryFilter;
  onChange: (value: ChatHistoryFilter) => void;
  disabled?: boolean;
};

/**
 * One compact single-select popup shared by the drawer and Chat tab history.
 * The leading icon is stable per option; only the selected row gets a check.
 */
export function ConversationHistoryFilter({
  value,
  onChange,
  disabled = false,
}: ConversationHistoryFilterProps) {
  const [visible, setVisible] = useState(false);
  const selectedLabel = useMemo(() => chatHistoryFilterLabel(value), [value]);

  return (
    <Menu
      visible={visible}
      onDismiss={() => setVisible(false)}
      anchor={(
        <IconButton
          icon="filter-variant"
          iconColor="#cdd6f4"
          size={20}
          style={{ width: 44, height: 44, minWidth: 44, minHeight: 44, margin: 0 }}
          disabled={disabled}
          accessibilityLabel={`履歴フィルター（${selectedLabel}）`}
          onPress={() => setVisible(true)}
        />
      )}
    >
      {CHAT_HISTORY_FILTER_OPTIONS.map((option) => {
        const selected = option.value === value;
        return (
          <Menu.Item
            key={option.value}
            title={option.label}
            leadingIcon={option.icon}
            trailingIcon={selected ? "check" : undefined}
            accessibilityLabel={`履歴フィルター: ${option.label}`}
            accessibilityState={{ selected }}
            onPress={() => {
              setVisible(false);
              onChange(option.value);
            }}
          />
        );
      })}
    </Menu>
  );
}
