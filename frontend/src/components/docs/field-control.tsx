"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
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
  onChange: (value: string) => void;
  onCommit?: (value: string) => void;
  onNavigatePrevious?: () => void;
  onNavigateNext?: () => void;
  onEscape?: () => void;
};

export function FieldControl({
  field,
  value,
  nodes,
  projects,
  currentNodeId,
  onChange,
  onCommit,
  onNavigatePrevious,
  onNavigateNext,
  onEscape,
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

  const handleSingleLineKeyDown = (event: KeyboardEvent<HTMLInputElement | HTMLSelectElement>) => {
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

  const handleSelectKeyDown = (event: KeyboardEvent<HTMLSelectElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      commitDraft(event.currentTarget.value);
      onEscape?.();
      return;
    }
    // 通常の上下キーはoption選択に残す。Field間移動はCtrl/Cmd+上下で行う。
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (!(event.ctrlKey || event.metaKey)) {
      event.stopPropagation();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    navigate(event.key === "ArrowUp" ? "previous" : "next", event.currentTarget.value);
  };

  if (type === "checkbox") {
    return (
      <label className="flex h-8 items-center gap-2 rounded border bg-background px-2 text-xs">
        <input
          data-docs-field-control
          type="checkbox"
          aria-label={label}
          checked={draft === "true"}
          onChange={(event) => {
            const next = event.target.checked ? "true" : "false";
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
    return (
      <select
        data-docs-field-control
        aria-label={label}
        value={draft}
        onChange={(event) => {
          updateDraft(event.target.value);
          commitDraft(event.target.value);
        }}
        onKeyDown={handleSelectKeyDown}
        className={inputClass}
      >
        <option value="">None</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
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
    const selectedProject = isProjectReference
      ? projects.find((project) => project.id === draft)
      : null;
    return (
      <div className="space-y-1">
        {isProjectReference && draft ? (
          <span className="inline-flex max-w-full items-center rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
            <span className="truncate">{selectedProject?.name ?? "案件を選択してください"}</span>
          </span>
        ) : null}
        <select
          data-docs-field-control
          aria-label={label}
          value={options.some((option) => option.value === draft) ? draft : ""}
          onChange={(event) => {
            updateDraft(event.target.value);
            commitDraft(event.target.value);
          }}
          onKeyDown={handleSelectKeyDown}
          className={inputClass}
        >
          <option value="">None</option>
          {options.map((option) => (
            <option key={option.id} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
    );
  }

  if (type === "long_text") {
    return (
      <Textarea
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
        rows={1}
        wrap="off"
        className="h-7 min-h-7 max-h-7 resize-none overflow-x-auto overflow-y-hidden whitespace-nowrap border-0 bg-transparent px-1 py-1 text-sm leading-5 shadow-none focus:h-7 focus:min-h-7 focus:max-h-7 focus:resize-none focus:overflow-x-auto focus:overflow-y-hidden focus:whitespace-nowrap focus-visible:ring-1"
      />
    );
  }

  return (
    <Input
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
      className="h-7 border-0 bg-transparent px-1 shadow-none focus-visible:ring-1"
      placeholder={type === "options_from_supertag" ? "comma separated" : undefined}
    />
  );
}
