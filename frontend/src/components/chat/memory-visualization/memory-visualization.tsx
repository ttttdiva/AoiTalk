"use client";

// Display adapted from adopted v25. The demo fixture/timer/query dispatcher is
// intentionally absent; the caller supplies existing chat and real observations.
import { useEffect, useLayoutEffect, useReducer, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Check, X } from "lucide-react";
import type { ConversationMessage, MemorySelectionItem, MemoryToolProjection, MemoryToolReference } from "@/lib/chat-api";
import { withBasePath } from "@/lib/base-path";
import { memoryApi } from "@/lib/ecc-api";
import type { MemorySelectionObservation, MemorySelectionStage } from "@/lib/memory-selection-state";
import { memoryVisualReducer, newMemoryVisualRun } from "@/lib/memory-visual-run";
import { memoryTurnReply } from "@/lib/memory-turn-reply";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { AdoptedAvatar } from "./adopted-avatar";
import { LightParticle, SidebarFlights } from "./memory-motion";
import "./memory-lab.css";

const STAGES: MemorySelectionStage[] = ["scope_selection", "relevance_decision", "context_budget"];
const LABELS = ["1. 候補の選択", "2. Jev の関連判断", "3. 文脈への採用"];
const STATUS = { running: "処理中", completed: "完了", disabled: "無効", skipped: "未実行", blocked: "ブロック", failed: "失敗", cancelled: "停止" };
const SCOPE = { global: "共通", user: "ユーザー", project: "プロジェクト", task: "タスク", session: "この会話" };

type Props = {
  items: MemorySelectionItem[];
  stages: Partial<Record<MemorySelectionStage, MemorySelectionObservation>>;
  history: readonly MemorySelectionObservation[]; liveObserved: boolean; agentRunId: string | null;
  toolRecall: MemoryToolProjection | null; liveToolObserved: boolean;
  recheckToolReference: (key: string, signal?: AbortSignal) => Promise<MemoryToolReference | null>;
  status: "loading" | "available" | "unavailable" | "error"; itemsPending: boolean;
  generationKey: string | null; busy: boolean; terminalPhase: string;
  view: "graph" | "process"; onViewChange: (view: "graph" | "process") => void;
  messages: ConversationMessage[]; streamingContent: string; activityMessage: string | null;
  pastSearchStatus: string; characterName: string;
  composer: ReactNode; conversationHistory: ReactNode;
};

function useReducedMotion() {
  const [reduced, setReduced] = useState(true);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return reduced;
}

function scores(item: MemorySelectionItem) {
  return [item.weighted_score != null ? `選択重み ${item.weighted_score.toFixed(3)}` : null,
    item.semantic_score != null ? `EG2 類似度 ${item.semantic_score.toFixed(3)}` : null,
    item.jev_relevance != null ? `Jev 関連度 ${item.jev_relevance.toFixed(2)}` : null].filter(Boolean).join(" · ");
}

const TOOL_STATUS = { completed: "完了", partial: "一部取得", failed: "失敗", unavailable: "結果形式を確認できません" };
function toolScore(item: MemoryToolReference) {
  return item.score == null ? "スコアの観測なし" : `${item.score_kind === "semantic_relevance" ? "意味検索" : "記憶検索"} score ${item.score.toFixed(3)}`;
}

function ToolCards({ items, onOpen }: { items: MemoryToolReference[]; onOpen: (item: MemoryToolReference) => void }) {
  return <>{items.map(item => <button key={item.ref_key} data-thought-id={item.ref_key} className="thought-card used" onClick={() => onOpen(item)}>
    <span className="thought-title">{item.title}<b>検索で取得</b></span>
    <small>{item.kind === "conversation" ? "過去会話" : `記憶原本 · v${item.version}`} · {toolScore(item)}</small>
    <p className="memory-tool-excerpt">{item.excerpt}</p>
  </button>)}</>;
}

function Graph({ items, stages, complete, drawing, onOpen, toolItems, onOpenTool }: {
  items: MemorySelectionItem[]; stages: Props["stages"]; complete: boolean; drawing: boolean;
  onOpen: (item: MemorySelectionItem) => void;
  toolItems?: MemoryToolReference[]; onOpenTool?: (item: MemoryToolReference) => void;
}) {
  const scroll = useRef<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState({ width: 1450, height: 500 });
  useLayoutEffect(() => {
    const host = scroll.current;
    if (!host) return;
    const measure = () => {
      const width = host.querySelector("svg")?.getBoundingClientRect().width || host.clientWidth || 1450;
      const height = host.clientHeight || 500;
      setViewport(previous => previous.width === width && previous.height === height ? previous : { width, height });
    };
    measure();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(host);
    return () => observer?.disconnect();
  }, []);
  const entries = toolItems ? toolItems.map((item, index) => ({ key: item.ref_key, title: item.title, rank: index + 1,
    selected: true, scope: item.kind === "conversation" ? "過去会話" : "記憶", action: "検索で取得", open: () => onOpenTool?.(item) }))
    : items.map(item => ({ key: item.memory_id, title: item.title, rank: item.candidate_rank,
      selected: !complete || item.selected, scope: SCOPE[item.scope], action: complete ? item.selected ? "文脈に採用" : "採用せず" : "判断を確認中", open: () => onOpen(item) }));
  // Keep the adopted column widths and type readable. Only the graph's vertical
  // geometry adapts; a dense real result set scrolls within this surface.
  const scale = viewport.width / 1450;
  const rowStart = Math.max(80, 65 / scale);
  const bottomSpace = Math.max(40, 25 / scale);
  const minimumPitch = Math.max(54, 36 / scale);
  const canvasHeight = Math.max(180, Math.floor(viewport.height / scale), rowStart + bottomSpace + Math.max(0, entries.length - 1) * minimumPitch);
  const originY = canvasHeight / 2;
  const rowPitch = Math.max(minimumPitch, Math.min(Math.max(86, minimumPitch), (canvasHeight - rowStart - bottomSpace) / Math.max(1, entries.length - 1)));
  const nodeFont = Math.max(21, 16 / scale);
  const halfNodeHeight = Math.max(20, 13 / scale);
  const halfOriginal = Math.max(24, 16 / scale);
  const titleLength = Math.min(17, Math.max(6, Math.floor(360 / nodeFont)));
  return <div ref={scroll} className="brain-graph-scroll"><svg className={`brain-graph ${drawing ? "is-drawing" : ""}`} viewBox={`0 0 1450 ${canvasHeight}`} style={{ "--brain-node-font": `${nodeFont}px` } as CSSProperties} role="group" aria-label="今回の送信から候補、採用判断、記憶原本への処理の流れ">
    <defs><filter id="brainGlow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="4" /></filter></defs>
    <rect className="brain-col person" x="0" y="0" width="282" height={canvasHeight} rx="32" />
    <rect className="brain-col object" x="299" y="0" width="501" height={canvasHeight} rx="32" />
    <rect className="brain-col motion" x="817" y="0" width="372" height={canvasHeight} rx="32" />
    <rect className="brain-col feeling" x="1216" y="0" width="234" height={canvasHeight} rx="32" />
    {(toolItems ? ["検索要求", "取得結果", "実検索", "参照元"] : ["問い合わせ", "記憶の候補", "採用判断", "記憶原本"]).map((label, index) => <text key={label} className={`brain-col-title title-${index}`} x={[141, 550, 1008, 1333][index]} y={Math.max(38, 26 / scale)} textAnchor="middle">{label}</text>)}
    {entries.map((item, index) => {
      const y = rowStart + index * rowPitch;
      const selected = item.selected;
      return <g key={item.key}>
        {[[141, originY, 550, y], [550, y, 1008, y], [1008, y, 1333, y]].map(([x, sy, tx, ty], link) => {
          const path = `M${x} ${sy}C${x + 170} ${sy},${tx - 180} ${ty},${tx} ${ty}`;
          return <g key={link} className={`brain-relation ${index % 2 ? "violet" : "pink"} ${complete && !selected ? "subdued" : "lit"}`} style={{ "--draw-delay": `${index * 80 + link * 230}ms`, "--draw-duration": "650ms" } as CSSProperties}>
            <path className="glow" d={path} pathLength="1" /><path className="line" d={path} pathLength="1" />
            {drawing && <LightParticle path={path} delay={index * 80 + link * 230} duration={650} color={index % 2 ? "#b599ec" : "#fa83b4"} />}
          </g>;
        })}
        <g className={`brain-node object main ${complete && !selected ? "" : "lit"}`} transform={`translate(550,${y})`} role="button" tabIndex={0} aria-label={`${item.title}の原本を開く`} onClick={item.open} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); item.open(); } }}>
          <rect x="-208" y={-halfNodeHeight} width="416" height={halfNodeHeight * 2} rx="22" /><text y={nodeFont * .36}>{item.title.length > titleLength ? item.title.slice(0, titleLength) + "…" : item.title}</text>
        </g>
        <g className="brain-node action main lit" transform={`translate(1008,${y})`}><rect x={-Math.min(340, Math.max(252, item.action.length * nodeFont + 28)) / 2} y={-halfNodeHeight} width={Math.min(340, Math.max(252, item.action.length * nodeFont + 28))} height={halfNodeHeight * 2} rx="22" /><text y={nodeFont * .36}>{item.action}</text></g>
        <g className="brain-node feeling main lit" data-flight-anchor={item.key} transform={`translate(1333,${y})`} role="button" tabIndex={0} aria-label={`${item.title}の原本へ`} onClick={item.open} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); item.open(); } }}>
          <title>{item.scope} · {item.title}</title><rect x={-halfOriginal} y={-halfOriginal} width={halfOriginal * 2} height={halfOriginal * 2} rx="13" /><text y={nodeFont * .36}>{item.rank}</text>
        </g>
      </g>;
    })}
    <g className="brain-node person lit" transform={`translate(141,${originY})`}><circle r={Math.max(26, 18 / scale)} fill="#e8abca" stroke="white" strokeWidth="4" /><text y={nodeFont * .36}>…</text><text y={Math.max(48, 38 / scale)}>{toolItems ? "実行済み検索" : "今回の送信"}</text></g>
    {!entries.length && <text className="memory-empty-graph" x="855" y={originY} textAnchor="middle">{!toolItems && stages.scope_selection?.status === "running" ? "候補を確認しています" : "このターンの表示可能な原本はありません"}</text>}
  </svg></div>;
}

function RankGroup({ items, stage, decided, onOpen, title }: {
  items: MemorySelectionItem[]; stage: number; decided: boolean;
  onOpen: (item: MemorySelectionItem) => void; title: string;
}) {
  // Preserve the actual backend rank; never calculate a display-only score.
  const current = stage === 2 && decided ? [...items].sort((a, b) => Number(b.selected) - Number(a.selected) || a.candidate_rank - b.candidate_rank) : items;
  return <section className={`rank-group rank-motion-${stage}`}><h3>{title} <span>（{items.length}件表示）</span></h3>
    <div className="rank-rows"><svg className="rank-curves" viewBox={`0 0 87 ${Math.max(1, items.length) * 57}`} style={{ height: `calc(${Math.max(1, items.length)} * var(--rank-row-height))` }} preserveAspectRatio="none" aria-hidden="true">{current.map((item, index) => {
      const before = items.findIndex(row => row.memory_id === item.memory_id);
      return <g key={item.memory_id}><text x="9" y={before * 57 + 31}>{item.candidate_rank}</text><path className={before === index ? "same" : before > index ? "up" : "down"} d={`M24 ${before * 57 + 24}C60 ${before * 57 + 24},48 ${index * 57 + 24},84 ${index * 57 + 24}`} /></g>;
    })}</svg><div className="rank-items" style={{ height: `calc(${Math.max(1, current.length)} * var(--rank-row-height))` }}>{current.map((item, index) => <button key={item.memory_id} className={`rank-row ${decided && !item.selected ? "muted" : ""}`} style={{ "--row-index": index } as CSSProperties} onClick={() => onOpen(item)}>
      <b>{item.title}</b><small>{scores(item)}{decided ? ` · ${item.selected ? "文脈に採用" : "採用せず"}` : ""}</small>
    </button>)}</div></div>
  </section>;
}

export function MemoryVisualization(props: Props) {
  const { items, stages, busy, generationKey, view, onViewChange, recheckToolReference } = props;
  const reduced = useReducedMotion();
  const root = useRef<HTMLElement | null>(null);
  const [paneOpen, setPaneOpen] = useState(true);
  const [manualFamily, setManualFamily] = useState<"automatic" | "tool" | null>(null);
  const [run, dispatchVisual] = useReducer(memoryVisualReducer, generationKey, newMemoryVisualRun);
  const [detail, setDetail] = useState<MemorySelectionItem | MemoryToolReference | null>(null);
  const [original, setOriginal] = useState<{ key: string; content?: string; error?: string; href?: string } | null>(null);
  const family = manualFamily ?? (props.toolRecall?.calls.length ? "tool" : "automatic");
  const toolItems = props.toolRecall?.items ?? [];
  const motionItemCount = family === "tool" ? toolItems.length : items.length;
  const detailKey = detail ? "memory_id" in detail ? detail.memory_id : detail.ref_key : null;
  const observedStage = stages.context_budget ? 2 : stages.relevance_decision ? 1 : 0;
  const stopping = props.terminalPhase === "stopping" || props.terminalPhase === "cancellation_pending";
  const cancelled = stopping || props.terminalPhase === "cancelled" || props.terminalPhase === "failed";
  const interruptionText = stopping ? "生成の停止を待っています" : props.terminalPhase === "failed" ? "生成が失敗しました" : "生成を停止しました";
  const motionReadiness = props.itemsPending ? "pending" : items.length ? "ready" : "skip";
  useEffect(() => {
    dispatchVisual({ type: "observe", key: generationKey, history: props.history,
      live: props.liveObserved && !reduced && !cancelled, readiness: motionReadiness,
      interrupt: reduced || cancelled });
  }, [generationKey, props.history, props.liveObserved, motionReadiness, reduced, cancelled]);
  useEffect(() => {
    if (!generationKey || run.key !== generationKey || run.phase === "idle" || run.manual || reduced || cancelled || detail) return;
    // Allow the adopted light/flight geometry to finish; this queue never gates
    // the answer stream or changes an observed processing status.
    const lastItem = Math.max(0, motionItemCount - 1);
    const duration = run.phase === "connecting" ? 1110 + lastItem * 80
      : run.phase === "collecting" ? 920 + lastItem * 140
      : run.phase === "rank" ? 1150 : 850;
    const timer = setTimeout(() => dispatchVisual({ type: "elapsed", key: generationKey }), duration);
    return () => clearTimeout(timer);
  }, [generationKey, run.key, run.phase, run.stage, run.manual, run.observations, reduced, cancelled, detail, motionItemCount]);
  const effectiveVisual = run.key !== generationKey || reduced || cancelled || detail ? "idle" : run.phase;
  const stage = run.manual || effectiveVisual === "rank" ? run.stage : observedStage;
  const effectiveView = run.manual ? view : effectiveVisual === "rank" ? "process" : effectiveVisual === "responding" || run.played ? "graph" : view;
  // A WS completion can arrive before its refreshed candidate projection.
  // Partial candidates have selected=false until the actual decision is read.
  const complete = stages.context_budget?.status === "completed" && !props.itemsPending;
  const boundReply = memoryTurnReply({ messages: props.messages, streamingContent: props.streamingContent, agentRunId: props.agentRunId, busy });
  const reply = boundReply || (cancelled ? interruptionText : busy ? props.activityMessage || "回答を準備しています" : "メッセージを送ると、この会話で参照した記憶を表示します。");
  const currentObservation = stages[STAGES[observedStage]];
  const selectionText = cancelled ? interruptionText : currentObservation
    ? `${LABELS[observedStage]} · ${STATUS[currentObservation.status]}` : busy ? "このターンの観測を待っています" : "このターンの観測なし";
  useEffect(() => {
    if (!detail) return;
    let active = true;
    const controller = new AbortController();
    const read = async () => {
      let identifier: string, version: number | null, scope: string | null = null;
      if ("memory_id" in detail) {
        identifier = detail.memory_id; version = detail.version; scope = detail.scope;
      } else {
        const fresh = await recheckToolReference(detail.ref_key, controller.signal);
        if (!fresh) throw new Error("source unavailable");
        if (fresh.kind === "conversation") return { key: detail.ref_key, content: fresh.excerpt, href: fresh.href ?? undefined };
        identifier = fresh.source_id; version = fresh.version ?? null;
      }
      const result = await memoryApi.get(identifier);
      const memory = result.memory;
      const expiry = memory.expires_at ? Date.parse(memory.expires_at) : Infinity;
      const valid = memory.status === "active" && memory.version === version && (!scope || memory.scope_type === scope) && expiry > Date.now();
      return valid ? { key: detailKey!, content: memory.content } : { key: detailKey!, error: "記憶の版または有効状態が変わりました。原本を表示できません。" };
    };
    void read().then(result => {
      if (!active) return;
      setOriginal(result);
    }).catch(() => { if (active) setOriginal({ key: detailKey!, error: "現在の権限で原本を読み込めませんでした。" }); });
    return () => { active = false; controller.abort(); };
  }, [detail, detailKey, recheckToolReference]);
  const open = (item: MemorySelectionItem) => { dispatchVisual({ type: "manual", key: generationKey }); setDetail(item); };
  const openTool = (item: MemoryToolReference) => { dispatchVisual({ type: "manual", key: generationKey }); setDetail(item); };
  const changeView = (next: "graph" | "process") => { dispatchVisual({ type: "manual", key: generationKey, stage: observedStage }); onViewChange(next); };
  return <section ref={root} className={`memory-reference ${reduced ? "reduce-memory-motion" : ""}`} data-run-phase={effectiveVisual} aria-label="記憶可視化チャット">
    <div className="brain-layout"><div className="brain-main"><header className="brain-header"><h1>この会話の記憶</h1><div>
      <span>{family === "automatic" ? "自動記憶" : "実行済みの過去会話検索"} · 最大8件表示</span><span className="brain-proposal">実処理の観測</span>
      <span className={`brain-run-status ${busy ? "active" : ""}`} role="status" aria-live="polite">{family === "automatic" ? selectionText : props.toolRecall?.calls.map(call => `${call.mode === "semantic" ? "意味検索" : "語句検索"} · ${TOOL_STATUS[call.status]}`).join(" / ") || "このターンでは検索の観測なし"}</span>
    </div><nav aria-label="記憶表示"><button className={effectiveView === "graph" ? "active" : ""} onClick={() => changeView("graph")}>関係図</button><button className={effectiveView === "process" ? "active" : ""} onClick={() => changeView("process")}>採用のようす</button></nav></header>
      <div className="memory-source-tabs" role="group" aria-label="記憶の取得経路"><button aria-pressed={family === "automatic"} onClick={() => setManualFamily("automatic")}>自動記憶</button><button aria-pressed={family === "tool"} onClick={() => setManualFamily("tool")}>実検索</button></div>
      <div className="brain-stage run-stage">{effectiveView === "graph" ? <div className="brain-map-surface" key="graph"><Graph items={items} stages={stages} complete={complete} drawing={family === "automatic" ? effectiveVisual === "connecting" : props.liveToolObserved && !reduced && !cancelled} onOpen={open} toolItems={family === "tool" ? toolItems : undefined} onOpenTool={openTool} /></div> : family === "tool" ? <div className="brain-ranking">
        <div className="rank-toolbar"><h2>実検索の返却順</h2><div className="rank-actions"><button onClick={() => changeView("graph")}>とじる</button></div></div>
        <p className="memory-stage-note">観測された検索結果の順番と実スコアです。</p><ToolCards items={toolItems} onOpen={openTool} />
      </div> : <div className="brain-ranking" key="rank">
        <div className="rank-toolbar"><h2>採用のようす</h2><div className="rank-tabs" role="tablist" aria-label="記憶選択の段階">{LABELS.map((label, index) => {
          const observation = stages[STAGES[index]];
          return <button key={label} role="tab" aria-selected={stage === index} className={stage === index ? "active" : ""} onClick={() => { dispatchVisual({ type: "manual", key: generationKey, stage: index }); onViewChange("process"); }}>
            {label}<span className={`rank-stage-state ${observation?.status === "running" ? "stage-working" : observation?.status === "completed" ? "stage-done" : observation ? "stage-observed" : "stage-waiting"}`} aria-label={observation ? STATUS[observation.status] : "未観測"}>{observation?.status === "completed" ? <Check size={12} /> : observation?.status === "running" ? <i /> : observation ? <span className="memory-stage-label">{STATUS[observation.status]}</span> : null}</span>
          </button>;
        })}</div><div className="rank-actions"><button onClick={() => changeView("graph")}>とじる</button></div></div>
        <p className="memory-stage-note">{stage === 1 ? stages.relevance_decision?.engine === "jev" && stages.relevance_decision.status === "completed" ? "実行されたJevの関連度と保護条件による採用順です。" : "Jevの判断は未完了です。候補はホストの条件で選択されます。" : stage === 2 ? "実際に文脈へ採用した記憶を上へまとめます。" : stages.scope_selection?.engine === "eg2" ? "現在の権限で取得したEG2意味検索の候補です。" : "現在の権限で取得した語句検索と保護対象の候補です。"}</p>
        <div className="rank-groups"><RankGroup title="ユーザー・共通" items={items.filter(item => item.scope === "user" || item.scope === "global")} stage={stage} decided={complete} onOpen={open} /><RankGroup title="プロジェクト・会話" items={items.filter(item => item.scope !== "user" && item.scope !== "global")} stage={stage} decided={complete} onOpen={open} /></div>
      </div>}</div>
      <div className={`brain-conversation ${busy ? "is-thinking" : ""}`}><div className="brain-avatar"><AdoptedAvatar /><span className="avatar-thinking" aria-hidden="true">✧</span></div><div className="brain-talk">
        <div className="brain-latest"><span>{props.characterName}の回答</span><p className={boundReply ? "brain-reply" : ""} data-response={Boolean(boundReply).toString()}>{reply}{busy && !props.streamingContent && <span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span>}</p><i aria-hidden="true">✧　☆　✧</i></div>
        <div className="brain-chat-lines">{props.messages.filter(message => message.role !== "system" && message.is_active_branch !== false).slice(-3).map(message => <p key={message.id} className={message.role === "user" ? "current" : ""}><b>{message.sender_display_name || (message.role === "user" ? "あなた" : props.characterName)}</b>{message.content}</p>)}</div>
        <div className="memory-composer-slot">{props.composer}</div>
      </div></div>
      <details className="memory-history"><summary>会話履歴・編集・再送を開く</summary><div>{props.conversationHistory}</div></details>
    </div><aside className={`brain-memories ${effectiveVisual === "collecting" ? "receiving" : ""}`}><div className="brain-memories-title"><h2>思い出したこと</h2><button onClick={() => { dispatchVisual({ type: "manual", key: generationKey }); setPaneOpen(value => !value); }} aria-expanded={paneOpen}>{paneOpen ? "とじる" : "ひらく"}</button></div>{paneOpen && <>
      <p className="brain-query">{props.status === "error" ? "記憶の観測を読み込めませんでした。" : props.status === "loading" ? "このターンの記憶を確認しています。" : (family === "tool" ? toolItems : items).length ? `${(family === "tool" ? toolItems : items).length}件の原本を現在の権限で確認しました。` : "このターンの表示可能な原本はありません。"}</p>
      <p className="memory-search-status">{props.pastSearchStatus}</p><button className="brain-rank-link" onClick={() => changeView("process")}>{family === "automatic" ? "採用のようすを見る" : "検索の返却順を見る"}</button>
      {family === "tool" ? <section className="thought-group"><h3>実際の検索結果</h3><ToolCards items={toolItems} onOpen={openTool} /></section> : ([true, false] as const).map(personal => <section className="thought-group" key={String(personal)}><h3>{personal ? "ユーザー・共通" : "プロジェクト・会話"}</h3>{items.filter(item => (item.scope === "user" || item.scope === "global") === personal).map((item, index) => <button key={item.memory_id} data-thought-id={item.memory_id} style={{ "--receive-delay": `${index * 140}ms` } as CSSProperties} className={`thought-card ${complete && !item.selected ? "quiet" : "used"}`} onClick={() => open(item)}>
        <span className="thought-title">{item.title}<b>{complete ? item.selected ? "文脈に採用" : "採用せず" : "参照候補"}</b></span><small>{SCOPE[item.scope]} · v{item.version}{scores(item) ? ` · ${scores(item)}` : ""}</small>
        {item.weighted_score != null && <i aria-label={`実際の選択重み ${item.weighted_score}`}><span style={{ width: `${item.weighted_score * 100}%` }} /></i>}
      </button>)}</section>)}
    </>}</aside></div>
    {effectiveVisual === "collecting" && paneOpen && !reduced && generationKey && <SidebarFlights key={generationKey} generationKey={generationKey} root={root} ids={family === "automatic" ? items.map(item => item.memory_id) : toolItems.map(item => item.ref_key)} />}
    <Dialog open={Boolean(detail)} onOpenChange={isOpen => { if (!isOpen) setDetail(null); }}><DialogContent className="memory-original-dialog" size="xl"><DialogTitle>{detail?.title || "記憶原本"}</DialogTitle>
      <p>{detail && original?.key === detailKey ? original.error || original.content : "原本を読み込み中…"}</p>
      {original?.key === detailKey && original.href && <a href={withBasePath(original.href)} target="_blank" rel="noopener noreferrer">元の会話を開く</a>}
      <small>{detail ? "memory_id" in detail ? `${SCOPE[detail.scope]} · v${detail.version} · 記憶原本` : `${detail.kind === "conversation" ? "過去会話の原本" : "記憶原本"} · ${toolScore(detail)}` : ""}</small>
      <button type="button" onClick={() => setDetail(null)}><X size={16} /> とじる</button>
    </DialogContent></Dialog>
  </section>;
}
