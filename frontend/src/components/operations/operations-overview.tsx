"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  ArrowUpRight,
  ChevronRight,
  FileText,
  GitBranch,
  RefreshCw,
} from "lucide-react";
import {
  costSummary,
  decisionHref,
  formatMoney,
  hasRunningRecord,
  hasTechnicalBlocker,
  kindLabel,
  scopeFromQuery,
  sourceHref,
  usageDetailHref,
  statusLabel,
  workKey,
  type CanonicalWork,
  type WorkScope,
} from "@/lib/operations-work-projection";
import { AppSelect } from "@/components/ui/app-select";
import { useWorkProjection } from "./use-work-projection";
import { MediaPortfolioSummary } from "./media/media-portfolio-summary";
import "./operations-overview.css";

type Props = { query: string; onNavigate: (href: string) => void };
const title = (work: CanonicalWork) => work.title?.trim() || "名称未記録";
const timestamp = (value?: string | null) =>
  value ? value.replace("T", " ").replace(/\.\d+Z$/, "Z") : "未記録";
const shortId = (value: string) =>
  value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;

export function OperationsOverview({ query, onNavigate }: Props) {
  const params = useMemo(() => new URLSearchParams(query), [query]);
  const scope = scopeFromQuery(params);
  // Changing URL scope must remove all previous titles, decisions and previews before effects run.
  return (
    <OverviewView
      key={JSON.stringify(scope)}
      scope={scope}
      params={params}
      onNavigate={onNavigate}
    />
  );
}

function OverviewView({
  scope,
  params,
  onNavigate,
}: {
  scope: WorkScope;
  params: URLSearchParams;
  onNavigate: Props["onNavigate"];
}) {
  const { data, loading, paging, error, stale, refresh, loadMore } =
    useWorkProjection(scope);
  const works = data?.items ?? [];
  const requested = params.get("overview_work");
  const selected = requested
    ? works.find((work) => workKey(work) === requested)
    : works[0];
  const [decisionKey, setDecisionKey] = useState("");
  const [shelf, setShelf] = useState<"artifacts" | "history" | "cost">(
    "artifacts",
  );
  const decisions = works.flatMap((work) =>
    work.decisions.map((decision) => ({
      work,
      decision,
      key: `${workKey(work)}:${decision.kind}:${decision.id}`,
    })),
  );
  const decision =
    decisions.find((value) => value.key === decisionKey) ?? decisions[0];
  const cost = costSummary(works);
  const selectionCost = selected ? costSummary([selected]) : null;
  const unobservedWork = works.filter(
    (work) =>
      !work.attempts.length ||
      work.gaps.some((gap) =>
        /attempts_truncated|runs_truncated|work_ledger_unavailable|work_source_link_unresolved|content_attempt_links_partial|current_run_link_missing|runtime_state_unobserved/.test(
          gap,
        ),
      ) ||
      work.attempts.some(
        (attempt) =>
          attempt.current_run_status === "unknown" ||
          attempt.linkage !== "recorded",
      ),
  ).length;
  const decisionUnknown = works.filter((work) =>
    work.gaps.some((gap) =>
      /action.*(unavailable|partial|ambiguous)|artifact.*(unavailable|partial)/.test(
        gap,
      ),
    ),
  ).length;
  const completedPage = data && !data.page.has_more;
  const isFresh = Boolean(data) && !stale && !loading && !paging;

  function navigate(updates: Record<string, string | null>) {
    const next = new URLSearchParams(params);
    next.set("tab", "overview");
    Object.entries(updates).forEach(([key, value]) =>
      value === null ? next.delete(key) : next.set(key, value),
    );
    onNavigate(`/operations?${next}`);
  }
  const mapQuery = new URLSearchParams(params);
  mapQuery.set("tab", "overview");
  mapQuery.set("overview_view", "map");
  mapQuery.delete("overview_work");

  return (
    <section
      className="operations-live"
      aria-label="Operations Overview"
      data-testid="operations-live-overview"
    >
      <header className="ol-top">
        <div className="ol-title">
          <span className="ol-mark" aria-hidden="true">
            AT
          </span>
          <div>
            <h1>Operations</h1>
            <p>WORK / PEOPLE / OUTPUT</p>
          </div>
        </div>
        <label className="ol-kind">
          表示対象
          <AppSelect
            contentClassName="ol-select-popup"
            aria-label="仕事の種類"
            value={scope.kind ?? "all"}
            onChange={(event) =>
              navigate({
                kind: event.target.value === "all" ? null : event.target.value,
                overview_work: null,
              })
            }
          >
            <option value="all">すべて</option>
            <option value="task">Tasks</option>
            <option value="opportunity">Opportunities</option>
            <option value="content_item">Content</option>
          </AppSelect>
        </label>
        <Link href={`/operations?${mapQuery}`} className="ol-map">
          <GitBranch size={17} /> Map
        </Link>
        <button
          type="button"
          className="ol-refresh"
          onClick={refresh}
          disabled={loading}
          aria-label="仕事の情報を更新"
        >
          <RefreshCw size={17} className={loading ? "ol-spinning" : ""} /> 更新
        </button>
      </header>
      <div className="ol-body">
        <div className="ol-media"><MediaPortfolioSummary compact /></div>
        <div className="ol-observation" role="status">
          <span>
            {paging
              ? "追加取得中 · 前回の取得記録"
              : loading
                ? data
                  ? "更新中 · 前回の取得記録"
                  : "読み込み中…"
                : stale
                  ? "更新失敗 · 前回の取得記録"
                  : data
                    ? `取得済み ${works.length} / ${data.page.available}件${completedPage ? "" : " · 一部を表示"}`
                    : "未取得"}
          </span>
          <span>
            {scope.projectId
              ? `Project ${shortId(scope.projectId)}`
              : scope.spaceId
                ? `Space ${shortId(scope.spaceId)}`
                : "権限内のすべての仕事"}
            {data ? ` · ${timestamp(data.observed_at)}` : ""}
          </span>
        </div>
        {error ? (
          <div role="alert" className="ol-alert">
            {error}
            <button type="button" onClick={refresh} disabled={loading}>
              再試行
            </button>
          </div>
        ) : null}
        <div className="ol-stats" aria-label="取得済みの仕事の集計">
          <Stat
            label="RUNNING / 実行中の記録"
            value={data ? works.filter(hasRunningRecord).length : "—"}
            tone="green"
            note={`取得時点のcurrent run · 未確認 ${unobservedWork}件`}
          />
          <Stat
            label="HUMAN DECISIONS / 本人判断"
            value={data ? decisions.length : "—"}
            tone="gold"
            note={`判断権限内の記録${decisionUnknown ? ` · 未確認 ${decisionUnknown}件` : ""}`}
          />
          <Stat
            label="BLOCKERS / 技術的障害"
            value={data ? works.filter(hasTechnicalBlocker).length : "—"}
            tone="coral"
            note={`記録された停止 · 実行未確認 ${unobservedWork}件`}
          />
          <Stat
            label="ACTUAL COST / 計測済み実費"
            value={data ? formatMoney(cost.actual) : "—"}
            tone="green"
            note={
              data
                ? `未計測・網羅未確認 ${cost.unknown}件 · 取得済み小計`
                : "未取得を0として扱いません"
            }
          />
        </div>
        {selected ? (
          <section className="ol-summary" aria-label="選択中の仕事の記録と費用の要約">
            <div className="ol-summary-records">
              <span className="ol-summary-label">選択中の記録 · {kindLabel(selected.ref.kind)} {shortId(selected.ref.id)}</span>
              <strong>成果物 {selected.artifacts.length}件 · Run {selected.runs.length}件</strong>
              <span>取得済みの保存記録{stale || loading ? " · 前回の取得内容" : ""}</span>
            </div>
            <div className="ol-summary-cost">
              <span className="ol-summary-label">この仕事の実費</span>
              <strong>{formatMoney(selectionCost!.actual)}</strong>
              <span>{selectionCost!.unknown ? "未計測・未確認あり" : "取得済み小計"}</span>
            </div>
            <div className="ol-summary-phase">
              <span className="ol-summary-label">最後に記録された工程</span>
              <strong>{selected.last_recorded_phase || "未記録"}</strong>
              <span>{timestamp(selected.last_recorded_phase_at)}</span>
            </div>
          </section>
        ) : null}
        {!loading && data && !works.length ? (
          <div className="ol-empty" role="status">
            <FileText size={28} />
            <h2>この条件で表示できる仕事はありません</h2>
            <p>条件と閲覧権限の範囲内を取得しました。</p>
          </div>
        ) : null}
        {requested && data && !selected ? (
          <div className="ol-alert" role="status">
            選択した仕事はこの条件・権限内の
            {data.page.has_more ? "取得済み一覧" : "一覧"}にありません。
            {data.page.has_more
              ? "続きを取得するか、別の仕事を選択してください。"
              : "別の仕事を選択してください。"}
          </div>
        ) : null}
        {works.length ? (
          <div className="ol-main">
            <aside className="ol-panel ol-decision">
              <h2 className="ol-section-title">
                <span>◆ あなたの判断</span>
                <small>{decisions.length}件</small>
              </h2>
              {decision ? (
                <>
                  <label className="ol-sr-only" htmlFor="ol-decision-select">
                    確認する判断
                  </label>
                  <AppSelect
                    contentClassName="ol-select-popup"
                    id="ol-decision-select"
                    className="ol-decision-select"
                    value={decision.key}
                    onChange={(event) => setDecisionKey(event.target.value)}
                  >
                    {decisions.map((value, index) => (
                      <option key={value.key} value={value.key}>
                        {String(index + 1).padStart(2, "0")}{" "}
                        {value.decision.kind === "external_action"
                          ? "外部操作"
                          : "成果物レビュー"}{" "}
                        · {title(value.work)}
                      </option>
                    ))}
                  </AppSelect>
                  <h3>{title(decision.work)}</h3>
                  <div
                    className="ol-document"
                    aria-label="判断対象の保存済み参照"
                  >
                    <div className="ol-document-head">
                      <FileText size={22} />
                      <span>{kindLabel(decision.work.ref.kind)} / RECORD</span>
                    </div>
                    <strong>
                      {decision.decision.kind === "external_action"
                        ? "外部操作の内容を確認"
                        : "保存済み成果物を確認"}
                    </strong>
                    <p>
                      {decision.decision.version === null
                        ? "版は未記録"
                        : `記録された版 v${decision.decision.version}`}
                    </p>
                    <code>{shortId(decision.decision.id)}</code>
                  </div>
                  <p className="ol-decision-copy">
                    {decision.decision.kind === "external_action"
                      ? "送信先・内容・影響を、既存の承認画面で確認します。"
                      : "成果物とレビュー記録を、元の仕事で確認します。"}
                  </p>
                  <dl className="ol-meta">
                    <dt>対象</dt>
                    <dd>
                      {kindLabel(decision.work.ref.kind)} ·{" "}
                      {shortId(decision.work.ref.id)}
                    </dd>
                    <dt>状態</dt>
                    <dd>{statusLabel(decision.work.business_status)}</dd>
                    <dt>部門</dt>
                    <dd>{decision.work.department || "未分類"}</dd>
                  </dl>
                  {isFresh ? (
                    <Link
                      className="ol-primary"
                      href={decisionHref(decision.work, decision.decision)}
                    >
                      内容と影響を確認
                      <ChevronRight size={20} />
                    </Link>
                  ) : (
                    <p className="ol-disabled-note">
                      最新の記録を取得してから判断画面を開いてください。
                    </p>
                  )}
                  <p className="ol-note">
                    この画面では承認・送信を実行しません。確認先の権限チェックが適用されます。
                  </p>
                </>
              ) : (
                <div className="ol-no-decision">
                  <span className="ol-empty-symbol">◇</span>
                  <h3>
                    {decisionUnknown
                      ? "判断の有無は未確認です"
                      : "取得済みの判断記録はありません"}
                  </h3>
                  <p>
                    取得済み・判断権限内の記録です。技術的な問題は仕事の状況から確認できます。
                  </p>
                </div>
              )}
              <div className="ol-decision-footer">
                <span>READ-ONLY PROJECTION</span>
                <p>元のTask・Opportunity・Contentが正本です。</p>
              </div>
            </aside>
            <div className="ol-execution">
              {selected ? (
                <>
                  <section
                    className="ol-panel ol-selected"
                    aria-label="選択中の仕事"
                  >
                    <div>
                      <label htmlFor="ol-work-select" className="ol-eyebrow">
                        SELECTED WORK / 選択中の仕事
                      </label>
                      <AppSelect
                        contentClassName="ol-select-popup"
                        id="ol-work-select"
                        value={workKey(selected)}
                        onChange={(event) =>
                          navigate({ overview_work: event.target.value })
                        }
                      >
                        {works.map((work) => (
                          <option key={workKey(work)} value={workKey(work)}>
                            {title(work)} · {kindLabel(work.ref.kind)}
                          </option>
                        ))}
                      </AppSelect>
                      <p>
                        {kindLabel(selected.ref.kind)} · 部門{" "}
                        {selected.department || "未分類"} ·{" "}
                        <Link href={sourceHref(selected)}>
                          元の仕事を開く <ArrowUpRight size={15} />
                        </Link>
                        {selected.project_id ? (
                          <>
                            <span> · </span>
                            <Link
                              href={`/projects?project_id=${encodeURIComponent(selected.project_id)}`}
                            >
                              Project <ArrowUpRight size={15} />
                            </Link>
                          </>
                        ) : null}
                      </p>
                    </div>
                    <div
                      className={`ol-status ${hasTechnicalBlocker(selected) ? "ol-coral" : hasRunningRecord(selected) ? "ol-green" : "ol-gold"}`}
                    >
                      <strong>
                        {hasRunningRecord(selected)
                          ? "実行中の記録"
                          : statusLabel(selected.business_status)}
                      </strong>
                      <span>
                        {selected.ref.kind === "content_item"
                          ? `公開予定 ${timestamp(selected.content_scheduled_at)}`
                          : `納期 ${timestamp(selected.task_due_at)}`}
                      </span>
                    </div>
                  </section>
                  <section
                    className="ol-panel ol-phases"
                    aria-label="工程と依存"
                  >
                    <div className="ol-panel-heading">
                      <h2>工程と依存</h2>
                      <span>同じ仕事に属する記録</span>
                    </div>
                    {selected.attempts.length ? (
                      <div className="ol-step-list">
                        {selected.attempts.map((attempt, index) => (
                          <div
                            className={`ol-step ${attempt.current_run_status === "running" ? "ol-step-current" : ""}`}
                            key={attempt.id}
                          >
                            <header className="ol-attempt-heading">
                              <h3>
                                担当{" "}
                                {attempt.assigned_agent_display_name ||
                                  "未記録"}
                              </h3>
                              <span className="ol-eyebrow">
                                {attempt.current_run_status === "running"
                                  ? "CURRENT RECORD"
                                  : "RECORDED"}
                              </span>
                            </header>
                            <strong>
                              {attempt.current_phase || "工程名 未記録"}
                            </strong>
                            <span>{statusLabel(attempt.state)}</span>
                            <p>
                              Run{" "}
                              <strong
                                className={
                                  attempt.current_run_status === "running"
                                    ? "ol-green"
                                    : "ol-gold"
                                }
                              >
                                [{statusLabel(attempt.current_run_status)}]
                              </strong>
                            </p>
                            <p className="ol-note">
                              Run{" "}
                              {attempt.current_run_id
                                ? shortId(attempt.current_run_id)
                                : "未記録"}
                            </p>
                            <p className="ol-note">
                              最終応答 {timestamp(attempt.heartbeat_at)}
                            </p>
                            {attempt.deadline ? (
                              <p className="ol-note">
                                実行期限 {timestamp(attempt.deadline)}
                              </p>
                            ) : null}
                            <Link
                              href={`/operations?tab=work&work_id=${encodeURIComponent(attempt.id)}`}
                            >
                              実行 {index + 1}
                              <ArrowUpRight size={14} />
                            </Link>
                            {attempt.parent_work_item_id ? (
                              <small>
                                親実行 {shortId(attempt.parent_work_item_id)}
                              </small>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <>
                        <p className="ol-unrecorded">
                          工程・担当・実行のつながりは未記録です。
                        </p>
                        <p className="ol-note">
                          担当設定と実行開始は別の記録として扱います。
                        </p>
                      </>
                    )}
                    <p className="ol-phase-note">
                      <GitBranch size={18} />{" "}
                      {selected.last_recorded_phase
                        ? `最後に記録された工程: ${selected.last_recorded_phase} · ${timestamp(selected.last_recorded_phase_at)}`
                        : "工程名は未記録。current run と過去runは区別して表示します。"}
                    </p>
                  </section>
                  <section
                    className="ol-panel ol-shelf"
                    aria-label="選択中の仕事の記録"
                  >
                    <div
                      className="ol-shelf-tabs"
                      role="group"
                      aria-label="記録の表示"
                    >
                      {(
                        [
                          ["artifacts", "成果物"],
                          ["history", "実行履歴"],
                          ["cost", "費用の内訳"],
                        ] as const
                      ).map(([id, label]) => (
                        <button
                          key={id}
                          type="button"
                          aria-pressed={shelf === id}
                          onClick={() => setShelf(id)}
                        >
                          {label}
                        </button>
                      ))}
                      <Link href={sourceHref(selected)}>
                        詳細 <ArrowUpRight size={16} />
                      </Link>
                    </div>
                    {shelf === "artifacts" ? (
                      <div className="ol-record-list">
                        {selected.artifacts.length ? (
                          selected.artifacts.map((artifact) => (
                            <div
                              className="ol-artifact"
                              key={`${artifact.kind}:${artifact.id}`}
                            >
                              <FileText size={24} />
                              <div>
                                <strong>
                                  {artifact.kind === "artifact_version"
                                    ? "成果物の版"
                                    : "Contentの版"}
                                </strong>
                                <p>
                                  {shortId(artifact.id)} ·{" "}
                                  {artifact.availability === "available"
                                    ? "参照あり"
                                    : artifact.availability === "unavailable"
                                      ? "利用不可"
                                      : "未確認"}
                                </p>
                              </div>
                              {artifact.availability === "available" ? (
                                <Link href={sourceHref(selected)}>
                                  元の画面で確認
                                  <ArrowUpRight size={16} />
                                </Link>
                              ) : null}
                            </div>
                          ))
                        ) : (
                          <p className="ol-unrecorded">
                            成果物の記録はありません。
                          </p>
                        )}
                      </div>
                    ) : shelf === "history" ? (
                      <ol className="ol-record-list">
                        {selected.runs.length ? (
                          selected.runs.map((run) => (
                            <li key={run.id}>
                              <strong>
                                {statusLabel(run.status)}
                                {run.last_recorded_phase
                                  ? ` · ${run.last_recorded_phase}`
                                  : ""}
                              </strong>
                              <span>
                                {run.agent_display_name || "担当名 未記録"} ·
                                Run {shortId(run.id)}
                              </span>
                              <span>
                                {timestamp(run.last_event_at || run.created_at)}
                              </span>
                            </li>
                          ))
                        ) : (
                          <li>Runの記録はありません。</li>
                        )}
                      </ol>
                    ) : (
                      <dl className="ol-cost-breakdown">
                        <dt>計測済み実費</dt>
                        <dd>{formatMoney(selectionCost!.actual)}</dd>
                        <dt>見積り</dt>
                        <dd>{formatMoney(selectionCost!.estimate)}</dd>
                        <dt>予約額</dt>
                        <dd>{formatMoney(selectionCost!.reservation)}</dd>
                        <dt>網羅性</dt>
                        <dd>
                          {selectionCost!.unknown
                            ? "未計測・未確認あり。合計は未確定。"
                            : "返された費用記録内の小計"}
                        </dd>
                      </dl>
                    )}
                    {shelf === "cost" ? (
                      <ul className="ol-record-list ol-usage-costs" aria-label="費用記録の詳細リンク">
                        {selected.costs.filter((cost) => usageDetailHref(cost)).map((cost) => (
                          <li key={`${cost.usage_id}:${cost.run_id}`}>
                            <Link href={usageDetailHref(cost)!}>
                              <strong>{cost.category === "actual" ? "計測済み実費" : cost.category === "estimate" ? "定価見積り" : "未計測"} · {cost.amount === null ? "未計測" : `${cost.amount} ${cost.currency}`}</strong>
                              <span>Usage {shortId(cost.usage_id!)} · Run {shortId(cost.run_id!)}</span>
                              <span>費用記録を開く <ArrowUpRight size={16} /></span>
                            </Link>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    {selected.gaps.length ? (
                      <details className="ol-gaps">
                        <summary>
                          未記録・未確認の項目 {selected.gaps.length}件
                        </summary>
                        <ul>
                          {selected.gaps.map((gap) => (
                            <li key={gap}>{gap}</li>
                          ))}
                        </ul>
                      </details>
                    ) : null}
                  </section>
                </>
              ) : (
                <section className="ol-panel ol-selection-empty">
                  <h2>仕事を選択してください</h2>
                  <p>別の仕事の詳細は自動で表示しません。</p>
                </section>
              )}
            </div>
            <section
              className="ol-panel ol-work-list"
              aria-label="取得済みの仕事"
            >
              <div className="ol-panel-heading">
                <h2>仕事の状況</h2>
                <span>
                  {works.length}件取得{data?.page.has_more ? " · 続きあり" : ""}
                </span>
              </div>
              <div className="ol-work-rows">
                {works.map((work) => (
                  <button
                    key={workKey(work)}
                    type="button"
                    aria-pressed={
                      selected && workKey(selected) === workKey(work)
                    }
                    onClick={() => navigate({ overview_work: workKey(work) })}
                  >
                    <span className="ol-work-kind">
                      {kindLabel(work.ref.kind)}
                    </span>
                    <strong>{title(work)}</strong>
                    <span>{statusLabel(work.business_status)}</span>
                    <span className="ol-work-cost">
                      {formatMoney(costSummary([work]).actual)}
                    </span>
                    <ChevronRight size={18} />
                  </button>
                ))}
              </div>
            </section>
          </div>
        ) : null}
        {data ? (
          <footer className="ol-footer">
            <span>
              取得済みの範囲のみ集計 · 見積り {formatMoney(cost.estimate)} ·
              予約額 {formatMoney(cost.reservation)}
            </span>
            {data.page.has_more ? (
              <button
                type="button"
                disabled={loading || paging}
                onClick={loadMore}
              >
                {paging ? "取得中…" : "続きを取得"}
              </button>
            ) : (
              <span>この条件のページを取得済み</span>
            )}
          </footer>
        ) : null}
      </div>
    </section>
  );
}
function Stat({
  label,
  value,
  tone,
  note,
}: {
  label: string;
  value: string | number;
  tone: string;
  note: string;
}) {
  return (
    <div className="ol-stat">
      <h2>{label}</h2>
      <strong className={`ol-${tone}`}>{value}</strong>
      <p>{note}</p>
    </div>
  );
}
