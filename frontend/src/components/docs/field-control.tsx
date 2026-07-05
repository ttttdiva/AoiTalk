"use client";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { DocsField, DocsNode, DocsProject } from "./types";
import { docsFieldType, fieldOptions } from "./docs-utils";

type FieldControlProps = {
  field: DocsField;
  value: string;
  nodes: DocsNode[];
  projects: DocsProject[];
  currentNodeId?: string;
  onChange: (value: string) => void;
  onCommit?: (value: string) => void;
};

export function FieldControl({
  field,
  value,
  nodes,
  projects,
  currentNodeId,
  onChange,
  onCommit,
}: FieldControlProps) {
  const type = docsFieldType(field);
  const options = fieldOptions(field);
  const inputClass = "h-7 w-full border-0 bg-transparent px-0 text-sm outline-none focus:ring-0";
  const label = `Field ${field.name}`;

  if (type === "checkbox") {
    return (
      <label className="flex h-7 items-center gap-2 text-xs">
        <input
          type="checkbox"
          aria-label={label}
          checked={value === "true"}
          onChange={(event) => {
            const next = event.target.checked ? "true" : "false";
            onChange(next);
            onCommit?.(next);
          }}
          className="size-4 accent-primary"
        />
        <span className="text-muted-foreground">{value === "true" ? "true" : "false"}</span>
      </label>
    );
  }

  if ((type === "options" || type === "options_from_supertag") && options.length > 0) {
    return (
      <select
        aria-label={label}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          onCommit?.(event.target.value);
        }}
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

  if (type === "reference") {
    return (
      <select
        aria-label={label}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          onCommit?.(event.target.value);
        }}
        className={inputClass}
      >
        <option value="">None</option>
        {nodes
          .filter((node) => node.id !== currentNodeId)
          .slice(0, 300)
          .map((node) => (
            <option key={node.id} value={node.id}>
              {node.title || node.body_text.slice(0, 60)}
            </option>
          ))}
      </select>
    );
  }

  if (type === "long_text") {
    return (
      <Textarea
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={(event) => onCommit?.(event.target.value)}
        className="min-h-16 border-0 bg-transparent px-0 text-xs shadow-none focus-visible:ring-0"
      />
    );
  }

  return (
    <Input
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
      value={value}
      onChange={(event) => onChange(event.target.value)}
      onBlur={(event) => onCommit?.(event.target.value)}
      className="h-7 border-0 bg-transparent px-0 shadow-none focus-visible:ring-0"
      placeholder={type === "options_from_supertag" ? "comma separated" : undefined}
    />
  );
}
