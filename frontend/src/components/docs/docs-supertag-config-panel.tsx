"use client";

import {
  Input,
} from "@/components/ui/input";
import {
  docsFieldType,
  fieldOptions,
  tagColorStyle,
} from "./docs-utils";
import {
  readConfigRecord,
  tagIdsFromRelatedConfig,
} from "./docs-workspace-shared";
import type {
  DocsField,
  DocsSupertag,
} from "./types";
import {
  FieldCreator,
} from "./docs-field-creator";
import {
  TemplateOutlineEditor,
} from "./docs-template-outline-editor";

// Supertag の設定パネル（色・説明・テンプレート・フィールド・完了状態マッピング・詳細設定）。
export function SupertagConfigPanel({
  tag,
  tags,
  fields,
  onCreateField,
  onUpdateSupertag,
  onUpdateField,
}: {
  tag: DocsSupertag;
  tags: DocsSupertag[];
  fields: DocsField[];
  onCreateField: (tagId: string, name: string, fieldType: string) => void;
  onUpdateSupertag: (tagId: string, patch: Partial<Pick<DocsSupertag, "name" | "description" | "color" | "icon" | "template_json" | "config_json" | "title_template" | "ai_instructions" | "parent_supertag_id">>) => void;
  onUpdateField: (fieldId: string, patch: Partial<Pick<DocsField, "name" | "field_type" | "required" | "options_json" | "sort_order">> & { default_value_json?: unknown }) => void;
}) {
  const config = readConfigRecord(tag.config_json);
  const optionsFields = fields.filter((field) => docsFieldType(field) === "options");
  const doneMapping = readConfigRecord(config.done_state_mapping ?? config.doneStateMapping);
  const selectedDoneField = fields.find((field) => field.id === doneMapping.field_id) ?? optionsFields[0] ?? null;
  const doneValue = typeof doneMapping.done_value === "string"
    ? doneMapping.done_value
    : typeof doneMapping.checked_value === "string"
      ? doneMapping.checked_value
      : "";
  const relatedTagId = tagIdsFromRelatedConfig(tag)[0] ?? "";

  const updateConfig = (patch: Record<string, unknown>) => {
    onUpdateSupertag(tag.id, { config_json: { ...config, ...patch } });
  };

  return (
    <div className="space-y-4 text-sm">
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <span className="rounded border px-2 py-1 text-xs" style={tagColorStyle(tag.color)}>#{tag.name}</span>
          <Input defaultValue={tag.color ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { color: event.target.value })} className="h-8" placeholder="Color" />
        </div>
        <Input defaultValue={tag.description ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { description: event.target.value })} className="h-8" placeholder="Description" />
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium text-muted-foreground">Content template</div>
        <TemplateOutlineEditor
          value={tag.template_json ?? {}}
          onSave={(templateJson) => onUpdateSupertag(tag.id, { template_json: templateJson })}
        />
      </section>

      <section className="space-y-2">
        <div className="text-xs font-medium text-muted-foreground">Fields</div>
        <div className="space-y-2">
          {fields.map((field) => (
            <div key={field.id} className="grid grid-cols-[minmax(0,1fr)_92px_auto] items-center gap-1 rounded border px-2 py-1">
              <Input defaultValue={field.name} onBlur={(event) => onUpdateField(field.id, { name: event.target.value })} className="h-8 rounded border bg-background px-2" />
              <select
                defaultValue={docsFieldType(field)}
                onChange={(event) => onUpdateField(field.id, { field_type: event.target.value })}
                className="h-7 rounded border bg-background px-1 text-xs"
              >
                {["text", "long_text", "options", "date", "checkbox", "reference", "number", "url", "email", "user"].map((type) => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </select>
              <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
                <input type="checkbox" defaultChecked={field.required} onChange={(event) => onUpdateField(field.id, { required: event.target.checked })} />
                req
              </label>
              {docsFieldType(field) === "options" ? (
                <input
                  defaultValue={fieldOptions(field).join(", ")}
                  onBlur={(event) => onUpdateField(field.id, { options_json: { values: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) } })}
                  className="col-span-3 h-8 rounded border bg-background px-2 text-xs outline-none focus:ring-2 focus:ring-ring"
                  placeholder="options"
                />
              ) : null}
            </div>
          ))}
        </div>
        <FieldCreator tagId={tag.id} onCreateField={onCreateField} />
      </section>

      <section className="space-y-2 border-t pt-3">
        <label className="flex items-center justify-between gap-2 text-xs">
          <span>Show checkbox</span>
          <input type="checkbox" defaultChecked={config.show_checkbox === true} onChange={(event) => updateConfig({ show_checkbox: event.target.checked })} />
        </label>
        <div className="grid grid-cols-[1fr_1fr] gap-2">
          <select
            value={selectedDoneField?.id ?? ""}
            onChange={(event) => updateConfig({ done_state_mapping: { ...doneMapping, field_id: event.target.value } })}
            className="h-8 rounded border bg-background px-2 text-xs"
          >
            <option value="">Done field</option>
            {optionsFields.map((field) => <option key={field.id} value={field.id}>{field.name}</option>)}
          </select>
          <select
            value={doneValue}
            onChange={(event) => updateConfig({ done_state_mapping: { ...doneMapping, done_value: event.target.value, field_id: selectedDoneField?.id ?? "" } })}
            className="h-8 rounded border bg-background px-2 text-xs"
          >
            <option value="">Checked value</option>
            {(selectedDoneField ? fieldOptions(selectedDoneField) : []).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </div>
      </section>

      <section className="space-y-2 border-t pt-3">
        <div className="text-xs font-medium text-muted-foreground">Advanced options</div>
        <Input defaultValue={tag.title_template ?? ""} onBlur={(event) => onUpdateSupertag(tag.id, { title_template: event.target.value })} className="h-8" placeholder="Title expression" />
        <select
          value={typeof config.default_child_supertag_id === "string" ? config.default_child_supertag_id : ""}
          onChange={(event) => updateConfig({ default_child_supertag_id: event.target.value || null })}
          className="h-8 w-full rounded border bg-background px-2 text-xs"
        >
          <option value="">Default child supertag</option>
          {tags.map((item) => <option key={item.id} value={item.id}>#{item.name}</option>)}
        </select>
        <select
          value={relatedTagId}
          onChange={(event) => updateConfig({
            related_content: event.target.value
              ? { query: { and: [{ tag: event.target.value, include_descendants: true }] } }
              : null,
          })}
          className="h-8 w-full rounded border bg-background px-2 text-xs"
        >
          <option value="">Related content tag</option>
          {tags.map((item) => <option key={item.id} value={item.id}>#{item.name}</option>)}
        </select>
      </section>
    </div>
  );
}
