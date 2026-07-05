"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { DocsWorkspace } from "@/components/docs/docs-workspace";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  BookOpen,
  Database,
  FileText,
  HelpCircle,
  Loader2,
  RefreshCw,
  Table2,
} from "lucide-react";

type ProjectInfo = {
  id: string;
  name: string;
  description?: string | null;
};

type ProjectInformationNode = {
  id: string;
  title: string;
  body_text: string;
  body_json?: Record<string, unknown>;
  updated_at?: string | null;
};

type ProjectQaEntry = {
  id: string;
  question: string;
  answer: string | null;
  status: string;
  review_state: string;
};

type RecordTableSummary = {
  id: string;
  name: string;
  description: string | null;
  updatedAt?: string | null;
  updated_at?: string | null;
};

type ProjectInformationResponse = {
  project: {
    id: string;
    name: string;
    description: string | null;
    knowledge_node_id: string;
  };
  node: ProjectInformationNode;
  qa_entries: ProjectQaEntry[];
  management_documents: Array<Record<string, unknown>>;
  record_tables: RecordTableSummary[];
};

function ProjectInformationLanding({
  data,
}: {
  data: ProjectInformationResponse;
}) {
  const summaryItems = [
    {
      label: "Docs page",
      value: data.node.title || data.project.name,
      icon: FileText,
    },
    {
      label: "Q&A",
      value: `${data.qa_entries.length}`,
      icon: HelpCircle,
    },
    {
      label: "Tables",
      value: `${data.record_tables.length}`,
      icon: Table2,
    },
    {
      label: "Management docs",
      value: `${data.management_documents.length}`,
      icon: Database,
    },
    {
      label: "Updated",
      value: formatDate(data.node.updated_at) || "Not saved",
      icon: RefreshCw,
    },
  ];

  return (
    <section className="border-b pb-4">
      <div className="max-w-5xl">
        <div className="text-xs font-semibold uppercase text-primary">Project information</div>
        <h2 className="mt-2 text-3xl font-semibold tracking-normal">{data.project.name}</h2>
        {data.project.description && (
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            {data.project.description}
          </p>
        )}
      </div>
      <div className="mt-4 grid overflow-hidden rounded-md border bg-muted/20 sm:grid-cols-2 xl:grid-cols-5">
        {summaryItems.map((item) => {
          const Icon = item.icon;
          return (
            <div key={item.label} className="min-w-0 border-b px-3 py-3 last:border-b-0 sm:border-r sm:last:border-r-0 xl:border-b-0">
              <div className="flex items-center gap-2 text-xs font-medium uppercase text-muted-foreground">
                <Icon className="size-3.5" />
                {item.label}
              </div>
              <div className="mt-1 truncate text-sm font-medium">{item.value}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function formatDate(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

export function ProjectInformationPanel({ project }: { project: ProjectInfo }) {
  const [data, setData] = useState<ProjectInformationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await apiFetch<ProjectInformationResponse>(
        `/api/projects/${project.id}/information`,
      );
      setData(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "案件情報の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [project.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const recordTables = useMemo(() => data?.record_tables ?? [], [data?.record_tables]);
  const selectedTable = useMemo(
    () => recordTables.find((table) => table.id === selectedTableId) ?? null,
    [recordTables, selectedTableId],
  );

  if (loading && !data) {
    return (
      <div className="flex min-h-[360px] items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        案件情報を読み込み中...
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="rounded-md border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
        {error}
        <div className="mt-3">
          <Button variant="outline" size="sm" onClick={() => void load()}>
            <RefreshCw className="mr-2 size-4" />
            再読み込み
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <BookOpen className="size-4 text-primary" />
            <h3 className="truncate text-sm font-semibold">案件情報</h3>
            <Badge variant="secondary">Docs正本</Badge>
          </div>
          <p className="mt-1 truncate text-xs text-muted-foreground">
            {data?.node.title || `${project.name} 案件情報`}
            {data?.node.updated_at ? ` / 更新 ${formatDate(data.node.updated_at)}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            <RefreshCw className="mr-2 size-4" />
            更新
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {data && <ProjectInformationLanding data={data} />}

      {data?.node.id && (
        <section className="min-h-[680px] overflow-hidden rounded-md border bg-background">
          <DocsWorkspace initialNodeId={data.node.id} />
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
          <section className="rounded-md border bg-background">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <div className="text-sm font-medium">Q&A</div>
              <Badge variant="secondary">{data?.qa_entries.length ?? 0}</Badge>
            </div>
            <div className="max-h-72 overflow-auto p-3">
              {(data?.qa_entries ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">Q&Aはまだありません。</p>
              ) : (
                <div className="space-y-3">
                  {data?.qa_entries.map((entry) => (
                    <div key={entry.id} className="rounded-md border p-3">
                      <div className="text-sm font-medium">{entry.question}</div>
                      {entry.answer && (
                        <p className="mt-2 text-sm text-muted-foreground">{entry.answer}</p>
                      )}
                      <div className="mt-2 flex gap-2">
                        <Badge variant="outline">{entry.status}</Badge>
                        <Badge variant="outline">{entry.review_state}</Badge>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section className="rounded-md border bg-background">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Database className="size-4" />
                台帳
              </div>
              <Badge variant="secondary">{recordTables.length}</Badge>
            </div>
            <div className="max-h-80 overflow-auto p-3">
              {recordTables.length === 0 ? (
                <p className="text-sm text-muted-foreground">台帳はまだありません。</p>
              ) : (
                <div className="space-y-2">
                  {recordTables.map((table) => (
                    <button
                      key={table.id}
                      type="button"
                      onClick={() => setSelectedTableId(table.id)}
                      className="w-full rounded-md border p-3 text-left hover:bg-muted"
                    >
                      <div className="flex items-center gap-2 text-sm font-medium">
                        <Table2 className="size-4" />
                        {table.name}.dbtable
                      </div>
                      {table.description && (
                        <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                          {table.description}
                        </p>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </section>
      </div>

      {selectedTable && (
        <div className="rounded-md border bg-background p-3">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h4 className="text-sm font-semibold">{selectedTable.name}.dbtable</h4>
              {selectedTable.description && (
                <p className="text-xs text-muted-foreground">{selectedTable.description}</p>
              )}
            </div>
            <Button variant="outline" size="sm" onClick={() => setSelectedTableId(null)}>
              閉じる
            </Button>
          </div>
          <RecordTableEditor
            projectId={project.id}
            tableId={selectedTable.id}
            onClose={() => setSelectedTableId(null)}
            onChanged={load}
          />
        </div>
      )}
    </div>
  );
}
