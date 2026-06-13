"use client";

import type React from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RecordTableEditor } from "@/components/records/record-table-editor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Archive,
  BookOpen,
  Check,
  Database,
  FileText,
  FolderSearch,
  Info,
  Link2,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Table2,
} from "lucide-react";

type CategoryStatus = "active" | "suggested" | "hidden" | "archived";
type ItemStatus = "active" | "suggested" | "archived";
type TargetKind = "file" | "record_table" | "url";
type DialogKind = "category" | "document" | "fact" | null;
type Selection =
  | { kind: "fact"; id: string }
  | { kind: "document"; id: string }
  | { kind: "table"; id: string }
  | null;

type ProjectInfoCategory = {
  id: string;
  key: string;
  label: string;
  description: string | null;
  status: CategoryStatus;
  source: string;
  sortOrder?: number | null;
  sort_order?: number | null;
};

type ProjectDocument = {
  id: string;
  categoryId?: string | null;
  category_id?: string | null;
  title: string;
  description: string | null;
  documentType?: string | null;
  document_type?: string | null;
  targetKind?: TargetKind | null;
  target_kind?: TargetKind | null;
  filePath?: string | null;
  file_path?: string | null;
  recordTableId?: string | null;
  record_table_id?: string | null;
  externalUrl?: string | null;
  external_url?: string | null;
  role: string | null;
  isPrimary?: boolean | null;
  is_primary?: boolean | null;
  aiAccessLevel?: string | null;
  ai_access_level?: string | null;
  status: ItemStatus;
  notes?: string | null;
  sourceType?: string | null;
  source_type?: string | null;
  sourceRef?: string | null;
  source_ref?: string | null;
  synthetic?: boolean;
};

type ProjectFact = {
  id: string;
  categoryId?: string | null;
  category_id?: string | null;
  title: string;
  content: string;
  factType?: string | null;
  fact_type?: string | null;
  importance: number | null;
  status: ItemStatus;
  sourceRef?: string | null;
  source_ref?: string | null;
};

type RecordTableSummary = {
  id: string;
  name: string;
  description: string | null;
  row_count?: number;
};

type OrganizerDocument = {
  title: string;
  file_path: string;
  document_type: string;
  category_key: string;
  role: string;
  is_primary: boolean;
  ai_access_level: string;
  description: string;
  notes: string;
};

type OrganizerFact = {
  title: string;
  content: string;
  category_key: string;
  fact_type: string;
  importance: number;
  source_ref: string;
};

type OrganizerResponse = {
  success: boolean;
  applied: boolean;
  source_folder: string;
  scanned: {
    count: number;
    max_files: number;
    files: Array<{
      path: string;
      name: string;
      extension: string;
      size_bytes: number;
      extract_error?: string | null;
    }>;
  };
  draft: {
    generated_by: string;
    summary_md: string;
    documents: OrganizerDocument[];
    facts: OrganizerFact[];
    decisions: string[];
    open_questions: string[];
  };
  result: {
    documents: number;
    facts: number;
  };
};

type InformationResponse = {
  categories: ProjectInfoCategory[];
  documents: ProjectDocument[];
  management_documents: ProjectDocument[];
  facts: ProjectFact[];
  record_tables: RecordTableSummary[];
};

type ProjectInfo = {
  id: string;
  name: string;
};

type FactForm = {
  title: string;
  categoryId: string;
  factType: string;
  content: string;
  sourceRef: string;
  importance: string;
};

type DocumentForm = {
  title: string;
  categoryId: string;
  documentType: string;
  targetKind: TargetKind;
  filePath: string;
  recordTableId: string;
  externalUrl: string;
  role: string;
  aiAccessLevel: string;
  description: string;
  notes: string;
};

type ListItem = {
  kind: "fact" | "document" | "table";
  id: string;
  title: string;
  body: string;
  type: string;
  role?: string | null;
  categoryId: string | null;
  source: string | null;
  importance?: number | null;
  synthetic?: boolean;
};

const EMPTY_FACT_FORM: FactForm = {
  title: "",
  categoryId: "",
  factType: "fact",
  content: "",
  sourceRef: "",
  importance: "5",
};

const EMPTY_DOCUMENT_FORM: DocumentForm = {
  title: "",
  categoryId: "",
  documentType: "document",
  targetKind: "file",
  filePath: "",
  recordTableId: "",
  externalUrl: "",
  role: "reference",
  aiAccessLevel: "metadata",
  description: "",
  notes: "",
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

function categoryIdOf(item: { categoryId?: string | null; category_id?: string | null }) {
  return item.categoryId ?? item.category_id ?? null;
}

function field<T>(camel: T | undefined | null, snake: T | undefined | null): T | null {
  return camel ?? snake ?? null;
}

function targetLabel(kind: string | null) {
  if (kind === "record_table") return "台帳";
  if (kind === "url") return "URL";
  return "ファイル";
}

function roleLabel(role: string | null) {
  const labels: Record<string, string> = {
    primary: "正本",
    reference: "参照",
    management: "管理資料",
    draft: "下書き",
  };
  return labels[role || ""] || role || "参照";
}

function organizerCategoryLabel(key: string) {
  const labels: Record<string, string> = {
    overview: "概要",
    stakeholders: "関係者",
    scope: "スコープ",
    requirements: "要件",
    deliverables: "成果物",
    important_documents: "重要資料",
    decisions: "決定事項",
    open_questions: "要確認",
    risks: "リスク",
    issues: "課題",
    milestones: "工程",
    architecture: "構成",
    detail_design: "詳細設計",
    verification: "検証",
  };
  return labels[key] || key;
}

function factTypeLabel(value: string | null) {
  const labels: Record<string, string> = {
    fact: "情報",
    decision: "決定",
    open_question: "要確認",
    design: "設計",
    verification: "検証",
    requirement: "要件",
    risk: "リスク",
    issue: "課題",
    milestone: "工程",
    document_summary: "資料要約",
    file_inventory: "資料一覧",
  };
  return labels[value || ""] || value || "情報";
}

function splitSourceRef(value: string | null) {
  return (value || "")
    .split(/[\n,、]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 8);
}

function isTableLikeContent(value: string) {
  const lines = value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines.some((line) => line.startsWith("|") && line.endsWith("|"));
}

function isGeneratedDocumentSummary(value: string | null | undefined) {
  const normalized = (value || "").trim();
  if (!normalized) return false;
  return (
    isTableLikeContent(normalized) ||
    normalized.includes(" / |") ||
    normalized.startsWith("#") ||
    normalized.startsWith("## ") ||
    normalized.length > 260
  );
}

function documentDisplayDescription(doc: ProjectDocument) {
  const description = doc.description || "";
  if (description && !isGeneratedDocumentSummary(description)) {
    return description;
  }
  const filePath = field(doc.filePath, doc.file_path);
  const externalUrl = field(doc.externalUrl, doc.external_url);
  const recordTableId = field(doc.recordTableId, doc.record_table_id);
  if (filePath) return `資料リンク: ${filePath}`;
  if (externalUrl) return `外部リンク: ${externalUrl}`;
  if (recordTableId) return "台帳リンク";
  return "根拠資料";
}

function matchesQuery(item: ListItem, query: string) {
  if (!query) return true;
  const haystack = [item.title, item.body, item.type, item.source]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
  return haystack.includes(query);
}

function itemSelection(item: ListItem): Selection {
  return { kind: item.kind, id: item.id } as Selection;
}

function sameSelection(a: Selection, b: Selection) {
  return !!a && !!b && a.kind === b.kind && a.id === b.id;
}

export function ProjectInformationPanel({ project }: { project: ProjectInfo }) {
  const [data, setData] = useState<InformationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [activeView, setActiveView] = useState("all");
  const [selection, setSelection] = useState<Selection>(null);
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [saving, setSaving] = useState(false);
  const [organizerOpen, setOrganizerOpen] = useState(false);
  const [organizerPath, setOrganizerPath] = useState("");
  const [organizerUseLlm, setOrganizerUseLlm] = useState(true);
  const [organizerLoading, setOrganizerLoading] = useState(false);
  const [organizerApplying, setOrganizerApplying] = useState(false);
  const [organizerResult, setOrganizerResult] = useState<OrganizerResponse | null>(
    null,
  );
  const [organizerError, setOrganizerError] = useState("");
  const [tableEditor, setTableEditor] = useState<RecordTableSummary | null>(null);

  const [categoryForm, setCategoryForm] = useState({ label: "", description: "" });
  const [documentForm, setDocumentForm] = useState<DocumentForm>(EMPTY_DOCUMENT_FORM);
  const [factForm, setFactForm] = useState<FactForm>(EMPTY_FACT_FORM);
  const [documentEdit, setDocumentEdit] =
    useState<DocumentForm>(EMPTY_DOCUMENT_FORM);
  const [factEdit, setFactEdit] = useState<FactForm>(EMPTY_FACT_FORM);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiFetch<InformationResponse>(
        `/api/projects/${project.id}/information`,
      );
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "案件情報の取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [project.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const categories = useMemo(
    () =>
      (data?.categories ?? [])
        .filter((category) => category.status !== "archived")
        .sort(
          (a, b) =>
            Number(a.sortOrder ?? a.sort_order ?? 0) -
              Number(b.sortOrder ?? b.sort_order ?? 0) ||
            a.label.localeCompare(b.label, "ja"),
        ),
    [data?.categories],
  );

  const categoryMap = useMemo(
    () => new Map(categories.map((category) => [category.id, category])),
    [categories],
  );

  const facts = useMemo(
    () => (data?.facts ?? []).filter((fact) => fact.status !== "archived"),
    [data?.facts],
  );

  const documents = useMemo(
    () =>
      [...(data?.documents ?? []), ...(data?.management_documents ?? [])].filter(
        (doc) => doc.status !== "archived",
      ),
    [data?.documents, data?.management_documents],
  );

  const recordTables = useMemo(
    () => data?.record_tables ?? [],
    [data?.record_tables],
  );
  const managementDocuments = documents.filter((doc) => doc.synthetic);
  const registeredDocuments = documents.filter((doc) => !doc.synthetic);
  const activeCategories = categories.filter((category) => category.status === "active");
  const suggestedCategories = categories.filter(
    (category) => category.status === "suggested",
  );

  const listItems = useMemo<ListItem[]>(() => {
    const factItems: ListItem[] = facts.map((fact) => ({
      kind: "fact",
      id: fact.id,
      title: fact.title,
      body: fact.content,
      type: field(fact.factType, fact.fact_type) || "fact",
      categoryId: categoryIdOf(fact),
      source: field(fact.sourceRef, fact.source_ref),
      importance: fact.importance,
    }));
    const documentItems: ListItem[] = documents.map((doc) => ({
      kind: "document",
      id: doc.id,
      title: doc.title,
      body: documentDisplayDescription(doc),
      type: field(doc.documentType, doc.document_type) || "document",
      role: doc.role,
      categoryId: categoryIdOf(doc),
      source:
        field(doc.filePath, doc.file_path) ||
        field(doc.externalUrl, doc.external_url) ||
        field(doc.sourceRef, doc.source_ref),
      synthetic: doc.synthetic,
    }));
    const tableItems: ListItem[] = recordTables.map((table) => ({
      kind: "table",
      id: table.id,
      title: table.name,
      body: table.description || `${table.row_count ?? 0} 行`,
      type: "record_table",
      categoryId: null,
      source: null,
    }));
    return [...factItems, ...tableItems, ...documentItems];
  }, [documents, facts, recordTables]);

  const normalizedQuery = query.trim().toLowerCase();

  const selectedFact =
    selection?.kind === "fact" ? facts.find((fact) => fact.id === selection.id) : null;
  const selectedDocument =
    selection?.kind === "document"
      ? documents.find((doc) => doc.id === selection.id)
      : null;

  useEffect(() => {
    if (selection && !listItems.some((item) => sameSelection(itemSelection(item), selection))) {
      setSelection(null);
    }
  }, [listItems, selection]);

  useEffect(() => {
    if (!selectedFact) return;
    setFactEdit({
      title: selectedFact.title,
      categoryId: categoryIdOf(selectedFact) || "",
      factType: field(selectedFact.factType, selectedFact.fact_type) || "fact",
      content: selectedFact.content,
      sourceRef: field(selectedFact.sourceRef, selectedFact.source_ref) || "",
      importance: String(selectedFact.importance ?? 5),
    });
  }, [selectedFact]);

  useEffect(() => {
    if (!selectedDocument) return;
    setDocumentEdit({
      title: selectedDocument.title,
      categoryId: categoryIdOf(selectedDocument) || "",
      documentType:
        field(selectedDocument.documentType, selectedDocument.document_type) ||
        "document",
      targetKind:
        field(selectedDocument.targetKind, selectedDocument.target_kind) || "file",
      filePath: field(selectedDocument.filePath, selectedDocument.file_path) || "",
      recordTableId:
        field(selectedDocument.recordTableId, selectedDocument.record_table_id) || "",
      externalUrl:
        field(selectedDocument.externalUrl, selectedDocument.external_url) || "",
      role: selectedDocument.role || "reference",
      aiAccessLevel:
        field(selectedDocument.aiAccessLevel, selectedDocument.ai_access_level) ||
        "metadata",
      description: documentDisplayDescription(selectedDocument),
      notes: selectedDocument.notes || "",
    });
  }, [selectedDocument]);

  const postItem = useCallback(
    async (body: Record<string, unknown>) => {
      setSaving(true);
      setError("");
      try {
        await apiFetch(`/api/projects/${project.id}/information`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        setDialog(null);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "保存に失敗しました");
      } finally {
        setSaving(false);
      }
    },
    [load, project.id],
  );

  const patchItem = useCallback(
    async (body: Record<string, unknown>) => {
      setSaving(true);
      setError("");
      try {
        await apiFetch(`/api/projects/${project.id}/information`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        await load();
        return true;
      } catch (err) {
        setError(err instanceof Error ? err.message : "更新に失敗しました");
        return false;
      } finally {
        setSaving(false);
      }
    },
    [load, project.id],
  );

  const deleteItem = useCallback(
    async (kind: string, id: string) => {
      setSaving(true);
      setError("");
      try {
        await apiFetch(`/api/projects/${project.id}/information`, {
          method: "DELETE",
          body: JSON.stringify({ kind, id }),
        });
        setSelection(null);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "削除に失敗しました");
      } finally {
        setSaving(false);
      }
    },
    [load, project.id],
  );

  const runOrganizer = useCallback(
    async (apply: boolean) => {
      if (apply && !organizerResult) return;
      if (apply) setOrganizerApplying(true);
      else setOrganizerLoading(true);
      setOrganizerError("");
      try {
        const result = await apiFetch<OrganizerResponse>(
          `/api/python-proxy/projects/${project.id}/information/organize-folder`,
          {
            method: "POST",
            body: JSON.stringify({
              path: organizerPath.trim(),
              apply,
              use_llm: organizerUseLlm,
              max_files: 80,
              draft: apply ? organizerResult?.draft : undefined,
            }),
          },
        );
        setOrganizerResult(result);
        if (apply) {
          await load();
        }
      } catch (err) {
        setOrganizerError(
          err instanceof Error
            ? err.message
            : "案件資料の整理に失敗しました",
        );
      } finally {
        setOrganizerLoading(false);
        setOrganizerApplying(false);
      }
    },
    [load, organizerPath, organizerResult, organizerUseLlm, project.id],
  );

  const openAddFact = () => {
    setFactForm({
      ...EMPTY_FACT_FORM,
      categoryId: activeView.startsWith("category:")
        ? activeView.slice("category:".length)
        : "",
    });
    setDialog("fact");
  };

  const openAddDocument = () => {
    setDocumentForm({
      ...EMPTY_DOCUMENT_FORM,
      categoryId: activeView.startsWith("category:")
        ? activeView.slice("category:".length)
        : "",
    });
    setDialog("document");
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        案件情報を読み込み中...
      </div>
    );
  }

  if (!data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-destructive">
          {error || "案件情報の取得に失敗しました"}
        </p>
        <Button variant="outline" size="sm" onClick={load}>
          <RefreshCw className="mr-1 size-3" />
          再読込
        </Button>
      </div>
    );
  }

  const totalCount = facts.length + registeredDocuments.length + recordTables.length;
  const selectedCategoryId = activeView.startsWith("category:")
    ? activeView.slice("category:".length)
    : "";

  const itemMatchesSearch = (kind: ListItem["kind"], id: string) => {
    if (!normalizedQuery) return true;
    const item = listItems.find(
      (candidate) => candidate.kind === kind && candidate.id === id,
    );
    return item ? matchesQuery(item, normalizedQuery) : false;
  };

  const factMatchesView = (fact: ProjectFact) => {
    if (activeView === "documents" || activeView === "management" || activeView === "tables") {
      return false;
    }
    if (selectedCategoryId && categoryIdOf(fact) !== selectedCategoryId) {
      return false;
    }
    return itemMatchesSearch("fact", fact.id);
  };

  const documentMatchesView = (document: ProjectDocument) => {
    const synthetic = !!document.synthetic;
    if (activeView === "facts" || activeView === "tables") return false;
    if (activeView === "documents" && synthetic) return false;
    if (activeView === "management" && !synthetic) return false;
    if (selectedCategoryId && categoryIdOf(document) !== selectedCategoryId) {
      return false;
    }
    return itemMatchesSearch("document", document.id);
  };

  const tableMatchesView = (table: RecordTableSummary) => {
    if (activeView !== "all" && activeView !== "tables") return false;
    return itemMatchesSearch("table", table.id);
  };

  const factCategoryKey = (fact: ProjectFact) => {
    const categoryId = categoryIdOf(fact);
    return categoryId ? (categoryMap.get(categoryId)?.key ?? "") : "";
  };

  const factType = (fact: ProjectFact) =>
    (field(fact.factType, fact.fact_type) || "fact").toLowerCase();

  const sortFacts = (items: ProjectFact[]) =>
    [...items].sort(
      (a, b) =>
        Number(b.importance ?? 0) - Number(a.importance ?? 0) ||
        a.title.localeCompare(b.title, "ja"),
    );

  const usedFactIds = new Set<string>();
  const takeFacts = (predicate: (fact: ProjectFact) => boolean) => {
    const items = sortFacts(
      facts.filter(
        (fact) => factMatchesView(fact) && !usedFactIds.has(fact.id) && predicate(fact),
      ),
    );
    items.forEach((fact) => usedFactIds.add(fact.id));
    return items;
  };

  const factSections = [
    {
      id: "overview",
      title: "概要",
      facts: takeFacts((fact) => {
        const categoryKey = factCategoryKey(fact);
        const type = factType(fact);
        return (
          categoryKey === "overview" ||
          type === "overview" ||
          fact.title.includes("概要")
        );
      }),
    },
    {
      id: "decisions",
      title: "決定事項",
      facts: takeFacts((fact) => {
        const categoryKey = factCategoryKey(fact);
        const type = factType(fact);
        return categoryKey === "decisions" || type === "decision";
      }),
    },
    {
      id: "open-questions",
      title: "要確認・リスク",
      facts: takeFacts((fact) => {
        const categoryKey = factCategoryKey(fact);
        const type = factType(fact);
        return (
          categoryKey === "open_questions" ||
          categoryKey === "risks" ||
          type === "open_question" ||
          type === "risk" ||
          fact.title.includes("要確認")
        );
      }),
    },
    {
      id: "design",
      title: "構成・設計",
      facts: takeFacts((fact) => {
        const categoryKey = factCategoryKey(fact);
        const type = factType(fact);
        return (
          [
            "architecture",
            "detail_design",
            "edge_firewall",
            "building_switches",
            "control_core_switch",
            "existing_configuration",
            "composition",
          ].includes(categoryKey) ||
          ["requirement", "design", "rule"].includes(type)
        );
      }),
    },
    {
      id: "verification",
      title: "検証・移行",
      facts: takeFacts((fact) => {
        const categoryKey = factCategoryKey(fact);
        const type = factType(fact);
        return (
          ["verification", "migration", "schedule"].includes(categoryKey) ||
          ["verification", "milestone", "task"].includes(type)
        );
      }),
    },
    {
      id: "other",
      title: "その他",
      facts: takeFacts(() => true),
    },
  ].filter((section) => section.facts.length > 0);

  const heroFact =
    facts.find((fact) => {
      const categoryKey = factCategoryKey(fact);
      const type = factType(fact);
      return (
        categoryKey === "overview" ||
        type === "overview" ||
        fact.title.includes("概要")
      );
    }) ?? facts[0] ?? null;

  const visibleRecordTables = recordTables.filter(tableMatchesView);
  const visibleRegisteredDocuments = registeredDocuments.filter(documentMatchesView);
  const visibleManagementDocuments = managementDocuments.filter(documentMatchesView);
  const hasVisibleContent =
    factSections.length > 0 ||
    visibleRecordTables.length > 0 ||
    visibleRegisteredDocuments.length > 0 ||
    visibleManagementDocuments.length > 0;

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b pb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <BookOpen className="size-4 text-primary" />
            <h2 className="truncate text-sm font-semibold">案件ページ</h2>
            <Badge variant="secondary">{totalCount} 件</Badge>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw className="mr-1 size-3.5" />
            再読込
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setOrganizerOpen(true);
              setOrganizerError("");
            }}
          >
            <FolderSearch className="mr-1 size-3.5" />
            取り込み案
          </Button>
          <Button variant="outline" size="sm" onClick={openAddDocument}>
            <Link2 className="mr-1 size-3.5" />
            根拠資料
          </Button>
          <Button size="sm" onClick={openAddFact}>
            <Plus className="mr-1 size-3.5" />
            案件情報
          </Button>
        </div>
      </div>

      {error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="grid min-h-[720px] overflow-hidden rounded-md border bg-white/75 shadow-sm dark:bg-slate-950/50 xl:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="min-h-0 border-b bg-slate-50/80 p-3 dark:bg-slate-950/60 xl:border-b-0 xl:border-r">
          <div className="relative mb-3">
            <Search className="absolute left-2 top-2.5 size-4 text-muted-foreground" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="案件内を検索"
              className="pl-8"
            />
          </div>

          <div className="space-y-1">
            <NavigationButton
              active={activeView === "all"}
              label="すべて"
              count={totalCount}
              onClick={() => setActiveView("all")}
            />
            <NavigationButton
              active={activeView === "facts"}
              label="案件情報"
              count={facts.length}
              icon={<Info className="size-3.5" />}
              onClick={() => setActiveView("facts")}
            />
            <NavigationButton
              active={activeView === "tables"}
              label="台帳"
              count={recordTables.length}
              icon={<Table2 className="size-3.5" />}
              onClick={() => setActiveView("tables")}
            />
            <NavigationButton
              active={activeView === "documents"}
              label="根拠資料"
              count={registeredDocuments.length}
              icon={<FileText className="size-3.5" />}
              onClick={() => setActiveView("documents")}
            />
            <NavigationButton
              active={activeView === "management"}
              label="管理資料"
              count={managementDocuments.length}
              icon={<Database className="size-3.5" />}
              onClick={() => setActiveView("management")}
            />
          </div>

          <div className="mt-4 border-t pt-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">
                カテゴリ
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                onClick={() => {
                  setCategoryForm({ label: "", description: "" });
                  setDialog("category");
                }}
              >
                <Plus className="size-3.5" />
              </Button>
            </div>
            <div className="space-y-1">
              {activeCategories.map((category) => (
                <NavigationButton
                  key={category.id}
                  active={activeView === `category:${category.id}`}
                  label={category.label}
                  count={
                    listItems.filter((item) => item.categoryId === category.id).length
                  }
                  onClick={() => setActiveView(`category:${category.id}`)}
                />
              ))}
            </div>
          </div>

          {suggestedCategories.length > 0 && (
            <div className="mt-4 border-t pt-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <Sparkles className="size-3.5" />
                候補カテゴリ
              </div>
              <div className="space-y-2">
                {suggestedCategories.map((category) => (
                  <div
                    key={category.id}
                    className="rounded-md border bg-white/80 p-2 dark:bg-slate-950/70"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">
                        {category.label}
                      </span>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-7"
                        onClick={() =>
                          patchItem({
                            kind: "category",
                            id: category.id,
                            status: "active",
                          })
                        }
                      >
                        <Check className="size-3.5" />
                      </Button>
                    </div>
                    {category.description && (
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                        {category.description}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </aside>

        <main className="min-h-0 overflow-auto">
          <section className="border-b bg-slate-50/80 dark:bg-slate-900/30">
            <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-5 py-6 md:px-8">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 max-w-4xl">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <BookOpen className="size-4 text-primary" />
                    <span>案件情報</span>
                    <Badge variant="secondary">{facts.length} 件</Badge>
                    <Badge variant="outline">{recordTables.length} 台帳</Badge>
                    <Badge variant="outline">{registeredDocuments.length} 資料</Badge>
                  </div>
                  <h3 className="mt-3 text-2xl font-semibold leading-tight md:text-3xl">
                    {project.name}
                  </h3>
                  {heroFact ? (
                    <p className="mt-4 max-w-3xl whitespace-pre-wrap text-sm leading-7 text-foreground/90">
                      {heroFact.content}
                    </p>
                  ) : (
                    <p className="mt-4 text-sm text-muted-foreground">
                      案件概要は未登録です。
                    </p>
                  )}
                </div>
                <div className="grid w-full max-w-md grid-cols-3 gap-2">
                  <MetricCard label="案件情報" value={facts.length} />
                  <MetricCard label="台帳" value={recordTables.length} />
                  <MetricCard label="資料" value={registeredDocuments.length} />
                </div>
              </div>
            </div>
          </section>

          <div className="mx-auto grid w-full max-w-7xl gap-8 px-5 py-6 md:px-8 2xl:grid-cols-[minmax(0,820px)_minmax(300px,1fr)]">
            <div className="min-w-0 space-y-8">
              {!hasVisibleContent && (
                <div className="rounded-md border bg-white/80 p-6 text-sm text-muted-foreground dark:bg-slate-950/70">
                  表示できる案件情報がありません。
                </div>
              )}

              {factSections.map((section) => (
                <section key={section.id} id={section.id} className="scroll-mt-6">
                  <SectionHeading title={section.title} count={section.facts.length} />
                  <div className="mt-3 space-y-3">
                    {section.facts.map((fact) => {
                      const editing = sameSelection(selection, {
                        kind: "fact",
                        id: fact.id,
                      });
                      return (
                        <FactDetail
                          key={fact.id}
                          fact={fact}
                          categories={activeCategories}
                          category={categoryMap.get(categoryIdOf(fact) || "") ?? null}
                          editing={editing}
                          form={factEdit}
                          setForm={setFactEdit}
                          saving={saving}
                          onEdit={() => setSelection({ kind: "fact", id: fact.id })}
                          onCancel={() => setSelection(null)}
                          onSave={async () => {
                            const saved = await patchItem({
                              kind: "fact",
                              id: fact.id,
                              title: factEdit.title,
                              category_id: factEdit.categoryId || null,
                              fact_type: factEdit.factType,
                              content: factEdit.content,
                              source_ref: factEdit.sourceRef,
                              importance: Number(factEdit.importance || 5),
                            });
                            if (saved) setSelection(null);
                          }}
                          onDelete={() => deleteItem("fact", fact.id)}
                        />
                      );
                    })}
                  </div>
                </section>
              ))}

              {visibleRecordTables.length > 0 && (
                <section id="tables" className="scroll-mt-6">
                  <SectionHeading title="台帳" count={visibleRecordTables.length} />
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    {visibleRecordTables.map((table) => (
                      <TableDetail
                        key={table.id}
                        table={table}
                        onOpen={() => setTableEditor(table)}
                      />
                    ))}
                  </div>
                </section>
              )}
            </div>

            <div className="min-w-0 space-y-8">
              {visibleRegisteredDocuments.length > 0 && (
                <section id="documents" className="scroll-mt-6">
                  <SectionHeading
                    title="根拠資料"
                    count={visibleRegisteredDocuments.length}
                  />
                  <div className="mt-3 space-y-3">
                    {visibleRegisteredDocuments.map((document) => {
                      const editing = sameSelection(selection, {
                        kind: "document",
                        id: document.id,
                      });
                      return (
                        <DocumentDetail
                          key={document.id}
                          document={document}
                          categories={activeCategories}
                          tables={recordTables}
                          category={categoryMap.get(categoryIdOf(document) || "") ?? null}
                          editing={editing}
                          form={documentEdit}
                          setForm={setDocumentEdit}
                          saving={saving}
                          onEdit={() =>
                            setSelection({ kind: "document", id: document.id })
                          }
                          onCancel={() => setSelection(null)}
                          onSave={async () => {
                            const saved = await patchItem({
                              kind: "document",
                              id: document.id,
                              title: documentEdit.title,
                              category_id: documentEdit.categoryId || null,
                              document_type: documentEdit.documentType,
                              target_kind: documentEdit.targetKind,
                              file_path: documentEdit.filePath,
                              record_table_id: documentEdit.recordTableId,
                              external_url: documentEdit.externalUrl,
                              role: documentEdit.role,
                              is_primary: documentEdit.role === "primary",
                              ai_access_level: documentEdit.aiAccessLevel,
                              description: documentEdit.description,
                              notes: documentEdit.notes,
                            });
                            if (saved) setSelection(null);
                          }}
                          onDelete={() => deleteItem("document", document.id)}
                        />
                      );
                    })}
                  </div>
                </section>
              )}

              {visibleManagementDocuments.length > 0 && (
                <section id="management" className="scroll-mt-6">
                  <SectionHeading
                    title="管理資料"
                    count={visibleManagementDocuments.length}
                  />
                  <div className="mt-3 space-y-3">
                    {visibleManagementDocuments.map((document) => (
                      <DocumentDetail
                        key={document.id}
                        document={document}
                        categories={activeCategories}
                        tables={recordTables}
                        category={categoryMap.get(categoryIdOf(document) || "") ?? null}
                        editing={false}
                        form={documentEdit}
                        setForm={setDocumentEdit}
                        saving={saving}
                        onEdit={() => undefined}
                        onCancel={() => undefined}
                        onSave={() => undefined}
                        onDelete={() => undefined}
                      />
                    ))}
                  </div>
                </section>
              )}
            </div>
          </div>
        </main>
      </div>

      <OrganizerDialog
        open={organizerOpen}
        onOpenChange={setOrganizerOpen}
        path={organizerPath}
        setPath={setOrganizerPath}
        useLlm={organizerUseLlm}
        setUseLlm={setOrganizerUseLlm}
        result={organizerResult}
        error={organizerError}
        loading={organizerLoading}
        applying={organizerApplying}
        onPreview={() => runOrganizer(false)}
        onApply={() => runOrganizer(true)}
      />

      <Dialog open={dialog === "category"} onOpenChange={(open) => !open && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>カテゴリ追加</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>名前</Label>
              <Input
                value={categoryForm.label}
                onChange={(event) =>
                  setCategoryForm((prev) => ({ ...prev, label: event.target.value }))
                }
              />
            </div>
            <div>
              <Label>説明</Label>
              <Textarea
                value={categoryForm.description}
                onChange={(event) =>
                  setCategoryForm((prev) => ({
                    ...prev,
                    description: event.target.value,
                  }))
                }
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              onClick={() =>
                postItem({
                  kind: "category",
                  label: categoryForm.label,
                  description: categoryForm.description,
                })
              }
              disabled={saving || !categoryForm.label.trim()}
            >
              追加
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DocumentDialog
        open={dialog === "document"}
        onOpenChange={(open) => !open && setDialog(null)}
        form={documentForm}
        setForm={setDocumentForm}
        categories={activeCategories}
        tables={recordTables}
        saving={saving}
        onSubmit={() =>
          postItem({
            kind: "document",
            title: documentForm.title,
            category_id: documentForm.categoryId || null,
            document_type: documentForm.documentType,
            target_kind: documentForm.targetKind,
            file_path: documentForm.filePath,
            record_table_id: documentForm.recordTableId,
            external_url: documentForm.externalUrl,
            role: documentForm.role,
            is_primary: documentForm.role === "primary",
            ai_access_level: documentForm.aiAccessLevel,
            description: documentForm.description,
            notes: documentForm.notes,
          })
        }
      />

      <FactDialog
        open={dialog === "fact"}
        onOpenChange={(open) => !open && setDialog(null)}
        form={factForm}
        setForm={setFactForm}
        categories={activeCategories}
        saving={saving}
        onSubmit={() =>
          postItem({
            kind: "fact",
            title: factForm.title,
            category_id: factForm.categoryId || null,
            fact_type: factForm.factType,
            content: factForm.content,
            source_ref: factForm.sourceRef,
            importance: Number(factForm.importance || 5),
          })
        }
      />

      <Dialog open={!!tableEditor} onOpenChange={(open) => !open && setTableEditor(null)}>
        <DialogContent className="h-[88vh] max-w-[96vw] p-0">
          {tableEditor && (
            <RecordTableEditor
              projectId={project.id}
              tableId={tableEditor.id}
              initialName={tableEditor.name}
              onClose={() => setTableEditor(null)}
              onChanged={load}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function NavigationButton({
  active,
  label,
  count,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  icon?: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
        active ? "bg-primary text-primary-foreground" : "hover:bg-muted"
      }`}
      onClick={onClick}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        {icon}
        <span className="truncate">{label}</span>
      </span>
      <span className="text-xs opacity-70">{count}</span>
    </button>
  );
}

function MetricCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border bg-white/80 px-3 py-2 shadow-sm dark:bg-slate-950/60">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function SectionHeading({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b pb-2">
      <h4 className="text-base font-semibold">{title}</h4>
      <Badge variant="secondary">{count} 件</Badge>
    </div>
  );
}

function CategorySelect({
  value,
  categories,
  onChange,
}: {
  value: string;
  categories: ProjectInfoCategory[];
  onChange: (value: string) => void;
}) {
  return (
    <select
      className="h-9 w-full rounded-md border bg-background px-3 text-sm"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">未分類</option>
      {categories.map((category) => (
        <option key={category.id} value={category.id}>
          {category.label}
        </option>
      ))}
    </select>
  );
}

function FactDetail({
  fact,
  categories,
  category,
  editing,
  form,
  setForm,
  saving,
  onEdit,
  onCancel,
  onSave,
  onDelete,
}: {
  fact: ProjectFact;
  categories: ProjectInfoCategory[];
  category: ProjectInfoCategory | null;
  editing: boolean;
  form: FactForm;
  setForm: React.Dispatch<React.SetStateAction<FactForm>>;
  saving: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void | Promise<void>;
  onDelete: () => void;
}) {
  const sources = splitSourceRef(field(fact.sourceRef, fact.source_ref));
  const tableLike = isTableLikeContent(fact.content);
  const displayType = field(fact.factType, fact.fact_type) || "fact";

  if (!editing) {
    return (
      <article
        className={`rounded-md border bg-white/90 p-4 shadow-sm dark:bg-slate-950/70 ${
          tableLike ? "border-amber-500/40" : ""
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Info className="size-4 shrink-0 text-primary" />
              <h5 className="min-w-0 text-sm font-semibold leading-6">
                {fact.title}
              </h5>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Badge variant="secondary" className="text-[10px]">
                {factTypeLabel(displayType)}
              </Badge>
              {category && (
                <Badge variant="outline" className="text-[10px]">
                  {category.label}
                </Badge>
              )}
              {fact.importance != null && (
                <Badge variant="outline" className="text-[10px]">
                  重要度 {fact.importance}
                </Badge>
              )}
              {tableLike && (
                <Badge variant="outline" className="text-[10px]">
                  台帳化候補
                </Badge>
              )}
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onEdit}
            aria-label="案件情報を編集"
          >
            <Pencil className="size-4" />
          </Button>
        </div>

        {tableLike ? (
          <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs leading-5 text-foreground/90">
            表形式の内容です。本文には展開せず、台帳として扱う対象です。
          </div>
        ) : (
          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-foreground/90">
            {fact.content || "内容未設定"}
          </p>
        )}

        {sources.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {sources.slice(0, 4).map((source) => (
              <span
                key={source}
                className="max-w-full truncate rounded border bg-muted/20 px-2 py-1 text-[11px] text-muted-foreground"
              >
                {source}
              </span>
            ))}
            {sources.length > 4 && (
              <span className="rounded border px-2 py-1 text-[11px] text-muted-foreground">
                +{sources.length - 4}
              </span>
            )}
          </div>
        )}
      </article>
    );
  }

  return (
    <article className="space-y-4 rounded-md border border-primary/40 bg-white/90 p-4 shadow-sm dark:bg-slate-950/70">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Info className="size-4 text-primary" />
            <h5 className="text-sm font-semibold">案件情報を編集</h5>
            <Badge variant="secondary">{factTypeLabel(form.factType)}</Badge>
            {tableLike && <Badge variant="outline">台帳化候補</Badge>}
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          <Button size="sm" onClick={onSave} disabled={saving || !form.title.trim()}>
            <Save className="mr-1 size-3.5" />
            保存
          </Button>
          <Button variant="outline" size="sm" onClick={onCancel} disabled={saving}>
            キャンセル
          </Button>
          <Button variant="ghost" size="icon" onClick={onDelete} disabled={saving}>
            <Archive className="size-4" />
          </Button>
        </div>
      </div>

      {tableLike && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-5">
          この内容はMarkdown表または表の行に見えます。案件情報の本文として読ませるのではなく、接続構成・機器一覧などの台帳に移す対象です。
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <Label>タイトル</Label>
          <Input
            value={form.title}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, title: event.target.value }))
            }
          />
        </div>
        <div>
          <Label>カテゴリ</Label>
          <CategorySelect
            value={form.categoryId}
            categories={categories}
            onChange={(value) => setForm((prev) => ({ ...prev, categoryId: value }))}
          />
        </div>
        <div>
          <Label>種別</Label>
          <Input
            value={form.factType}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, factType: event.target.value }))
            }
          />
        </div>
        <div>
          <Label>重要度</Label>
          <Input
            type="number"
            min={1}
            max={10}
            value={form.importance}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, importance: event.target.value }))
            }
          />
        </div>
        <div className="md:col-span-2">
          <Label>内容</Label>
          <Textarea
            value={form.content}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, content: event.target.value }))
            }
            className="min-h-44 resize-y whitespace-pre-wrap break-words"
          />
        </div>
        <div className="md:col-span-2">
          <Label>根拠</Label>
          <Input
            value={form.sourceRef}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, sourceRef: event.target.value }))
            }
          />
          {sources.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {sources.map((source) => (
                <span
                  key={source}
                  className="max-w-full truncate rounded border px-2 py-1 text-xs text-muted-foreground"
                >
                  {source}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

function DocumentDetail({
  document,
  categories,
  tables,
  category,
  editing,
  form,
  setForm,
  saving,
  onEdit,
  onCancel,
  onSave,
  onDelete,
}: {
  document: ProjectDocument;
  categories: ProjectInfoCategory[];
  tables: RecordTableSummary[];
  category: ProjectInfoCategory | null;
  editing: boolean;
  form: DocumentForm;
  setForm: React.Dispatch<React.SetStateAction<DocumentForm>>;
  saving: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onSave: () => void | Promise<void>;
  onDelete: () => void;
}) {
  const synthetic = !!document.synthetic;
  const source =
    field(document.filePath, document.file_path) ||
    field(document.externalUrl, document.external_url) ||
    field(document.sourceRef, document.source_ref);
  const targetKind = field(document.targetKind, document.target_kind) || "file";
  const documentType =
    field(document.documentType, document.document_type) || "document";
  const description = documentDisplayDescription(document);

  if (!editing) {
    return (
      <article className="rounded-md border bg-white/90 p-4 shadow-sm dark:bg-slate-950/70">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {synthetic ? (
                <Database className="size-4 shrink-0 text-muted-foreground" />
              ) : (
                <FileText className="size-4 shrink-0 text-primary" />
              )}
              <h5 className="min-w-0 text-sm font-semibold leading-6">
                {document.title}
              </h5>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Badge variant="secondary" className="text-[10px]">
                {synthetic ? "管理資料" : roleLabel(document.role)}
              </Badge>
              <Badge variant="outline" className="text-[10px]">
                {targetLabel(targetKind)}
              </Badge>
              {category && (
                <Badge variant="outline" className="text-[10px]">
                  {category.label}
                </Badge>
              )}
              {documentType && (
                <Badge variant="outline" className="text-[10px]">
                  {documentType}
                </Badge>
              )}
            </div>
          </div>
          {!synthetic && (
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              onClick={onEdit}
              aria-label="根拠資料を編集"
            >
              <Pencil className="size-4" />
            </Button>
          )}
        </div>

        {description && (
          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-foreground/85">
            {description}
          </p>
        )}

        {source && (
          <div className="mt-3 rounded-md border bg-muted/20 px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
            <div className="break-all">{source}</div>
          </div>
        )}
      </article>
    );
  }

  return (
    <article className="space-y-4 rounded-md border border-primary/40 bg-white/90 p-4 shadow-sm dark:bg-slate-950/70">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {synthetic ? (
              <Database className="size-4 text-muted-foreground" />
            ) : (
              <FileText className="size-4 text-primary" />
            )}
            <h3 className="truncate text-sm font-semibold">
              {synthetic ? "管理資料" : "根拠資料"}
            </h3>
            <Badge variant="secondary">{roleLabel(form.role)}</Badge>
            <Badge variant="outline">{targetLabel(form.targetKind)}</Badge>
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          {!synthetic && (
            <>
              <Button size="sm" onClick={onSave} disabled={saving || !form.title.trim()}>
                <Save className="mr-1 size-3.5" />
                保存
              </Button>
              <Button variant="outline" size="sm" onClick={onCancel} disabled={saving}>
                キャンセル
              </Button>
              <Button variant="ghost" size="icon" onClick={onDelete} disabled={saving}>
                <Archive className="size-4" />
              </Button>
            </>
          )}
        </div>
      </div>

      {synthetic && (
        <div className="rounded-md border bg-muted/30 p-3 text-xs leading-5 text-muted-foreground">
          これは管理資料設定から合成表示された参照情報です。ここでは案件DBの根拠として表示し、編集対象にはしません。
        </div>
      )}

      <DocumentFields
        form={form}
        setForm={setForm}
        categories={categories}
        tables={tables}
        disabled={synthetic}
      />

      {source && (
        <div className="rounded-md border bg-muted/20 p-3 text-xs">
          <div className="mb-1 font-medium">参照先</div>
          <div className="break-all text-muted-foreground">{source}</div>
        </div>
      )}
    </article>
  );
}

function TableDetail({
  table,
  onOpen,
}: {
  table: RecordTableSummary;
  onOpen: () => void;
}) {
  return (
    <article className="space-y-3 rounded-md border bg-white/90 p-4 shadow-sm dark:bg-slate-950/70">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Table2 className="size-4 text-primary" />
            <h3 className="truncate text-sm font-semibold">{table.name}</h3>
            <Badge variant="secondary">{table.row_count ?? 0} 行</Badge>
          </div>
        </div>
        <Button size="sm" onClick={onOpen}>
          <Pencil className="mr-1 size-3.5" />
          台帳を編集
        </Button>
      </div>
      <div className="rounded-md border bg-muted/20 p-3 text-sm">
        {table.description || "説明は未設定です。"}
      </div>
    </article>
  );
}

function DocumentFields({
  form,
  setForm,
  categories,
  tables,
  disabled = false,
}: {
  form: DocumentForm;
  setForm: React.Dispatch<React.SetStateAction<DocumentForm>>;
  categories: ProjectInfoCategory[];
  tables: RecordTableSummary[];
  disabled?: boolean;
}) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div className="md:col-span-2">
        <Label>資料名</Label>
        <Input
          value={form.title}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, title: event.target.value }))
          }
        />
      </div>
      <div>
        <Label>カテゴリ</Label>
        <CategorySelect
          value={form.categoryId}
          categories={categories}
          onChange={(value) => setForm((prev) => ({ ...prev, categoryId: value }))}
        />
      </div>
      <div>
        <Label>資料種別</Label>
        <Input
          value={form.documentType}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, documentType: event.target.value }))
          }
        />
      </div>
      <div>
        <Label>リンク種別</Label>
        <select
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={form.targetKind}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({
              ...prev,
              targetKind: event.target.value as TargetKind,
            }))
          }
        >
          <option value="file">ファイル</option>
          <option value="record_table">台帳</option>
          <option value="url">URL</option>
        </select>
      </div>
      <div>
        <Label>役割</Label>
        <select
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={form.role}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, role: event.target.value }))
          }
        >
          <option value="primary">正本</option>
          <option value="reference">参照</option>
          <option value="management">管理資料</option>
          <option value="draft">下書き</option>
        </select>
      </div>
      {form.targetKind === "file" && (
        <div className="md:col-span-2">
          <Label>プロジェクトファイルパス</Label>
          <Input
            value={form.filePath}
            disabled={disabled}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, filePath: event.target.value }))
            }
            placeholder="management/parameter-sheet.xlsx"
          />
        </div>
      )}
      {form.targetKind === "record_table" && (
        <div className="md:col-span-2">
          <Label>台帳</Label>
          <select
            className="h-9 w-full rounded-md border bg-background px-3 text-sm"
            value={form.recordTableId}
            disabled={disabled}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, recordTableId: event.target.value }))
            }
          >
            <option value="">選択</option>
            {tables.map((table) => (
              <option key={table.id} value={table.id}>
                {table.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {form.targetKind === "url" && (
        <div className="md:col-span-2">
          <Label>URL</Label>
          <Input
            value={form.externalUrl}
            disabled={disabled}
            onChange={(event) =>
              setForm((prev) => ({ ...prev, externalUrl: event.target.value }))
            }
          />
        </div>
      )}
      <div>
        <Label>AIアクセス</Label>
        <select
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
          value={form.aiAccessLevel}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, aiAccessLevel: event.target.value }))
          }
        >
          <option value="metadata">メタデータのみ</option>
          <option value="read">読取可</option>
          <option value="edit">編集可</option>
          <option value="blocked">不可</option>
        </select>
      </div>
      <div>
        <Label>説明</Label>
        <Input
          value={form.description}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, description: event.target.value }))
          }
        />
      </div>
      <div className="md:col-span-2">
        <Label>注意事項</Label>
        <Textarea
          value={form.notes}
          disabled={disabled}
          onChange={(event) =>
            setForm((prev) => ({ ...prev, notes: event.target.value }))
          }
        />
      </div>
    </div>
  );
}

function DocumentDialog({
  open,
  onOpenChange,
  form,
  setForm,
  categories,
  tables,
  saving,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  form: DocumentForm;
  setForm: React.Dispatch<React.SetStateAction<DocumentForm>>;
  categories: ProjectInfoCategory[];
  tables: RecordTableSummary[];
  saving: boolean;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>根拠資料追加</DialogTitle>
        </DialogHeader>
        <DocumentFields
          form={form}
          setForm={setForm}
          categories={categories}
          tables={tables}
        />
        <DialogFooter>
          <Button onClick={onSubmit} disabled={saving || !form.title.trim()}>
            追加
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FactDialog({
  open,
  onOpenChange,
  form,
  setForm,
  categories,
  saving,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  form: FactForm;
  setForm: React.Dispatch<React.SetStateAction<FactForm>>;
  categories: ProjectInfoCategory[];
  saving: boolean;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>案件情報追加</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>タイトル</Label>
            <Input
              value={form.title}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, title: event.target.value }))
              }
            />
          </div>
          <div>
            <Label>カテゴリ</Label>
            <CategorySelect
              value={form.categoryId}
              categories={categories}
              onChange={(value) =>
                setForm((prev) => ({ ...prev, categoryId: value }))
              }
            />
          </div>
          <div>
            <Label>内容</Label>
            <Textarea
              value={form.content}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, content: event.target.value }))
              }
              className="min-h-32"
            />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <Label>種別</Label>
              <Input
                value={form.factType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, factType: event.target.value }))
                }
              />
            </div>
            <div>
              <Label>重要度</Label>
              <Input
                type="number"
                min={1}
                max={10}
                value={form.importance}
                onChange={(event) =>
                  setForm((prev) => ({
                    ...prev,
                    importance: event.target.value,
                  }))
                }
              />
            </div>
          </div>
          <div>
            <Label>根拠</Label>
            <Input
              value={form.sourceRef}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, sourceRef: event.target.value }))
              }
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            onClick={onSubmit}
            disabled={saving || !form.title.trim() || !form.content.trim()}
          >
            追加
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function OrganizerDialog({
  open,
  onOpenChange,
  path,
  setPath,
  useLlm,
  setUseLlm,
  result,
  error,
  loading,
  applying,
  onPreview,
  onApply,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  setPath: (value: string) => void;
  useLlm: boolean;
  setUseLlm: (value: boolean) => void;
  result: OrganizerResponse | null;
  error: string;
  loading: boolean;
  applying: boolean;
  onPreview: () => void;
  onApply: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>資料フォルダから取り込み案を作成</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-[1fr_auto]">
            <div>
              <Label>プロジェクトファイラー内フォルダ</Label>
              <Input
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="例: 6.お客様受領資料"
              />
            </div>
            <div className="flex items-end">
              <Button variant="outline" onClick={onPreview} disabled={loading || applying}>
                {loading ? (
                  <Loader2 className="mr-1 size-3.5 animate-spin" />
                ) : (
                  <Sparkles className="mr-1 size-3.5" />
                )}
                取り込み案
              </Button>
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(event) => setUseLlm(event.target.checked)}
            />
            LLMで案件情報を抽出
          </label>
          {error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </p>
          )}
          {result && (
            <div className="space-y-4">
              <div className="rounded-md border bg-muted/30 p-3">
                <div className="flex flex-wrap gap-2 text-sm">
                  <Badge variant="secondary">
                    {result.draft.generated_by === "llm" ? "LLM整理" : "自動分類"}
                  </Badge>
                  <span>資料 {result.scanned.count} 件</span>
                  <span>根拠資料 {result.draft.documents.length} 件</span>
                  <span>案件情報 {result.draft.facts.length} 件</span>
                </div>
                {result.draft.summary_md && (
                  <p className="mt-2 max-h-24 overflow-auto break-words text-sm text-muted-foreground">
                    {result.draft.summary_md}
                  </p>
                )}
              </div>

              <PreviewList
                title="登録予定の根拠資料"
                empty="登録候補はありません"
                items={result.draft.documents.map((doc) => ({
                  key: `${doc.file_path}:${doc.title}`,
                  title: doc.title,
                  meta: organizerCategoryLabel(doc.category_key),
                  body: doc.file_path,
                }))}
              />

              <PreviewList
                title="登録予定の案件情報"
                empty="登録候補はありません"
                items={result.draft.facts.map((fact, index) => ({
                  key: `${fact.source_ref}:${fact.title}:${index}`,
                  title: fact.title,
                  meta: `${organizerCategoryLabel(fact.category_key)} / 重要度 ${fact.importance}`,
                  body: fact.content,
                }))}
              />

              {result.applied && (
                <p className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">
                  根拠資料 {result.result.documents} 件、案件情報 {result.result.facts} 件を反映しました。
                </p>
              )}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={loading || applying}
          >
            閉じる
          </Button>
          <Button
            onClick={onApply}
            disabled={loading || applying || !result || result.applied}
          >
            {applying && <Loader2 className="mr-1 size-3.5 animate-spin" />}
            承認して反映
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PreviewList({
  title,
  empty,
  items,
}: {
  title: string;
  empty: string;
  items: Array<{ key: string; title: string; meta: string; body: string }>;
}) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">{title}</h3>
      <div className="max-h-56 overflow-y-auto rounded-md border">
        {items.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">{empty}</p>
        ) : (
          items.map((item) => (
            <div key={item.key} className="border-b p-3 last:border-b-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{item.title}</span>
                <Badge variant="outline">{item.meta}</Badge>
              </div>
              <p className="mt-1 line-clamp-2 break-words text-xs text-muted-foreground">
                {item.body}
              </p>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
