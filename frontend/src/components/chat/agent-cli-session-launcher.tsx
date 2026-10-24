"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { HelpPopover } from "@/components/ui/help-popover";
import { Input } from "@/components/ui/input";
import {
  ChatApiError,
  chatApi,
  type CliSessionCreateRequest,
} from "@/lib/chat-api";
import { withBasePath } from "@/lib/base-path";
import {
  executionAvailabilityLabel,
  providerReadinessLabel,
  type AgentExecutionProfileCatalogItem,
} from "@/lib/agent-runtime-selection";

type CatalogContext =
  | { sessionId: string; projectId?: never }
  | { sessionId?: never; projectId?: string | null };

export type AgentCliSessionLauncherProps = CatalogContext & {
  agentId: string;
  agentRevisionId: string;
  profile: AgentExecutionProfileCatalogItem;
};

type Attempt = {
  createFingerprint: string;
  createKey: string;
  messageFingerprint: string;
  messageKey: string;
  sessionId: string | null;
  sessionVerified: boolean;
};

type CliSessionProvider = CliSessionCreateRequest["provider"];
const CLI_SESSION_PROVIDERS = new Set<CliSessionProvider>([
  "antigravity-cli",
  "claude-cli",
  "codex-cli",
  "grok-cli",
]);

function isCliSessionProvider(value: string): value is CliSessionProvider {
  return CLI_SESSION_PROVIDERS.has(value as CliSessionProvider);
}

function newIdempotencyKey(kind: "create" | "message"): string {
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `web-cli-${kind}:${id}`;
}

function errorText(error: unknown): string {
  if (error instanceof ChatApiError) {
    return `${error.message}${error.code ? ` (${error.code})` : ""}`;
  }
  return error instanceof Error && error.message.trim()
    ? error.message
    : "CLIセッションを開始できませんでした";
}

export function AgentCliSessionLauncher(props: AgentCliSessionLauncherProps) {
  const { agentId, agentRevisionId, profile } = props;
  const [open, setOpen] = useState(false);
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [message, setMessage] = useState("");
  const [includeAgentRole, setIncludeAgentRole] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionVerified, setSessionVerified] = useState(false);
  const attemptRef = useRef<Attempt | null>(null);

  const canOpen =
    profile.ingress === "cli_session" &&
    profile.provider_readiness !== "unavailable" &&
    (profile.available || profile.availability_category === "context_required");

  const handleSubmit = async () => {
    const cwd = workingDirectory.trim();
    if (!cwd || !message) return;
    setPending(true);
    setError(null);
    try {
      let attempt = attemptRef.current;
      let target = profile;
      if (!attempt?.sessionId) {
        const catalog = await chatApi.getAgentExecutionProfileCatalog(agentId, {
          ...(props.sessionId
            ? { sessionId: props.sessionId }
            : { agentRevisionId, projectId: props.projectId ?? null }),
          workingDirectory: cwd,
        });
        const validated = catalog.profiles.find(
          (candidate) => candidate.profile_id === profile.profile_id,
        );
        if (!validated || validated.ingress !== "cli_session") {
          throw new Error("選択したExecution ProfileはCLIセッション入口で利用できません");
        }
        if (!validated.available) {
          throw new Error(
            `${executionAvailabilityLabel(validated)}${
              validated.availability_reason ? ` (${validated.availability_reason})` : ""
            }`,
          );
        }
        if (validated.provider_readiness === "unavailable") {
          throw new Error(
            `${providerReadinessLabel(validated.provider_readiness)}${
              validated.provider_readiness_reason
                ? ` (${validated.provider_readiness_reason})`
                : ""
            }`,
          );
        }
        const targetModel = validated.model;
        if (!targetModel) {
          throw new Error("CLIセッションのmodelが登録されていません");
        }
        if (!isCliSessionProvider(validated.provider)) {
          throw new Error("CLIセッションのproviderが登録されていません");
        }
        const targetProvider = validated.provider;
        target = validated;
        const expectedRoleContextMode = includeAgentRole ? "agent_role" : "raw";
        const createFingerprint = JSON.stringify({
          agentId,
          agentRevisionId,
          projectId: props.sessionId ? null : props.projectId?.trim() || null,
          profileId: target.profile_id,
          provider: targetProvider,
          model: targetModel,
          effort: target.effort,
          cwd,
          roleContextMode: expectedRoleContextMode,
        });
        const messageFingerprint = JSON.stringify({ message });
        if (
          !attempt ||
          attempt.createFingerprint !== createFingerprint ||
          attempt.messageFingerprint !== messageFingerprint
        ) {
          attempt = {
            createFingerprint,
            createKey: newIdempotencyKey("create"),
            messageFingerprint,
            messageKey: newIdempotencyKey("message"),
            sessionId: null,
            sessionVerified: false,
          };
          attemptRef.current = attempt;
        }
        const created = await chatApi.createCliSession({
          agent_id: agentId,
          agent_revision_id: agentRevisionId,
          ...(!props.sessionId && props.projectId?.trim()
            ? { project_id: props.projectId.trim() }
            : {}),
          provider: targetProvider,
          model: targetModel,
          ...(target.effort ? { effort: target.effort } : {}),
          working_directory: cwd,
          idempotency_key: attempt.createKey,
          execution_profile_id: target.profile_id,
          ...(includeAgentRole ? { role_context_mode: "agent_role" as const } : {}),
        });
        const createdSessionId = created.session_id?.trim();
        if (!createdSessionId) {
          throw new Error("CLIセッション作成の受付IDを確認できませんでした");
        }
        attempt.sessionId = createdSessionId;
        setSessionId(createdSessionId);
        if (created.agent_id !== agentId || created.agent_revision_id !== agentRevisionId) {
          throw new Error(
            "CLIセッションのAgent/revision照合に失敗したため、初回turnを送信しませんでした",
          );
        }
        if (
          created.provider !== targetProvider ||
          created.model !== targetModel ||
          (created.effort ?? "") !== (target.effort ?? "")
        ) {
          throw new Error(
            "CLIセッションのprovider/model/effort照合に失敗したため、初回turnを送信しませんでした",
          );
        }
        const returnedProfileId =
          typeof created.execution_profile_id === "string"
            ? created.execution_profile_id
            : null;
        const returnedRoleContextMode =
          created.role_context_mode === "raw" || created.role_context_mode === "agent_role"
            ? created.role_context_mode
            : null;
        const returnedRoleContextDigest = created.role_context_sha256;
        const roleContextDigestMatches = includeAgentRole
          ? typeof returnedRoleContextDigest === "string" &&
            /^[0-9a-f]{64}$/iu.test(returnedRoleContextDigest)
          : returnedRoleContextDigest == null;
        if (
          returnedProfileId !== target.profile_id ||
          returnedRoleContextMode !== expectedRoleContextMode ||
          !roleContextDigestMatches
        ) {
          throw new Error(
            "CLIセッションのExecution ProfileまたはRole context照合に失敗したため、初回turnを送信しませんでした",
          );
        }
        attempt.sessionVerified = true;
        setSessionVerified(true);
      }

      if (!attempt?.sessionId || !attempt.sessionVerified) {
        throw new Error("CLIセッションを確認できませんでした");
      }
      await chatApi.sendCliSessionMessage(attempt.sessionId, {
        message,
        idempotency_key: attempt.messageKey,
      });
      setAccepted(true);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <Button
        type="button"
        size="xs"
        variant="outline"
        disabled={!canOpen}
        onClick={() => setOpen(true)}
      >
        CLIセッションを開始
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent size="md" showCloseButton={!pending}>
          <DialogHeader>
            <DialogTitle>同じAgentのCLIセッションを開始</DialogTitle>
            <DialogDescription>
              Agent/revisionとExecution Profileを固定し、会話送信とは別のCLI入口へ送ります。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
              <dt className="text-muted-foreground">Agent</dt>
              <dd className="break-all">{agentId}</dd>
              <dt className="text-muted-foreground">Revision</dt>
              <dd className="break-all">{agentRevisionId}</dd>
              <dt className="text-muted-foreground">Execution Profile</dt>
              <dd>{profile.name} ({profile.profile_id})</dd>
            </dl>

            <label className="grid gap-1 text-xs">
              <span className="inline-flex items-center gap-1">
                作業ディレクトリ
                <HelpPopover label="作業ディレクトリ">
                  trusted Windows policyのallowed_roots内にある絶対パスを指定します。owner・Agent・pathは送信前にserverが再検証します。
                </HelpPopover>
              </span>
              <Input
                aria-label="CLI作業ディレクトリ"
                value={workingDirectory}
                onChange={(event) => setWorkingDirectory(event.target.value)}
                placeholder="D:\\Dev\\project"
                disabled={pending || Boolean(sessionId)}
              />
            </label>

            <label className="grid gap-1 text-xs">
              <span>最初の依頼</span>
              <textarea
                aria-label="CLI最初の依頼"
                className="min-h-24 resize-y rounded-lg border border-input bg-card px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:bg-muted disabled:opacity-50"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                disabled={pending || Boolean(sessionId)}
              />
            </label>

            <label className="flex items-start gap-2 text-xs">
              <Checkbox
                aria-label="このAgentの役割で実行"
                checked={includeAgentRole}
                onCheckedChange={(checked) => setIncludeAgentRole(checked === true)}
                disabled={pending || Boolean(sessionId)}
              />
              <span>
                このAgentの役割で実行
                <span className="block text-[10px] text-muted-foreground">
                  {includeAgentRole
                    ? "Agent Role contextを固定して配送します。"
                    : "raw: native CLIへAgentの職務本文は追加されません。"}
                </span>
              </span>
            </label>

            {error ? (
              <p role="alert" className="text-xs text-destructive">{error}</p>
            ) : null}
            {accepted && sessionId ? (
              <div role="status" className="rounded-md border border-emerald-500/40 bg-emerald-50 p-2 text-xs text-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200">
                <p>CLIセッション作成と初回turnの送信受付が完了しました。セッション詳細ではprepared / sentの証拠を確認できます。provider固有の受領は共通には確認されていません。</p>
                <a
                  className="break-all underline underline-offset-2"
                  href={withBasePath(`/api/python-proxy/cli-sessions/${encodeURIComponent(sessionId)}`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  セッション詳細を開く ({sessionId})
                </a>
              </div>
            ) : sessionId && sessionVerified ? (
              <div role="status" className="rounded-md border p-2 text-xs">
                <p>CLIセッションは作成済みです。初回turnは未受付のため、同じidempotency keyで再送できます。</p>
                <a
                  className="break-all underline underline-offset-2"
                  href={withBasePath(`/api/python-proxy/cli-sessions/${encodeURIComponent(sessionId)}`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  セッション詳細を開く ({sessionId})
                </a>
              </div>
            ) : sessionId ? (
              <div role="alert" className="rounded-md border border-destructive/40 p-2 text-xs text-destructive">
                <p>作成されたCLIセッションの固定contextを照合できません。初回turnは送信しません。</p>
                <a
                  className="break-all underline underline-offset-2"
                  href={withBasePath(`/api/python-proxy/cli-sessions/${encodeURIComponent(sessionId)}`)}
                  target="_blank"
                  rel="noreferrer"
                >
                  セッション詳細を開く ({sessionId})
                </a>
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={pending}>
              閉じる
            </Button>
            {!accepted && (!sessionId || sessionVerified) ? (
              <Button
                type="button"
                onClick={() => void handleSubmit()}
                disabled={pending || !workingDirectory.trim() || !message}
              >
                {pending ? "確認・送信中…" : sessionId ? "初回turnを再送" : "確認して開始"}
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
