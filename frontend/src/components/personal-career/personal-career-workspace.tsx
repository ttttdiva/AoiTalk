"use client";

import { useId, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCurrentUserId } from "@/components/providers/swr-global-provider";
import { PersonalAgentInvocationPanel } from "../personal-finance/personal-agent-invocation-panel";
import {
  isAmbiguousCareerWrite,
  newCareerIdempotencyKey,
  personalCareerApi,
  type CareerAvailableSourceOut,
  type CareerContextOut,
  type CareerProfileVersionOut,
  type CareerSourceOut,
} from "./personal-career-api";

type PendingAction = Readonly<{
  ownerKey: string;
  label: string;
  send: () => Promise<unknown>;
  apply: (result: unknown) => void;
}>;

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "操作を完了できませんでした";
}

function lines(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

export function PersonalCareerWorkspace() {
  const currentUserId = useCurrentUserId();
  const ownerKey = currentUserId?.trim() || "";
  const [available, setAvailable] = useState<CareerAvailableSourceOut[]>([]);
  const [sources, setSources] = useState<CareerSourceOut[]>([]);
  const [versions, setVersions] = useState<CareerProfileVersionOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [reviewVersion, setReviewVersion] = useState<CareerProfileVersionOut | null>(null);
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [acceptedProfileRef, setAcceptedProfileRef] = useState("");
  const [context, setContext] = useState<CareerContextOut | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [skillsText, setSkillsText] = useState("");
  const [preferencesText, setPreferencesText] = useState("");
  const [constraintsText, setConstraintsText] = useState("");
  const [sourceRefInput, setSourceRefInput] = useState("");
  const [sourceHashInput, setSourceHashInput] = useState("");
  const [sourceMimeInput, setSourceMimeInput] = useState("application/pdf");
  const [sourceBytesInput, setSourceBytesInput] = useState("");
  const acceptedProfileRefRef = useRef("");
  const pendingRef = useRef<PendingAction | null>(null);
  const ownerRef = useRef(ownerKey);
  const dataEpoch = useRef(0);
  const contextEpoch = useRef(0);
  const inFlight = useRef(false);
  const reviewCheckboxId = useId();

  async function loadData(scope: string, epoch: number, signal?: AbortSignal) {
    const [availablePage, sourcePage, versionPage] = await Promise.all([
      personalCareerApi.availableSources(signal),
      personalCareerApi.sources(signal),
      personalCareerApi.versions(signal),
    ]);
    if (epoch !== dataEpoch.current || ownerRef.current !== scope) return;
    setAvailable(availablePage.sources);
    setSources(sourcePage.sources);
    setVersions(versionPage.versions);
  }

  // Clear owner-scoped private state before the owner transition can paint.
  useLayoutEffect(() => {
    if (ownerRef.current !== ownerKey) {
      ownerRef.current = ownerKey;
      setAvailable([]);
      setSources([]);
      setVersions([]);
      setReviewVersion(null);
      setReviewConfirmed(false);
      setAcceptedProfileRef("");
      acceptedProfileRefRef.current = "";
      setContext(null);
      setContextLoading(false);
      setSkillsText("");
      setPreferencesText("");
      setConstraintsText("");
      setSourceRefInput("");
      setSourceHashInput("");
      setSourceMimeInput("application/pdf");
      setSourceBytesInput("");
      setMessage("");
    }
    const epoch = ++dataEpoch.current;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- owner changes must clear the old list before the new request resolves.
    setLoading(true);
    if (!ownerKey) {
      setLoading(false);
      setMessage("ログイン状態を確認してください");
      return () => controller.abort();
    }
    void loadData(ownerKey, epoch, controller.signal).catch((error: unknown) => {
      if (controller.signal.aborted || epoch !== dataEpoch.current || ownerRef.current !== ownerKey) return;
      setMessage(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted && epoch === dataEpoch.current && ownerRef.current === ownerKey) setLoading(false);
    });
    return () => controller.abort();
  }, [ownerKey]);

  async function runAction(operation: PendingAction) {
    if (busy || inFlight.current || ownerRef.current !== operation.ownerKey) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await operation.send();
      if (ownerRef.current !== operation.ownerKey || pendingRef.current !== operation) return;
      pendingRef.current = null;
      setPending(null);
      operation.apply(result);
    } catch (error) {
      if (ownerRef.current !== operation.ownerKey || pendingRef.current !== operation) return;
      if (isAmbiguousCareerWrite(error)) {
        setMessage(`${operation.label}の結果が不明です。同じ内容・同じ冪等キーで照合してください。`);
      } else {
        pendingRef.current = null;
        setPending(null);
        setReviewConfirmed(false);
        setMessage(errorMessage(error));
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function startAction(operation: PendingAction) {
    if (!ownerKey || busy || pendingRef.current) return;
    pendingRef.current = operation;
    setPending(operation);
    setMessage("");
    void runAction(operation);
  }

  async function retryPending() {
    const operation = pendingRef.current;
    if (!operation || operation.ownerKey !== ownerKey || busy) return;
    await runAction(operation);
  }

  function registerSource(source: CareerAvailableSourceOut) {
    const body = Object.freeze({
      source_ref: source.source_ref,
      source_sha256: source.source_sha256,
      mime_type: source.mime_type,
      byte_size: source.byte_size,
      idempotency_key: newCareerIdempotencyKey(),
    });
    const operation: PendingAction = Object.freeze({
      ownerKey,
      label: "source 登録",
      send: () => personalCareerApi.registerSource(body),
      apply: () => {
        setMessage("本人用sourceを登録しました。プロフィールに含める場合は明示的に選択してください。");
        const epoch = ++dataEpoch.current;
        void loadData(ownerKey, epoch).catch((error: unknown) => setMessage(errorMessage(error)));
      },
    });
    startAction(operation);
  }

  function toggleSource(source: CareerSourceOut) {
    const body = Object.freeze({ selected: !source.selected, idempotency_key: newCareerIdempotencyKey() });
    const operation: PendingAction = Object.freeze({
      ownerKey,
      label: "source選択",
      send: () => personalCareerApi.selectSource(source.source_ref, body),
      apply: () => {
        setReviewConfirmed(false);
        setContext(null);
        const epoch = ++dataEpoch.current;
        void loadData(ownerKey, epoch).catch((error: unknown) => setMessage(errorMessage(error)));
        setMessage(body.selected ? "sourceを選択しました。" : "sourceの選択を解除しました。");
      },
    });
    startAction(operation);
  }

  function registerPastedSource() {
    const sourceRef = sourceRefInput.trim();
    const body = Object.freeze({
      source_ref: sourceRef,
      source_sha256: sourceHashInput.trim(),
      mime_type: sourceMimeInput.trim(),
      byte_size: Number(sourceBytesInput),
      idempotency_key: newCareerIdempotencyKey(),
    });
    const operation: PendingAction = Object.freeze({
      ownerKey,
      label: "source登録",
      send: () => personalCareerApi.registerSource(body),
      apply: () => {
        setSourceRefInput(""); setSourceHashInput(""); setSourceBytesInput("");
        const epoch = ++dataEpoch.current;
        void loadData(ownerKey, epoch).catch((error: unknown) => setMessage(errorMessage(error)));
        setMessage("owner scope と資料の現在状態を確認してsourceを登録しました。");
      },
    });
    startAction(operation);
  }

  function createProfileVersion() {
    const selectedRefs = sources.filter((source) => source.selected).map((source) => source.source_ref);
    const body = Object.freeze({
      profile: {
        skills: lines(skillsText),
        preferences: lines(preferencesText),
        constraints: lines(constraintsText),
      },
      source_refs: selectedRefs,
      idempotency_key: newCareerIdempotencyKey(),
    });
    const operation: PendingAction = Object.freeze({
      ownerKey,
      label: "プロフィール版作成",
      send: () => personalCareerApi.createVersion(body),
      apply: (result) => {
        const version = result as CareerProfileVersionOut;
        setReviewVersion(version);
        setAcceptedProfileRef("");
        acceptedProfileRefRef.current = "";
        setReviewConfirmed(false);
        setContext(null);
        setVersions((current) => [version, ...current.filter((item) => item.version_ref !== version.version_ref)]);
        setMessage("プロフィール版を作成しました。全内容を確認してから、別操作で本人確認してください。");
      },
    });
    startAction(operation);
  }

  async function openVersion(versionRef: string) {
    setMessage("");
    const epoch = ++dataEpoch.current;
    try {
      const version = await personalCareerApi.version(versionRef);
      if (epoch !== dataEpoch.current || ownerRef.current !== ownerKey) return;
      setReviewVersion(version);
      setReviewConfirmed(false);
      setMessage("");
    } catch (error) {
      if (epoch === dataEpoch.current && ownerRef.current === ownerKey) setMessage(errorMessage(error));
    }
  }

  function confirmVersion() {
    if (!reviewVersion || reviewVersion.confirmed || !reviewConfirmed || busy) return;
    const body = Object.freeze({
      confirmed_digest: reviewVersion.confirmed_digest,
      owner_confirmed: true as const,
      idempotency_key: newCareerIdempotencyKey(),
    });
    const operation: PendingAction = Object.freeze({
      ownerKey,
      label: "プロフィール本人確認",
      send: () => personalCareerApi.confirmVersion(reviewVersion.version_ref, body),
      apply: () => {
        const confirmedVersion = { ...reviewVersion, confirmed: true };
        setReviewVersion(confirmedVersion);
        setVersions((current) => current.map((item) => item.version_ref === confirmedVersion.version_ref ? confirmedVersion : item));
        setReviewConfirmed(false);
        setMessage("表示中のprofile全内容を本人確認済みにしました。");
      },
    });
    startAction(operation);
  }

  async function loadAcceptedContext() {
    if (!ownerKey || !acceptedProfileRef || busy) return;
    const requestOwner = ownerKey;
    const requestProfileRef = acceptedProfileRefRef.current || acceptedProfileRef;
    const requestEpoch = ++contextEpoch.current;
    setContext(null);
    setMessage("");
    setContextLoading(true);
    try {
      const value = await personalCareerApi.context({ accepted_profile_ref: requestProfileRef });
      if (
        ownerRef.current !== requestOwner
        || contextEpoch.current !== requestEpoch
        || acceptedProfileRefRef.current !== requestProfileRef
      ) return;
      setContext(value);
    } catch (error) {
      if (
        ownerRef.current === requestOwner
        && contextEpoch.current === requestEpoch
        && acceptedProfileRefRef.current === requestProfileRef
      ) setMessage(errorMessage(error));
    } finally {
      if (
        ownerRef.current === requestOwner
        && contextEpoch.current === requestEpoch
        && acceptedProfileRefRef.current === requestProfileRef
      ) setContextLoading(false);
    }
  }

  const locked = busy || pending !== null;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-4 text-sm" data-testid="personal-career-workspace">
      <header><h1 className="text-xl font-semibold">転職・キャリア</h1></header>
      {message && <p role="status" aria-live="polite">{message}</p>}
      {!ownerKey && <p role="alert">ログイン状態を確認してください。</p>}
      {loading && <p>本人用データを読み込み中…</p>}

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="career-sources-title">
        <h2 id="career-sources-title" className="font-semibold">資料の登録と選択</h2>
        <ul className="space-y-2">
          {available.map((source) => (
            <li key={source.source_ref} className="flex flex-wrap items-center justify-between gap-2 rounded border p-2">
              <span>{source.mime_type}・{source.byte_size} bytes・{source.source_ref.slice(-6)}</span>
              {source.registered
                ? <span>{source.selected ? "選択中" : "登録済み"}</span>
                : <Button type="button" variant="outline" disabled={locked} onClick={() => registerSource(source)}>この本人用資料を登録</Button>}
            </li>
          ))}
        </ul>
        {available.length === 0 && !loading && <p>登録できる本人用資料はありません。</p>}
        {sources.map((source) => (
          <label key={source.source_ref} className="flex min-h-10 items-center gap-2 border-t pt-2">
            {/* eslint-disable-next-line no-restricted-syntax -- existing source selection field; preserve its stable label contract. */}
            <input type="checkbox" aria-label={`${source.mime_type}・${source.byte_size} bytes・${source.source_ref.slice(-6)}`} checked={Boolean(source.selected)} disabled={locked} onChange={() => toggleSource(source)} />
            <span>{source.mime_type}・{source.byte_size} bytes・{source.source_ref.slice(-6)} {source.selected ? "（選択中）" : ""}</span>
          </label>
        ))}
        <details>
          <summary className="cursor-pointer">資料IDなどを指定して登録</summary>
          <fieldset className="mt-2 grid gap-2 sm:grid-cols-2" disabled={locked}>
            <label className="grid gap-1">本人用source ref<input aria-label="career source ref" value={sourceRefInput} onChange={(event) => setSourceRefInput(event.target.value)} /></label>
            <label className="grid gap-1">SHA-256<input aria-label="career source hash" value={sourceHashInput} onChange={(event) => setSourceHashInput(event.target.value)} /></label>
            <label className="grid gap-1">MIME type<input aria-label="career source mime" value={sourceMimeInput} onChange={(event) => setSourceMimeInput(event.target.value)} /></label>
            <label className="grid gap-1">サイズ（bytes）<input aria-label="career source bytes" inputMode="numeric" value={sourceBytesInput} onChange={(event) => setSourceBytesInput(event.target.value)} /></label>
          </fieldset>
          <Button type="button" className="mt-2" variant="outline" disabled={locked || !sourceRefInput || !sourceHashInput || !/^\d+$/.test(sourceBytesInput)} onClick={registerPastedSource}>owner scopeを検証して登録</Button>
        </details>
      </section>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="career-profile-title">
        <h2 id="career-profile-title" className="font-semibold">プロフィール版を作成</h2>
        <fieldset className="grid gap-3 sm:grid-cols-3" disabled={locked}>
          <label className="grid gap-1">スキル（1行1項目）<textarea aria-label="キャリアスキル" rows={5} value={skillsText} onChange={(event) => setSkillsText(event.target.value)} /></label>
          <label className="grid gap-1">希望（1行1項目）<textarea aria-label="キャリア希望" rows={5} value={preferencesText} onChange={(event) => setPreferencesText(event.target.value)} /></label>
          <label className="grid gap-1">制約（1行1項目）<textarea aria-label="キャリア制約" rows={5} value={constraintsText} onChange={(event) => setConstraintsText(event.target.value)} /></label>
        </fieldset>
        <Button type="button" disabled={locked || !ownerKey} onClick={createProfileVersion}>未確認プロフィール版を作成</Button>
      </section>

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="career-versions-title">
        <h2 id="career-versions-title" className="font-semibold">保存済みプロフィール版</h2>
        <ul className="space-y-2">
          {versions.map((version) => (
            <li key={version.version_ref} className="flex flex-wrap items-center justify-between gap-2 border-b pb-2">
              <span>版 {version.version}・{version.confirmed ? "本人確認済み" : "未確認"}・{version.version_ref.slice(-6)}</span>
              <Button type="button" size="sm" variant="outline" disabled={locked} onClick={() => void openVersion(version.version_ref)}>全内容を確認</Button>
            </li>
          ))}
        </ul>
        {reviewVersion && (
          <article aria-label="本人確認するプロフィール版" className="space-y-3 rounded border p-3" data-testid="career-profile-review">
            <h3 className="font-semibold">版 {reviewVersion.version}・{reviewVersion.confirmed ? "本人確認済み" : "本人確認待ち"}</h3>
            <p className="break-all font-mono">digest: {reviewVersion.confirmed_digest}</p>
            <dl className="grid gap-3 sm:grid-cols-3">
              <div><dt className="font-medium">スキル</dt><dd><ul>{reviewVersion.profile.skills.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul></dd></div>
              <div><dt className="font-medium">希望</dt><dd><ul>{reviewVersion.profile.preferences.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul></dd></div>
              <div><dt className="font-medium">制約</dt><dd><ul>{reviewVersion.profile.constraints.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul></dd></div>
            </dl>
            <p>source refs: {reviewVersion.source_refs.map((ref) => ref.slice(-6)).join("、") || "なし"}</p>
            {reviewVersion.unconfirmed_claim_count > 0 && <p>未確認クレーム {reviewVersion.unconfirmed_claim_count}件は context に含めません。</p>}
            {!reviewVersion.confirmed && (
              <>
                <label htmlFor={reviewCheckboxId} className="flex items-center gap-2">
                  {/* eslint-disable-next-line no-restricted-syntax -- existing confirmation field; preserve its stable label contract. */}
                  <input id={reviewCheckboxId} type="checkbox" checked={reviewConfirmed} disabled={locked} onChange={(event) => setReviewConfirmed(event.target.checked)} />
                  表示したprofile全内容とsource refsを確認しました
                </label>
                <Button type="button" disabled={locked || !reviewConfirmed} onClick={confirmVersion}>この版を本人確認する</Button>
              </>
            )}
          </article>
        )}
      </section>

      {pending && (
        <section aria-label="キャリア操作結果の照合" className="rounded border border-amber-300 p-3">
          <p>{pending.ownerKey === ownerKey ? `${pending.label}の結果が不明です。` : "別のログインスコープで保留中です。元のアカウントに戻ると同じ操作で照合できます。"}</p>
          <Button type="button" disabled={busy || pending.ownerKey !== ownerKey} onClick={() => void retryPending()}>同じ内容・同じ冪等キーで照合</Button>
        </section>
      )}

      <section className="space-y-3 rounded-lg border p-4" aria-labelledby="career-context-title">
        <h2 id="career-context-title" className="font-semibold">確認済みプロフィールを明示指定して context を確認</h2>
        <label className="grid max-w-xl gap-1">本人が選択する accepted_profile_ref
        {/* eslint-disable-next-line no-restricted-syntax -- the native control preserves keyboard/form semantics for this owner-only identifier picker. */}
        <select aria-label="accepted_profile_ref" value={acceptedProfileRef} disabled={locked} onChange={(event) => { contextEpoch.current += 1; acceptedProfileRefRef.current = event.target.value; setAcceptedProfileRef(event.target.value); setContext(null); setContextLoading(false); }}>
            <option value="">選択してください</option>
            {versions.filter((version) => version.confirmed).map((version) => <option key={version.version_ref} value={version.version_ref}>版 {version.version}・{version.version_ref.slice(-6)}</option>)}
          </select>
        </label>
        <Button type="button" disabled={locked || !acceptedProfileRef} onClick={() => void loadAcceptedContext()}>このaccepted版だけでcontextを確認</Button>
        {contextLoading && <p role="status">選択中のaccepted版のcontextを読み込み中…</p>}
        {context && (
          <div data-testid="career-context" className="rounded border p-3">
            <p>スキル: {context.profile.skills.join("、") || "なし"}</p>
            <p>希望: {context.profile.preferences.join("、") || "なし"}</p>
            <p>制約: {context.profile.constraints.join("、") || "なし"}</p>
            <p>選択中source refs: {context.source_refs.map((ref) => ref.slice(-6)).join("、") || "なし"}</p>
            {context.needs_review.map((reason) => <p key={reason}>要確認: {reason}</p>)}
          </div>
        )}
      </section>

      <PersonalAgentInvocationPanel
        title="Career Agentで明示したaccepted profileのcontextを読む"
        profileId="personal-career-planner"
        operation="career.context"
        actionLabel="選択したaccepted版をpersonal Agent adapterで確認"
        invoke={personalCareerApi.invokePersonalAgent}
        getArguments={() => ({ accepted_profile_ref: acceptedProfileRef })}
        canInvoke={Boolean(acceptedProfileRef && versions.some((version) => version.version_ref === acceptedProfileRef && version.confirmed))}
        disabledReason={acceptedProfileRef && versions.some((version) => version.version_ref === acceptedProfileRef && version.confirmed) ? undefined : "本人確認済みのaccepted_profile_refを明示選択してください。"}
      />
    </div>
  );
}
