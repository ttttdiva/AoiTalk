export type TemplateOutlineRow = {
  id: string;
  text: string;
  depth: number;
};

let rowCounter = 0;

function nextRowId() {
  rowCounter += 1;
  return `template-row-${rowCounter}`;
}

export function templateRowsFromJson(value: unknown): TemplateOutlineRow[] {
  const record = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const blocks = Array.isArray(record.blocks) ? record.blocks : [];
  return blocks
    .map((block) => {
      if (!block || typeof block !== "object") return null;
      const row = block as Record<string, unknown>;
      const text = typeof row.text === "string" ? row.text : "";
      const depthValue = typeof row.depth === "number" ? row.depth : typeof row.indent === "number" ? row.indent : 0;
      return { id: nextRowId(), text, depth: Math.max(0, Math.min(8, Math.trunc(depthValue))) };
    })
    .filter((row): row is TemplateOutlineRow => Boolean(row));
}

export function templateJsonFromRows(rows: TemplateOutlineRow[]) {
  return {
    format: "doc_block_template",
    blocks: rows
      .map((row) => ({ type: "paragraph", text: row.text.trim(), depth: Math.max(0, Math.min(8, Math.trunc(row.depth))) }))
      .filter((row) => row.text.length > 0),
  };
}

export function createTemplateOutlineRow(text = "", depth = 0): TemplateOutlineRow {
  return { id: nextRowId(), text, depth: Math.max(0, Math.min(8, Math.trunc(depth))) };
}
