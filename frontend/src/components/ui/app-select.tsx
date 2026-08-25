"use client";

import * as React from "react";
import type { SelectRootChangeEventDetails } from "@base-ui/react/select";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type SelectOption = {
  value: string;
  label: React.ReactNode;
  disabled: boolean;
};

const EMPTY_VALUE = "__aoitalk_app_select_empty_value__";

export type AppSelectChangeEvent = {
  currentTarget: { value: string };
  target: { value: string };
};

export type AppSelectOpenChangeDetails = SelectRootChangeEventDetails;

type TriggerProps = Omit<
  React.ButtonHTMLAttributes<HTMLButtonElement>,
  "children" | "defaultValue" | "name" | "onChange" | "required" | "type" | "value"
>;

export type AppSelectProps = TriggerProps & {
  autoComplete?: string;
  children?: React.ReactNode;
  contentClassName?: string;
  /** Optional trigger-only content; popup options keep their full labels. */
  triggerContent?: React.ReactNode;
  /**
   * Capture-phase keyboard hook for the Base UI popup. This is intentionally
   * opt-in so existing AppSelect consumers keep the native Select behavior.
   */
  onContentKeyDownCapture?: React.KeyboardEventHandler<HTMLDivElement>;
  defaultValue?: number | string | null;
  alignItemWithTrigger?: boolean;
  name?: string;
  onChange?: (event: AppSelectChangeEvent) => void;
  onOpenChange?: (
    open: boolean,
    details?: AppSelectOpenChangeDetails,
  ) => void;
  onValueChange?: (value: string) => void;
  open?: boolean;
  placeholder?: React.ReactNode;
  positionerClassName?: string;
  readOnly?: boolean;
  required?: boolean;
  showSelectedIndicator?: boolean;
  size?: "default" | "sm";
  value?: number | string | null;
  /** Optional portal container. Defaults to document.body. */
  container?: React.RefObject<HTMLElement | null> | HTMLElement | null;
};

function optionValue(value: unknown, label: React.ReactNode) {
  if (value !== undefined && value !== null) return String(value);
  if (typeof label === "string" || typeof label === "number") return String(label);
  return "";
}

function collectOptions(children: React.ReactNode): SelectOption[] {
  const options: SelectOption[] = [];

  const visit = (nodes: React.ReactNode) => {
    React.Children.forEach(nodes, (child) => {
      if (!React.isValidElement(child)) return;

      if (child.type === React.Fragment || child.type === "optgroup") {
        visit((child.props as { children?: React.ReactNode }).children);
        return;
      }

      if (child.type !== "option") return;
      const props = child.props as React.OptionHTMLAttributes<HTMLOptionElement>;
      options.push({
        value: optionValue(props.value, props.children),
        label: props.children,
        disabled: props.disabled === true,
      });
    });
  };

  visit(children);
  return options;
}

function internalValue(value: string | null | undefined) {
  return value === "" ? EMPTY_VALUE : value;
}

function externalValue(value: string | null) {
  return value === EMPTY_VALUE || value === null ? "" : value;
}

/**
 * アプリ全体で使う、HTML selectに近い移行用APIのカスタムSelect。
 *
 * popupはBase UIで描画するためOS依存の見た目にならない。既存画面を安全に
 * 移行できるよう、`option` childrenと`event.target.value`形式のonChangeを
 * 受け付ける。新規コードではonValueChangeを優先する。
 */
export function AppSelect({
  autoComplete,
  children,
  contentClassName,
  triggerContent,
  onContentKeyDownCapture,
  defaultValue,
  alignItemWithTrigger,
  disabled,
  id,
  name,
  onChange,
  onOpenChange,
  onValueChange,
  open,
  placeholder,
  positionerClassName,
  readOnly,
  required,
  showSelectedIndicator = true,
  size = "default",
  value,
  container,
  ...triggerProps
}: AppSelectProps) {
  const options = React.useMemo(() => collectOptions(children), [children]);
  const items = React.useMemo(
    () =>
      options.map((option) => ({
        label: option.label,
        value: internalValue(option.value) as string,
      })),
    [options],
  );
  const controlledValue = internalValue(
    value === undefined || value === null ? value : String(value),
  );
  const initialValue = internalValue(
    defaultValue === undefined || defaultValue === null ? defaultValue : String(defaultValue),
  );

  const handleValueChange = (nextValue: string | null) => {
    const next = externalValue(nextValue);
    onValueChange?.(next);
    onChange?.({
      currentTarget: { value: next },
      target: { value: next },
    });
  };

  return (
    <Select
      autoComplete={autoComplete}
      defaultValue={initialValue}
      disabled={disabled}
      items={items}
      itemToStringValue={(itemValue) => externalValue(itemValue)}
      name={name}
      onOpenChange={(nextOpen, details) => onOpenChange?.(nextOpen, details)}
      onValueChange={handleValueChange}
      open={open}
      readOnly={readOnly}
      required={required}
      value={controlledValue}
    >
      <SelectTrigger id={id} size={size} {...triggerProps}>
        {triggerContent !== undefined ? (
          <SelectValue>{triggerContent}</SelectValue>
        ) : (
          <SelectValue placeholder={placeholder} />
        )}
      </SelectTrigger>
      <SelectContent
        alignItemWithTrigger={alignItemWithTrigger}
        className={contentClassName}
        onKeyDownCapture={onContentKeyDownCapture}
        positionerClassName={positionerClassName}
        container={container}
      >
        {options.map((option, index) => (
          <SelectItem
            key={`${option.value}:${index}`}
            value={internalValue(option.value)}
            disabled={option.disabled}
            showIndicator={showSelectedIndicator}
          >
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
