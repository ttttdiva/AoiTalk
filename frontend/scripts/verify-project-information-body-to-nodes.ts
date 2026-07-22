import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { asc, isNull } from "drizzle-orm";

// 案件情報 root ページ = parent_id IS NULL かつ node_type が day/system 以外のノード。
// これらの root と全子孫に対して、移行が生成し得るゴミ・重複を検知する。
const NON_ROOT_NODE_TYPES = new Set(["day", "system"]);
const MARKDOWN_HEADING_RE = /^#{1,6}\s/;
const MAX_BODY_TEXT_LENGTH = 500;

function loadEnvFiles() {
  for (const fileName of [".env", ".env.local"]) {
    const filePath = resolve(process.cwd(), fileName);
    if (!existsSync(filePath)) continue;
    for (const line of readFileSync(filePath, "utf8").split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const index = trimmed.indexOf("=");
      const key = trimmed.slice(0, index).trim();
      let value = trimmed.slice(index + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (key && process.env[key] === undefined) process.env[key] = value;
    }
  }
}

type CheckNode = {
  id: string;
  parentId: string | null;
  nodeType: string | null;
  title: string;
  bodyText: string | null;
};

export type Violation = {
  kind: "markdown_heading" | "sibling_duplicate" | "root_nested_duplicate" | "body_text_not_title_mirror";
  rootId: string;
  rootTitle: string;
  nodeId: string;
  title: string;
  detail: string;
};

/**
 * 案件情報 root ページとその全子孫を対象に (a)-(d) の違反を検出する純関数。
 * decryptBodyText は body_text 復号平文を返す関数（暗号ユーティリティを注入）。
 */
export function collectProjectInformationViolations(
  nodes: CheckNode[],
  decryptBodyText: (value: string | null | undefined) => string,
): Violation[] {
  const byId = new Map<string, CheckNode>();
  const childrenByParent = new Map<string, CheckNode[]>();
  for (const node of nodes) {
    byId.set(node.id, node);
  }
  for (const node of nodes) {
    if (!node.parentId) continue;
    // 親も対象ノード集合に存在する場合のみ子として扱う（archivedな親経由は対象外）。
    if (!byId.has(node.parentId)) continue;
    const list = childrenByParent.get(node.parentId) ?? [];
    list.push(node);
    childrenByParent.set(node.parentId, list);
  }

  const roots = nodes.filter(
    (node) => node.parentId === null && !NON_ROOT_NODE_TYPES.has(node.nodeType ?? "node"),
  );

  const violations: Violation[] = [];

  for (const root of roots) {
    // parent_id を辿って root 配下の全子孫（root 自身は別扱い）を収集。
    const pageNodes: CheckNode[] = [root];
    const queue = [...(childrenByParent.get(root.id) ?? [])];
    const seen = new Set<string>([root.id]);
    while (queue.length > 0) {
      const current = queue.shift();
      if (!current || seen.has(current.id)) continue;
      seen.add(current.id);
      pageNodes.push(current);
      queue.push(...(childrenByParent.get(current.id) ?? []));
    }

    const push = (kind: Violation["kind"], node: CheckNode, detail: string) => {
      violations.push({
        kind,
        rootId: root.id,
        rootTitle: root.title,
        nodeId: node.id,
        title: node.title,
        detail,
      });
    };

    // (a) Markdown 見出しノード禁止。
    for (const node of pageNodes) {
      if (MARKDOWN_HEADING_RE.test(node.title)) {
        push("markdown_heading", node, `title が見出しパターンにマッチ: "${node.title}"`);
      }
    }

    // (b) 同一 parent_id の下に同一 title の生存ノードが2件以上あれば失敗。
    //     親をまたいだ同一 title は正当なので対象外。
    const siblingTitleCount = new Map<string, Map<string, CheckNode[]>>();
    for (const node of pageNodes) {
      if (!node.parentId) continue;
      const perParent = siblingTitleCount.get(node.parentId) ?? new Map<string, CheckNode[]>();
      const bucket = perParent.get(node.title) ?? [];
      bucket.push(node);
      perParent.set(node.title, bucket);
      siblingTitleCount.set(node.parentId, perParent);
    }
    for (const perParent of siblingTitleCount.values()) {
      for (const [title, bucket] of perParent) {
        if (bucket.length > 1) {
          for (const node of bucket) {
            push(
              "sibling_duplicate",
              node,
              `同一親 ${node.parentId} の下に title "${title}" が ${bucket.length} 件重複`,
            );
          }
        }
      }
    }

    // (c) root 直下の子の title が、同じページ内のネスト位置（parent が root でないノード）にも
    //     同一 title で存在すれば失敗（root 対ネストのセクション二重生成禁止）。
    const directChildTitles = new Set(
      (childrenByParent.get(root.id) ?? []).map((node) => node.title),
    );
    for (const node of pageNodes) {
      if (node.id === root.id) continue;
      if (node.parentId === root.id) continue; // 直下の子自身はネスト位置ではない
      if (directChildTitles.has(node.title)) {
        push(
          "root_nested_duplicate",
          node,
          `root 直下の子と同一 title "${node.title}" がネスト位置（parent=${node.parentId}）にも存在`,
        );
      }
    }

    // (d) 生存ノードの body_text 復号平文は title ミラーのみが正。
    //     title と不一致、改行を含む、500字超のいずれかで失敗（blob 本文の残存検知）。
    for (const node of pageNodes) {
      const plain = decryptBodyText(node.bodyText ?? "");
      if (plain !== node.title) {
        push(
          "body_text_not_title_mirror",
          node,
          `body_text 復号平文が title と不一致 (len=${plain.length})`,
        );
        continue;
      }
      if (/[\r\n]/.test(plain)) {
        push("body_text_not_title_mirror", node, "body_text に改行が含まれる");
        continue;
      }
      if (plain.length > MAX_BODY_TEXT_LENGTH) {
        push(
          "body_text_not_title_mirror",
          node,
          `body_text が ${MAX_BODY_TEXT_LENGTH} 字を超過 (len=${plain.length})`,
        );
      }
    }
  }

  return violations;
}

async function main() {
  loadEnvFiles();
  const [{ db }, { knowledgeNodes }, { decryptNodeBodyText, decryptNodeBodyJson }] = await Promise.all([
    import("@/db"),
    import("@/db/schema"),
    import("@/lib/server/knowledge-docs-utils"),
  ]);

  const allNodes = await db
    .select()
    .from(knowledgeNodes)
    .where(isNull(knowledgeNodes.archivedAt))
    .orderBy(asc(knowledgeNodes.title));

  const checkNodes: CheckNode[] = allNodes.map((node) => ({
    id: node.id,
    parentId: node.parentId,
    nodeType: node.nodeType,
    title: node.title,
    bodyText: node.bodyText,
  }));

  const violations = collectProjectInformationViolations(checkNodes, (value) =>
    decryptNodeBodyText(value ?? ""),
  );

  const roots = checkNodes.filter(
    (node) => node.parentId === null && !NON_ROOT_NODE_TYPES.has(node.nodeType ?? "node"),
  );
  const rootTitleById = new Map(roots.map((root) => [root.id, root.title]));

  // 参考情報（従来出力互換）。
  const projectInfoRoots = allNodes.filter((node) => {
    const bodyJson = decryptNodeBodyJson(node.bodyJson ?? {});
    return bodyJson.format === "project_information_doc_block";
  });

  const byKind = new Map<Violation["kind"], number>();
  for (const violation of violations) {
    byKind.set(violation.kind, (byKind.get(violation.kind) ?? 0) + 1);
  }

  console.log(`PROJECT_INFO_ROOTS_BY_NODE_TYPE=${roots.length}`);
  console.log(`PROJECT_INFO_ROOTS_BY_FORMAT=${projectInfoRoots.length}`);
  console.log(`MARKDOWN_HEADING_VIOLATIONS=${byKind.get("markdown_heading") ?? 0}`);
  console.log(`SIBLING_DUPLICATE_VIOLATIONS=${byKind.get("sibling_duplicate") ?? 0}`);
  console.log(`ROOT_NESTED_DUPLICATE_VIOLATIONS=${byKind.get("root_nested_duplicate") ?? 0}`);
  console.log(`BODY_TEXT_TITLE_MIRROR_VIOLATIONS=${byKind.get("body_text_not_title_mirror") ?? 0}`);
  console.log(`TOTAL_VIOLATIONS=${violations.length}`);

  if (violations.length > 0) {
    console.error("案件情報 Docs 検証に失敗しました。以下の違反を解消してください:");
    for (const violation of violations.slice(0, 100)) {
      const rootTitle = rootTitleById.get(violation.rootId) ?? violation.rootTitle;
      console.error(
        `- [${violation.kind}] root="${rootTitle}" node=${violation.nodeId} title="${violation.title}" :: ${violation.detail}`,
      );
    }
    if (violations.length > 100) {
      console.error(`... 他 ${violations.length - 100} 件`);
    }
    process.exit(1);
  }

  console.log("案件情報 Docs 検証: 違反なし。");
}

function isDirectRun() {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return resolve(entry) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
}

if (isDirectRun()) {
  main()
    .then(() => process.exit(0))
    .catch((error) => {
      console.error(error);
      process.exit(1);
    });
}
