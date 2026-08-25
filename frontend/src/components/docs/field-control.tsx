"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { DocsField, DocsNode, DocsProject } from "./types";
import { docsFieldType, fieldOptions, referenceOptions } from "./docs-utils";

type FieldControlProps = {
  field: DocsField;
  value: string;
  nodes: DocsNode[];
  projects: DocsProject[];
  currentNodeId?: string;
  controlId?: string;
  longTextLayout?: "compact" | "document";
  onChange: (value: string) => void;
  onCommit?: (value: string) => void;
  onNavigatePrevious?: () => void;
  onNavigateNext?: () => void;
  onEscape?: () => void;
  disabled?: boolean;
};

export function FieldControl({
  field,
  value,
  nodes,
  projects,
  currentNodeId,
  controlId,
  longTextLayout = "compact",
  onChange,
  onCommit,
  onNavigatePrevious,
  onNavigateNext,
  onEscape,
  disabled = false,
}: FieldControlProps) {
  const type = docsFieldType(field);
  const options = fieldOptions(field);
  const inputClass = "h-7 w-full border-0 bg-transparent px-1 text-sm outline-none focus:ring-1 focus:ring-ring/50";
  const label = `Field ${field.name}`;
  const isProjectReference =
    field.field_type === "project_ref" ||
    field.name.toLowerCase() === "project" ||
    field.system_key === "project" ||
    field.system_key?.endsWith("_project") === true;
  const valueKey = `${field.id}:${value}`;
  const [draftState, setDraftState] = useState({ key: valueKey, draft: value });
  const [selectOpen, setSelectOpen] = useState(false);
  const dirtyValueKeyRef = useRef<string | null>(null);
  const draft = draftState.key === valueKey ? draftState.draft : value;

  useEffect(() => {
    dirtyValueKeyRef.current = null;
  }, [valueKey]);

  const updateDraft = (next: string) => {
    dirtyValueKeyRef.current = valueKey;
    setDraftState({ key: valueKey, draft: next });
    onChange(next);
  };

  const commitDraft = (next = draft) => {
    // 展開・格納や行フォーカスの移動だけではFieldを書き戻さない。
    // 未編集のblurで空値が送られると、PUT APIが既存Fieldを削除してしまうため。
    if (dirtyValueKeyRef.current !== valueKey) return;
    dirtyValueKeyRef.current = null;
    onCommit?.(next);
  };

  const navigate = (direction: "previous" | "next", next = draft) => {
    commitDraft(next);
    if (direction === "previous") onNavigatePrevious?.();
    else onNavigateNext?.();
  };

  const handleSingleLineKeyDown = (event: {
    currentTarget: EventTarget;
    key: string;
    preventDefault: () => void;
    stopPropagation: () => void;
  }) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      commitDraft(event.currentTarget instanceof HTMLInputElement ? event.currentTarget.value : draft);
      onEscape?.();
      return;
    }
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    event.stopPropagation();
    navigate(event.key === "ArrowUp" ? "previous" : "next", event.currentTarget instanceof HTMLInputElement ? event.currentTarget.value : draft);
  };

  const handleSelectKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    orderedValues: string[],
    renderedValue: string,
  ) => {
    if (event.key === "Escape") {
      if (selectOpen) return;
      event.preventDefault();
      event.stopPropagation();
      commitDraft(draft);
      onEscape?.();
      return;
    }
    // 通常の上下キーはoption選択に残す。Field間移動はCtrl/Cmd+上下で行う。
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (!(event.ctrlKey || event.metaKey)) {
      if (!selectOpen) {
        event.preventDefault();
        event.stopPropagation();
        const currentIndex = Math.max(0, orderedValues.indexOf(renderedValue));
        const offset = event.key === "ArrowUp" ? -1 : 1;
        const nextIndex = Math.max(0, Math.min(orderedValues.length - 1, currentIndex + offset));
        const next = orderedValues[nextIndex] ?? renderedValue;
        if (next !== renderedValue) {
          updateDraft(next);
          commitDraft(next);
        }
      }
      event.stopPropagation();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    navigate(event.key === "ArrowUp" ? "previous" : "next", renderedValue);
  };

  if (type === "checkbox") {
    return (
      <label className="flex h-8 items-center gap-2 rounded-md border bg-background px-2 text-xs">
        <Checkbox
          id={controlId}
          data-docs-field-control
          aria-label={label}
          checked={draft === "true"}
          disabled={disabled}
          onCheckedChange={(checked) => {
            const next = checked === true ? "true" : "false";
            updateDraft(next);
            commitDraft(next);
          }}
          onKeyDown={(event) => handleSingleLineKeyDown(event)}
          className="size-4 accent-primary"
        />
        <span className="text-muted-foreground">{draft === "true" ? "true" : "false"}</span>
      </label>
    );
  }

  if ((type === "options" || type === "options_from_supertag") && options.length > 0) {
    const renderedValue = options.includes(draft) ? draft : "";
    const orderedValues = ["", ...options];
    return (
      <AppSelect
        id={controlId}
        data-docs-field-control
        aria-label={label}
        value={renderedValue}
        open={selectOpen}
        onOpenChange={setSelectOpen}
        onChange={(event) => {
          updateDraft(event.target.value);
          commitDraft(event.target.value);
        }}
        onKeyDown={(event) => handleSelectKeyDown(event, orderedValues, renderedValue)}
        className={`${inputClass} rounded-md border border-input bg-background/70 px-2 shadow-none hover:bg-accent/40`}
        size="sm"
        disabled={disabled}
      >
        <option value="">None</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </AppSelect>
    );
  }

  if (type === "reference" || isProjectReference) {
    const options = isProjectReference
      ? projects.map((project) => ({
          id: `project:${project.id}`,
          value: project.id,
          label: project.name,
        }))
      : referenceOptions(nodes, projects, currentNodeId);
    const renderedValue = options.some((option) => option.value === draft) ? draft : "";
    const orderedValues = ["", ...options.map((option) => option.value)];
    return (
      <AppSelect
        id={controlId}
        data-docs-field-control
        aria-label={label}
        value={renderedValue}
        open={selectOpen}
        onOpenChange={setSelectOpen}
        onChange={(event) => {
          updateDraft(event.target.value);
          commitDraft(event.target.value);
        }}
        onKeyDown={(event) => handleSelectKeyDown(event, orderedValues, renderedValue)}
        className={`${inputClass} rounded-md border border-input bg-background/70 px-2 shadow-none hover:bg-accent/40`}
        size="sm"
        disabled={disabled}
      >
        <option value="">None</option>
        {options.map((option) => (
          <option key={option.id} value={option.value}>
            {option.label}
          </option>
        ))}
      </AppSelect>
    );
  }

  if (type === "long_text") {
    return (
      <Textarea
        id={controlId}
        data-docs-field-control
        aria-label={label}
        value={draft}
        onChange={(event) => updateDraft(event.target.value)}
        onBlur={(event) => commitDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            commitDraft(event.currentTarget.value);
            onEscape?.();
            return;
          }
          if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
          if (longTextLayout === "document") {
            if (!event.ctrlKey && !event.metaKey) return;
            event.preventDefault();
            event.stopPropagation();
            navigate(event.key === "ArrowUp" ? "previous" : "next", event.currentTarget.value);
            return;
          }
          const cursor = event.currentTarget.selectionStart;
          const value = event.currentTarget.value;
          const atBoundary = event.key === "ArrowUp"
            ? !value.slice(0, cursor).includes("\n")
            : !value.slice(cursor).includes("\n");
          if (!atBoundary) return;
          event.preventDefault();
          event.stopPropagation();
          navigate(event.key === "ArrowUp" ? "previous" : "next", value);
        }}
        rows={longTextLayout === "document" ? 4 : 1}
        disabled={disabled}
        wrap={longTextLayout === "document" ? "soft" : "off"}
        className={longTextLayout === "document"
          ? "min-h-24 max-h-80 resize-y overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-words [overflow-wrap:anywhere] border-0 bg-transparent px-1 py-1 text-sm leading-5 shadow-none focus-visible:ring-1"
          : "h-7 min-h-7 max-h-7 resize-none overflow-x-auto overflow-y-hidden whitespace-nowrap border-0 bg-transparent px-1 py-1 text-sm leading-5 shadow-none focus:h-7 focus:min-h-7 focus:max-h-7 focus:resize-none focus:overflow-x-auto focus:overflow-y-hidden focus:whitespace-nowrap focus-visible:ring-1"}
      />
    );
  }

  return (
    <Input
      id={controlId}
      data-docs-field-control
      aria-label={label}
      type={
        type === "number"
          ? "number"
          : type === "date"
            ? "date"
            : type === "url"
                ? "url"
                : type === "email"
                  ? "email"
                  : "text"
      }
      value={draft}
      onChange={(event) => updateDraft(event.target.value)}
      onBlur={(event) => commitDraft(event.target.value)}
      onKeyDown={(event) => handleSingleLineKeyDown(event)}
      disabled={disabled}
      className="h-7 border-0 bg-transparent px-1 shadow-none focus-visible:ring-1"
      placeholder={type === "options_from_supertag" ? "comma separated" : undefined}
    />
  );
}
