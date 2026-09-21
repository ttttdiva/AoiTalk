"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, useEffect, useCallback, useRef } from "react";
import useSWR from "swr";
import { CheckCheck, Bell, X } from "lucide-react";
import { formatRelativeTime } from "@/lib/utils";
import {
  claimOsNotificationCandidates,
  isFreshUnreadOsNotification,
} from "@/lib/os-notification-dedupe";
import {
  getKnowledgeCaptureNotificationTarget,
  getKnowledgeCaptureNotificationDeepLink,
  getNotificationInternalRoute,
  isUuid,
  isKnowledgeCaptureNotificationType,
  KNOWLEDGE_CAPTURE_OPEN_MODE,
  matchesKnowledgeCaptureNotificationDetail,
  normalizeKnowledgeCaptureCandidateDetail,
  type KnowledgeCaptureCandidate,
  type KnowledgeCaptureNotificationTarget,
} from "@/lib/knowledge-capture";
import { KnowledgeCaptureReviewDialog } from "@/components/knowledge-capture/knowledge-capture-review-dialog";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";
import {
  Popover,
  PopoverContent,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useOptionalShellChrome } from "../shell-context";

// ─── 通知 ───
const OS_NOTIFICATION_SEEN_KEY = "aoitalk-os-notification-seen";
const OS_NOTIFICATION_SEEN_LIMIT = 200;
const OS_NOTIFICATION_STALE_MS = 24 * 60 * 60 * 1000;
const OS_NOTIFICATION_LOCK_NAME = "aoitalk-os-notification-claim";
const NOTIFICATION_SERVICE_WORKER_URL = "/aoitalk-notifications-sw.js";
const NOTIFICATIONS_SWR_KEY = "layout/notifications";
const HANDLED_SERVICE_WORKER_CLICK_EVENTS = new WeakSet<object>();
const CONSUMED_OPEN_NOTIFICATION_IDS = new Set<string>();
const MAX_CONSUMED_OPEN_NOTIFICATION_IDS = 200;

type OsNotificationPermission = NotificationPermission | "unsupported";

interface InAppNotification {
  id: string;
  type: string;
  notification_type?: string | null;
  title: string;
  message?: string | null;
  project_id?: string | null;
  task_id?: string | null;
  is_read: boolean;
  created_at: string;
  delivered_at?: string | null;
  payload?: Record<string, unknown> | null;
}

const EMPTY_NOTIFICATIONS: InAppNotification[] = [];

type NotificationEvidence = {
  kind: string;
  timestamp?: string;
  hash?: string;
};

const EVIDENCE_KIND_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+\- ]{0,79}$/;
const EVIDENCE_HASH_RE = /^(?:sha256:)?[0-9a-f]{64}$/;

export function boundedWorkIntelligenceEvidence(
  notification: InAppNotification,
): NotificationEvidence[] {
  const payload = notification.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return [];
  }
  const roots: unknown[] = [
    payload.work_intelligence_evidence,
    payload.work_intelligence_evidence_refs,
    payload.evidence_refs,
    payload.evidence,
  ];
  const rows: unknown[] = [];
  for (const root of roots) {
    if (Array.isArray(root)) rows.push(...root);
    else if (root && typeof root === "object" && !Array.isArray(root)) {
      const value = root as Record<string, unknown>;
      const nested = value.evidence_refs ?? value.evidence ?? value.refs;
      if (Array.isArray(nested)) rows.push(...nested);
    }
    if (rows.length >= 24) break;
  }
  const result: NotificationEvidence[] = [];
  const seen = new Set<string>();
  for (const row of rows.slice(0, 24)) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    const value = row as Record<string, unknown>;
    const rawKind = typeof value.kind === "string" ? value.kind.trim() : "";
    if (!rawKind || !EVIDENCE_KIND_RE.test(rawKind)) continue;
    const rawHash =
      typeof value.hash === "string"
        ? value.hash.trim().toLowerCase()
        : typeof value.ref_hash === "string"
          ? value.ref_hash.trim().toLowerCase()
          : "";
    const hash = EVIDENCE_HASH_RE.test(rawHash)
      ? rawHash.startsWith("sha256:")
        ? rawHash
        : `sha256:${rawHash}`
      : undefined;
    const rawTimestamp =
      typeof value.timestamp === "string"
        ? value.timestamp.trim()
        : typeof value.created_at === "string"
          ? value.created_at.trim()
          : "";
    const timestampDate = rawTimestamp ? new Date(rawTimestamp) : null;
    const timestamp =
      timestampDate && Number.isFinite(timestampDate.getTime())
        ? timestampDate.toISOString()
        : undefined;
    const key = `${rawKind}\0${timestamp ?? ""}\0${hash ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ kind: rawKind, timestamp, hash });
    if (result.length >= 12) break;
  }
  return result;
}

function shortEvidenceHash(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

function NotificationEvidenceDetails({
  notification,
}: {
  notification: InAppNotification;
}) {
  const evidence = boundedWorkIntelligenceEvidence(notification);
  if (evidence.length === 0) return null;
  return (
    <details className="mx-3 mb-2 rounded border border-border/60 bg-background/30 px-2 py-1.5 text-[10px]">
      <summary className="cursor-pointer list-none font-medium text-muted-foreground [&::-webkit-details-marker]:hidden">
        根拠 {evidence.length}件を表示
      </summary>
      <ul className="mt-1.5 space-y-1" aria-label="通知の根拠">
        {evidence.map((item, index) => (
          <li key={`${item.kind}-${item.timestamp ?? ""}-${index}`} className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 flex-1 truncate">{item.kind}</span>
            {item.timestamp && <time className="shrink-0 text-muted-foreground" dateTime={item.timestamp}>{item.timestamp}</time>}
            {item.hash && <span className="shrink-0 font-mono text-muted-foreground" title="認可には使わない相関ハッシュ">{shortEvidenceHash(item.hash)}</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

async function getNotificationServiceWorkerRegistration() {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return null;
  }
  try {
    const registration = await navigator.serviceWorker.register(
      NOTIFICATION_SERVICE_WORKER_URL,
      { scope: "/" },
    );
    // `register()` may return before the worker controls the page.  Awaiting
    // `ready` avoids subscribing against an installing worker and makes
    // registration/subscription deterministic across tabs and reloads.
    try {
      return await navigator.serviceWorker.ready;
    } catch {
      return registration;
    }
  } catch {
    return null;
  }
}

type NotificationPanelProps = {
  /** Bind open state and unread count to the shared shell chrome. */
  listenGlobal?: boolean;
  /** Render as a standalone notification card anchored to the rail button. */
  presentation?: "sidebar" | "popover";
  /** Optional trigger styling for hosts outside the Global Rail. */
  triggerClassName?: string;
  /** Close the workspace navigation before displaying the popover. */
  onOpenChange?: (open: boolean) => void;
};

type NotificationBellPopoverProps = {
  /** Mount the data-owning popover only for the requested responsive surface. */
  mobileOnly?: boolean;
  triggerClassName?: string;
  onOpenChange?: (open: boolean) => void;
};

const MOBILE_BREAKPOINT = 768;

function useResolvedMobileSurface(): boolean | null {
  const [isMobile, setIsMobile] = useState<boolean | null>(null);

  useEffect(() => {
    const mediaQuery =
      typeof window.matchMedia === "function"
        ? window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
        : null;
    const update = () => {
      setIsMobile(mediaQuery ? mediaQuery.matches : window.innerWidth < MOBILE_BREAKPOINT);
    };
    update();
    mediaQuery?.addEventListener("change", update);
    return () => mediaQuery?.removeEventListener("change", update);
  }, []);

  return isMobile;
}

function decodeVapidKey(value: string): BufferSource {
  const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  const binary = window.atob(padded);
  return Uint8Array.from(
    binary,
    (character) => character.charCodeAt(0),
  ) as unknown as BufferSource;
}

async function ensureWebPushSubscription(
  registration: ServiceWorkerRegistration,
): Promise<boolean> {
  if (!("PushManager" in window) || !registration.pushManager) return false;
  try {
    const response = await fetch("/api/web-push/vapid-public-key", {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) return false;
    const config = (await response.json()) as {
      enabled?: boolean;
      public_key?: string | null;
    };
    if (!config.enabled || !config.public_key) return false;

    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: decodeVapidKey(config.public_key),
      });
    }
    const json = subscription.toJSON();
    const endpoint = json.endpoint;
    const p256dh = json.keys?.p256dh;
    const auth = json.keys?.auth;
    if (!endpoint || !p256dh || !auth) return false;

    const saveResponse = await fetch("/api/web-push/subscription", {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        endpoint,
        expiration_time: subscription.expirationTime,
        p256dh,
        auth,
        content_encoding: "aes128gcm",
      }),
    });
    return saveResponse.ok;
  } catch {
    // Push is an optional enhancement. Browser policy/provider failures fall
    // back to the existing in-app and page Notification API paths.
    return false;
  }
}

function rememberPushedNotification(id: string): void {
  try {
    const current = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
    const parsed = current ? JSON.parse(current) : [];
    const ids = Array.isArray(parsed)
      ? parsed.filter((value): value is string => typeof value === "string")
      : [];
    if (!ids.includes(id)) ids.push(id);
    window.localStorage.setItem(
      OS_NOTIFICATION_SEEN_KEY,
      JSON.stringify(ids.slice(-OS_NOTIFICATION_SEEN_LIMIT)),
    );
  } catch {
    // localStorage is best-effort; push delivery remains durable in the inbox.
  }
}

export function NotificationPanel({
  listenGlobal = false,
  presentation = "sidebar",
  triggerClassName,
  onOpenChange,
}: NotificationPanelProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedOpenNotificationId = searchParams.get("open_notification");
  const shellChrome = useOptionalShellChrome();
  const [localOpen, setLocalOpen] = useState(false);
  const open =
    listenGlobal && shellChrome ? shellChrome.notificationPanelOpen : localOpen;
  const publishUnreadCount = useCallback(
    (nextCount: number) => {
      if (!listenGlobal || !shellChrome) return;
      shellChrome.setNotificationUnreadCount(nextCount);
    },
    [listenGlobal, shellChrome],
  );
  const setPanelOpen = useCallback(
    (next: boolean) => {
      onOpenChange?.(next);
      if (listenGlobal && shellChrome) {
        shellChrome.setNotificationPanelOpen(next);
      } else {
        setLocalOpen(next);
      }
    },
    [listenGlobal, onOpenChange, shellChrome],
  );
  const [osNotificationPermission, setOsNotificationPermission] =
    useState<OsNotificationPermission>(() => {
      // ブラウザ API を初期化子で同期的に読み取る（SSR/未対応環境では unsupported）。
      // 権限依存 UI は open=false の初期描画に現れないため hydration 差異は生じない。
      if (typeof window === "undefined" || !("Notification" in window)) {
        return "unsupported";
      }
      return window.Notification.permission;
    });
  // syncOsNotifications は本フック定義より後方で宣言されるため、ref 経由で
  // SWR の onSuccess から最新実装を呼ぶ（宣言順の循環を避ける）。
  const syncOsNotificationsRef = useRef<(next: InAppNotification[]) => void>(
    () => {},
  );

  // 通知一覧の取得・ポーリングを SWR に委譲。低帯域配慮でポーリングは 60 秒間隔とし、
  // タブ非表示中・オフライン中はポーリングを止めて通信量を抑える。
  // 取得成功時のみ OS 通知同期を行う。
  const { data, mutate, isLoading } = useSWR<InAppNotification[]>(
    NOTIFICATIONS_SWR_KEY,
    async () => {
      const res = await fetch("/api/notifications", {
        credentials: "include",
        signal: AbortSignal.timeout(5000),
      });
      // 非 OK 時は throw して直前データを保持（従来の「何もしない」と同義）。
      if (!res.ok) throw new Error("通知の取得に失敗しました");
      const payload = await res.json();
      return Array.isArray(payload)
        ? payload
        : (payload.notifications ?? []);
    },
    {
      refreshInterval: 60000,
      refreshWhenHidden: false,
      refreshWhenOffline: false,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      keepPreviousData: true,
      dedupingInterval: 0,
      onSuccess: (nextNotifications) => {
        publishUnreadCount(nextNotifications.filter((notification) => !notification.is_read).length);
        syncOsNotificationsRef.current(nextNotifications);
      },
    },
  );
  const notifications = data ?? EMPTY_NOTIFICATIONS;
  const loading = isLoading;

  const [reviewCandidate, setReviewCandidate] =
    useState<KnowledgeCaptureCandidate | null>(null);
  const [reviewTarget, setReviewTarget] =
    useState<KnowledgeCaptureNotificationTarget | null>(null);
  const [reviewNotificationId, setReviewNotificationId] = useState<string | null>(
    null,
  );
  const [reviewOpen, setReviewOpen] = useState(false);
  const [knowledgeCaptureLoadingId, setKnowledgeCaptureLoadingId] = useState<
    string | null
  >(null);
  const [knowledgeCaptureError, setKnowledgeCaptureError] = useState<string | null>(
    null,
  );
  const reportKnowledgeCaptureError = useCallback(
    (message: string) => {
      setKnowledgeCaptureError(message);
      // Cold-start/SW opens happen while the notification panel is closed.
      // Surface the bounded failure there so a stale target is actionable
      // without navigating to a different Project view.
      setPanelOpen(true);
    },
    [setPanelOpen],
  );

  const notificationTypeLabel = useCallback((type: string) => {
    switch (type) {
      case "reminder":
        return "リマインダー";
      case "due_soon":
        return "期限間近";
      case "overdue":
        return "期限超過";
      case "assigned":
        return "アサイン";
      case "comment":
        return "コメント";
      case "knowledge_capture_question":
        return "ナレッジ確認";
      case "knowledge_capture_draft":
        return "ナレッジ候補";
      default:
        return type;
    }
  }, []);

  const markAsRead = useCallback(
    async (id: string) => {
      const notification = notifications.find((item) => item.id === id);
      if (notification?.is_read) return false;
      try {
        const response = await fetch(`/api/notifications/${id}/read`, {
          method: "POST",
          credentials: "include",
        });
        if (!response.ok) return false;
        void mutate(
          (prev) =>
            (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
              n.id === id ? { ...n, is_read: true } : n,
            ),
          { revalidate: false },
        );
        publishUnreadCount(
          notifications.filter(
            (item) => item.id !== id && !item.is_read,
          ).length,
        );
        return true;
      } catch {
        // エラー時は何もしない
        return false;
      }
    },
    [mutate, notifications, publishUnreadCount],
  );

  const markAllAsRead = useCallback(async () => {
    const unreadOrdinaryNotifications = notifications.filter(
      (notification) =>
        !notification.is_read &&
        !isKnowledgeCaptureNotificationType(notification),
    );
    const unreadIds = unreadOrdinaryNotifications.map((notification) => notification.id);
    if (unreadIds.length === 0) return;
    const unreadCountBefore = notifications.filter((n) => !n.is_read).length;
    const remainingUnreadCount = notifications.filter(
      (notification) =>
        !notification.is_read && !unreadIds.includes(notification.id),
    ).length;

    const rollback = () => {
      publishUnreadCount(unreadCountBefore);
      return mutate(
        (prev) =>
          (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
            unreadIds.includes(n.id) ? { ...n, is_read: false } : n,
          ),
        { revalidate: false },
      );
    };

    // KC is read only after its review dialog opens.  Optimistically update
    // ordinary notifications only; a malformed/stale KC row is still typed
    // KC and must remain visibly unread.
    void mutate(
      (prev) =>
        (prev ?? EMPTY_NOTIFICATIONS).map((n) =>
          unreadIds.includes(n.id) ? { ...n, is_read: true } : n,
        ),
      { revalidate: false },
    );
    publishUnreadCount(remainingUnreadCount);
    try {
      const res = await fetch("/api/notifications/read-all", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) void rollback();
    } catch {
      void rollback();
    }
  }, [mutate, notifications, publishUnreadCount]);

  const openKnowledgeCaptureNotification = useCallback(
    async (notificationId: string): Promise<boolean> => {
      const normalizedNotificationId =
        typeof notificationId === "string" ? notificationId.trim() : "";
      setKnowledgeCaptureError(null);
      setReviewOpen(false);
      setReviewTarget(null);
      setReviewCandidate(null);
      setReviewNotificationId(null);

      if (!normalizedNotificationId) {
        reportKnowledgeCaptureError(
          "ナレッジ通知を開けませんでした。通知は未読のままです。",
        );
        return false;
      }

      setKnowledgeCaptureLoadingId(normalizedNotificationId);
      try {
        const notificationResponse = await fetch(
          `/api/notifications/${encodeURIComponent(normalizedNotificationId)}`,
          {
            credentials: "include",
            cache: "no-store",
          },
        );
        if (!notificationResponse.ok) {
          reportKnowledgeCaptureError(
            "ナレッジ通知を開けませんでした。通知は未読のままです。",
          );
          return false;
        }

        const durableNotification = await notificationResponse.json();
        if (
          !durableNotification ||
          typeof durableNotification !== "object" ||
          typeof durableNotification.id !== "string" ||
          durableNotification.id.toLowerCase() !==
            normalizedNotificationId.toLowerCase()
        ) {
          reportKnowledgeCaptureError(
            "ナレッジ通知を開けませんでした。通知は未読のままです。",
          );
          return false;
        }

        if (!isKnowledgeCaptureNotificationType(durableNotification)) {
          reportKnowledgeCaptureError(
            "ナレッジ通知を開けませんでした。通知は未読のままです。",
          );
          return false;
        }
        const target = getKnowledgeCaptureNotificationTarget(
          durableNotification,
        );
        if (!target) {
          reportKnowledgeCaptureError(
            "ナレッジ通知を開けませんでした。通知は未読のままです。",
          );
          return false;
        }

        const detailResponse = await fetch(
          `/api/projects/${encodeURIComponent(target.projectId)}/knowledge-capture/candidates/${encodeURIComponent(target.candidateId)}`,
          {
            credentials: "include",
            cache: "no-store",
          },
        );
        if (!detailResponse.ok) {
          reportKnowledgeCaptureError(
            detailResponse.status === 404
              ? "ナレッジ候補が見つかりません。通知は未読のままです。"
              : "ナレッジ候補の詳細を取得できませんでした。通知は未読のままです。",
          );
          return false;
        }

        const detail = normalizeKnowledgeCaptureCandidateDetail(
          await detailResponse.json(),
        );
        if (!matchesKnowledgeCaptureNotificationDetail(target, detail)) {
          reportKnowledgeCaptureError(
            "ナレッジ通知の候補状態が変わっています。通知は未読のままです。",
          );
          return false;
        }

        // The modal is mounted outside the popover. This closes the panel
        // without navigating and gives the dialog its authoritative detail.
        setPanelOpen(false);
        setReviewTarget(target);
        setReviewCandidate(detail);
        setReviewNotificationId(normalizedNotificationId);
        setReviewOpen(true);
        return true;
      } catch {
        reportKnowledgeCaptureError(
          "ナレッジ候補の詳細を取得できませんでした。通知は未読のままです。",
        );
        return false;
      } finally {
        setKnowledgeCaptureLoadingId(null);
      }
    },
    [reportKnowledgeCaptureError, setPanelOpen],
  );

  const handleNotificationClick = useCallback(
    async (notification: InAppNotification) => {
      if (isKnowledgeCaptureNotificationType(notification)) {
        void openKnowledgeCaptureNotification(notification.id);
        return;
      }
      if (!notification.is_read) {
        await markAsRead(notification.id);
      }
      const route = getNotificationInternalRoute(notification);
      if (route) {
        setPanelOpen(false);
        router.push(route);
      }
    },
    [markAsRead, openKnowledgeCaptureNotification, router, setPanelOpen],
  );

  const handleReviewDidOpen = useCallback(
    (notificationId: string) => {
      void markAsRead(notificationId);
    },
    [markAsRead],
  );

  const handleReviewResolved = useCallback(() => {
    setReviewOpen(false);
    setReviewTarget(null);
    setReviewCandidate(null);
    setReviewNotificationId(null);
    setKnowledgeCaptureError(null);
    void mutate();
  }, [mutate]);

  const handleReviewOpenChange = useCallback((nextOpen: boolean) => {
    setReviewOpen(nextOpen);
    if (!nextOpen) {
      setReviewTarget(null);
      setReviewCandidate(null);
      setReviewNotificationId(null);
    }
  }, []);

  const showOsNotification = useCallback(
    async (notification: InAppNotification) => {
      if (
        typeof window === "undefined" ||
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const isKnowledgeCapture = isKnowledgeCaptureNotificationType(notification);
      const url = isKnowledgeCapture
        ? getKnowledgeCaptureNotificationDeepLink(notification.id) ?? "/chat"
        : getNotificationInternalRoute(notification) ?? "/";
      const options: NotificationOptions = {
        body: notification.message || notificationTypeLabel(notification.type),
        tag: `aoitalk-${notification.id}`,
        data: isKnowledgeCapture
          ? {
              url,
              notificationId: notification.id,
              openMode: KNOWLEDGE_CAPTURE_OPEN_MODE,
            }
          : {
              url,
              notificationId: notification.id,
            },
        icon: "/favicon.ico",
        requireInteraction: true,
      };

      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) {
        try {
          await registration.showNotification(notification.title, options);
          return;
        } catch {
          // Fall back to the page-level Notification API below.
        }
      }

      const osNotification = new window.Notification(
        notification.title,
        options,
      );
      osNotification.onclick = () => {
        window.focus();
        if (isKnowledgeCapture) {
          void openKnowledgeCaptureNotification(notification.id);
          return;
        }
        void markAsRead(notification.id);
        const route = getNotificationInternalRoute(notification);
        if (route) {
          setPanelOpen(false);
          router.push(route);
        }
      };
    },
    [
      markAsRead,
      notificationTypeLabel,
      openKnowledgeCaptureNotification,
      router,
      setPanelOpen,
    ],
  );

  const syncOsNotifications = useCallback(
    async (nextNotifications: InAppNotification[]) => {
      if (typeof window === "undefined") return;

      if (
        !("Notification" in window) ||
        window.Notification.permission !== "granted"
      ) {
        return;
      }

      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) {
        try {
          const now = Date.now();
          const activeTags = new Set(
            nextNotifications
              .filter((notification) =>
                isFreshUnreadOsNotification(
                  notification,
                  now,
                  OS_NOTIFICATION_STALE_MS,
                ),
              )
              .map((notification) => `aoitalk-${notification.id}`),
          );
          const displayed = await registration.getNotifications();
          displayed.forEach((notification) => {
            if (
              notification.tag.startsWith("aoitalk-") &&
              !activeTags.has(notification.tag)
            ) {
              notification.close();
            }
          });
        } catch {
          // Cleanup failure must not block claiming new notifications.
        }
      }

      const claim = () => {
        let storedIds: string[] = [];
        try {
          const stored = window.localStorage.getItem(OS_NOTIFICATION_SEEN_KEY);
          const parsed = stored ? JSON.parse(stored) : [];
          if (Array.isArray(parsed)) {
            storedIds = parsed.filter(
              (id): id is string => typeof id === "string",
            );
          }
        } catch {
          storedIds = [];
        }

        const result = claimOsNotificationCandidates(
          nextNotifications,
          storedIds,
          {
            now: Date.now(),
            staleMs: OS_NOTIFICATION_STALE_MS,
            seenLimit: OS_NOTIFICATION_SEEN_LIMIT,
            displayLimit: 3,
          },
        );
        try {
          window.localStorage.setItem(
            OS_NOTIFICATION_SEEN_KEY,
            JSON.stringify(result.seenIds),
          );
        } catch {
          // Without a durable claim, displaying would repeat on every poll.
          return [];
        }
        return result.claimed;
      };

      if (!("locks" in navigator)) {
        // Without an atomic claim, multiple tabs can show the same OS toast.
        return;
      }

      let unreadNewNotifications: InAppNotification[];
      try {
        unreadNewNotifications = await navigator.locks.request(
          OS_NOTIFICATION_LOCK_NAME,
          claim,
        );
      } catch {
        return;
      }

      await Promise.allSettled(
        unreadNewNotifications.map((notification) =>
          showOsNotification(notification),
        ),
      );
    },
    [showOsNotification],
  );

  // SWR の onSuccess から最新の syncOsNotifications を呼べるよう ref を更新。
  useEffect(() => {
    syncOsNotificationsRef.current = syncOsNotifications;
  }, [syncOsNotifications]);

  useEffect(() => {
    if (!listenGlobal) return;
    const handleGlobalToggle = (event: Event) => {
      const target = (event as CustomEvent<{ target?: string; open?: boolean }>).detail
        ?.target;
      if (target && target !== "notification") return;
      const next = (event as CustomEvent<{ open?: boolean }>).detail?.open;
      if (typeof next === "boolean") {
        setPanelOpen(next);
      } else {
        setPanelOpen(!open);
      }
    };
    window.addEventListener("global-toggle-notifications", handleGlobalToggle);
    return () =>
      window.removeEventListener("global-toggle-notifications", handleGlobalToggle);
  }, [listenGlobal, open, setPanelOpen]);

  // The rail variant is portalled outside the shell DOM. Keep a local
  // capture-phase guard as a fallback for browser/portal combinations where
  // the Popover primitive cannot observe the document event directly.
  useEffect(() => {
    if (presentation !== "popover" || !open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (
        target?.closest("[data-testid='notification-popover']") ||
        target?.closest("[data-testid='notification-trigger']")
      ) {
        return;
      }
      setPanelOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setPanelOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    // Listen at the window as well as the portal's document so keyboard
    // events dispatched from a focused trigger (and test harnesses that
    // dispatch directly on window) close the card consistently.
    window.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      window.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open, presentation, setPanelOpen]);

  const requestOsNotificationPermission = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setOsNotificationPermission("unsupported");
      return;
    }
    const permission = await window.Notification.requestPermission();
    setOsNotificationPermission(permission);
    if (permission === "granted") {
      const registration = await getNotificationServiceWorkerRegistration();
      if (registration) await ensureWebPushSubscription(registration);
      await syncOsNotifications(notifications);
    }
  }, [notifications, syncOsNotifications]);

  const sendTestOsNotification = useCallback(async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (window.Notification.permission !== "granted") {
      await requestOsNotificationPermission();
      return;
    }
    await showOsNotification({
      id: `test-${Date.now()}`,
      type: "reminder",
      title: "AoiTalk notification test",
      message: "ブラウザのOS通知は有効です。",
      task_id: null,
      is_read: false,
      created_at: new Date().toISOString(),
    });
  }, [requestOsNotificationPermission, showOsNotification]);

  useEffect(() => {
    if (typeof window === "undefined" || !("Notification" in window)) {
      return;
    }

    void (async () => {
      const registration = await getNotificationServiceWorkerRegistration();
      if (registration && window.Notification.permission === "granted") {
        await ensureWebPushSubscription(registration);
      }
    })();
  }, []);

  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
      return;
    }
    const serviceWorker = navigator.serviceWorker;
    const handleServiceWorkerMessage = (event: MessageEvent) => {
      const data = event.data as {
        type?: unknown;
        openMode?: unknown;
        notificationId?: unknown;
      };
      if (
        data?.type === "aoitalk-notification-click" &&
        data.openMode === KNOWLEDGE_CAPTURE_OPEN_MODE &&
        typeof data.notificationId === "string" &&
        isUuid(data.notificationId)
      ) {
        if (!HANDLED_SERVICE_WORKER_CLICK_EVENTS.has(event)) {
          HANDLED_SERVICE_WORKER_CLICK_EVENTS.add(event);
          void openKnowledgeCaptureNotification(data.notificationId.trim());
        }
        return;
      }
      if (
        data?.type !== "aoitalk-push-delivered" ||
        typeof data.notificationId !== "string"
      ) {
        return;
      }
      // A push already displayed by the SW must not be replayed as a page
      // toast when SWR refreshes the durable inbox row.
      rememberPushedNotification(data.notificationId);
      void mutate();
    };
    serviceWorker.addEventListener(
      "message",
      handleServiceWorkerMessage,
    );
    return () =>
      serviceWorker.removeEventListener(
        "message",
        handleServiceWorkerMessage,
      );
  }, [mutate, openKnowledgeCaptureNotification]);

  useEffect(() => {
    if (!requestedOpenNotificationId) return;
    const normalizedNotificationId = requestedOpenNotificationId.trim();
    const consumedKey = normalizedNotificationId.toLowerCase();
    if (CONSUMED_OPEN_NOTIFICATION_IDS.has(consumedKey)) return;
    CONSUMED_OPEN_NOTIFICATION_IDS.add(consumedKey);
    if (CONSUMED_OPEN_NOTIFICATION_IDS.size > MAX_CONSUMED_OPEN_NOTIFICATION_IDS) {
      const oldest = CONSUMED_OPEN_NOTIFICATION_IDS.values().next().value;
      if (typeof oldest === "string") {
        CONSUMED_OPEN_NOTIFICATION_IDS.delete(oldest);
      }
    }

    void (async () => {
      try {
        await openKnowledgeCaptureNotification(normalizedNotificationId);
      } finally {
        const url = new URL(window.location.href);
        url.searchParams.delete("open_notification");
        window.history.replaceState(
          window.history.state,
          "",
          `${url.pathname}${url.search}${url.hash}`,
        );
      }
    })();
  }, [openKnowledgeCaptureNotification, requestedOpenNotificationId]);

  const unreadCount = notifications.filter((n) => !n.is_read).length;
  const ordinaryUnreadCount = notifications.filter(
    (notification) =>
      !notification.is_read && !isKnowledgeCaptureNotificationType(notification),
  ).length;

  const notificationBody = (
    <>
      {knowledgeCaptureError && (
        <div
          role="alert"
          className="mx-3 mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs"
        >
          {knowledgeCaptureError}
        </div>
      )}
      {osNotificationPermission === "denied" && (
        <div className="px-3 py-2 text-xs text-muted-foreground">
          ブラウザでOS通知がブロックされています
        </div>
      )}
      {loading && notifications.length === 0 && (
        <div className="px-4 py-3 text-center text-xs text-muted-foreground">
          読み込み中...
        </div>
      )}
      {!loading && notifications.length === 0 && (
        <div className="px-4 py-3 text-center text-xs text-muted-foreground">
          通知はありません
        </div>
      )}
      <div className="max-h-[min(24rem,60vh)] overflow-y-auto">
        {notifications.map((n) => (
          <div
            key={n.id}
            className={`border-b border-border/50 transition-colors hover:bg-accent/50 ${
              n.is_read ? "opacity-60" : ""
            }`}
          >
            <button
              type="button"
              onClick={() => void handleNotificationClick(n)}
              disabled={
                knowledgeCaptureLoadingId !== null &&
                isKnowledgeCaptureNotificationType(n)
              }
              className="w-full px-3 py-2 text-left"
            >
              <div className="flex items-start gap-2">
                {!n.is_read && (
                  <span className="mt-1.5 inline-block size-2 shrink-0 rounded-full bg-blue-500" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] font-medium text-muted-foreground uppercase">
                      {notificationTypeLabel(n.type || n.notification_type || "notification")}
                    </span>
                  </div>
                  <p className="truncate text-sm font-medium">
                    {knowledgeCaptureLoadingId === n.id ? "読み込み中…" : n.title}
                  </p>
                  {n.message && (
                    <p className="truncate text-xs text-muted-foreground">
                      {n.message}
                    </p>
                  )}
                  <span className="text-[10px] text-muted-foreground">
                    {formatRelativeTime(n.created_at)}
                  </span>
                </div>
              </div>
            </button>
            <NotificationEvidenceDetails notification={n} />
          </div>
        ))}
      </div>
    </>
  );

  const notificationActions = (
    <div className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={() => void markAllAsRead()}
        disabled={ordinaryUnreadCount === 0}
        className="p-1 rounded hover:bg-accent disabled:cursor-not-allowed disabled:opacity-40"
        title="すべて確認済みにする"
        aria-label="すべて確認済みにする"
      >
        <CheckCheck className="size-3.5" />
      </button>
      {osNotificationPermission !== "unsupported" &&
        osNotificationPermission !== "denied" && (
          <button
            type="button"
            onClick={() =>
              void (osNotificationPermission === "granted"
                ? sendTestOsNotification()
                : requestOsNotificationPermission())
            }
            className="p-1 rounded hover:bg-accent"
            title={
              osNotificationPermission === "granted"
                ? "OS通知をテスト"
                : "OS通知を許可"
            }
            aria-label={
              osNotificationPermission === "granted"
                ? "OS通知をテスト"
                : "OS通知を許可"
            }
          >
            <Bell className="size-3.5" />
          </button>
        )}
      <button
        type="button"
        onClick={() => setPanelOpen(false)}
        className="p-1 rounded hover:bg-accent"
        title="閉じる"
        aria-label="通知を閉じる"
      >
        <X className="size-3.5" />
      </button>
    </div>
  );

  const reviewDialog = (
    <KnowledgeCaptureReviewDialog
      open={reviewOpen}
      notificationId={reviewNotificationId}
      target={reviewTarget}
      candidate={reviewCandidate}
      onOpenChange={handleReviewOpenChange}
      onDidOpen={handleReviewDidOpen}
      onResolved={handleReviewResolved}
    />
  );

  if (presentation === "popover") {
    return (
      <>
        {reviewDialog}
        <Popover open={open} onOpenChange={setPanelOpen}>
          <PopoverTrigger
            render={
              <button
                type="button"
                className={triggerClassName ?? "ao-global-rail-button"}
                aria-label={`${open ? "通知を閉じる" : "通知を開く"}${unreadCount > 0 ? `（${unreadCount}件）` : ""}`}
                aria-pressed={open}
                title={`${open ? "通知を閉じる" : "通知を開く"}${unreadCount > 0 ? `（${unreadCount}件）` : ""}`}
                data-testid="notification-trigger"
              />
            }
          >
            <span className="relative">
              <Bell className="size-[17px]" />
              {unreadCount > 0 && (
                <span
                  className="absolute -right-2 -top-2 flex min-w-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[9px] font-bold leading-3 text-white"
                  aria-hidden="true"
                >
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
            </span>
          </PopoverTrigger>
          <PopoverContent
            side="right"
            align="end"
            sideOffset={8}
            className="w-[min(24rem,calc(100vw-1rem))] overflow-hidden p-0"
            positionerClassName="z-[80]"
            data-testid="notification-popover"
          >
            <div className="flex items-center justify-between gap-2 border-b px-3 py-2">
              <PopoverTitle className="text-sm font-semibold">通知</PopoverTitle>
              {notificationActions}
            </div>
            <div className="text-popover-foreground">{notificationBody}</div>
          </PopoverContent>
        </Popover>
      </>
    );
  }

  return (
    <>
      {reviewDialog}
      <SidebarGroup>
        <div className="flex items-center justify-between px-2">
          <button
            onClick={() => setPanelOpen(!open)}
            className="flex items-center gap-1.5 px-1 py-1 rounded hover:bg-accent transition-colors"
            title="通知"
          >
            <div className="relative">
              <Bell className="size-4" />
              {unreadCount > 0 && (
                <span className="absolute -top-1.5 -right-1.5 flex size-4 items-center justify-center rounded-full bg-red-500 text-[10px] font-bold text-white">
                  {unreadCount > 9 ? "9+" : unreadCount}
                </span>
              )}
            </div>
            <SidebarGroupLabel className="p-0">通知</SidebarGroupLabel>
          </button>
          {open && notificationActions}
        </div>
        {open && (
          <SidebarGroupContent>{notificationBody}</SidebarGroupContent>
        )}
      </SidebarGroup>
    </>
  );
}

export function NotificationBellPopover({
  mobileOnly = false,
  triggerClassName,
  onOpenChange,
}: NotificationBellPopoverProps) {
  const isMobile = useResolvedMobileSurface();
  if (isMobile === null || isMobile !== mobileOnly) return null;
  return (
    <NotificationPanel
      listenGlobal
      presentation="popover"
      triggerClassName={triggerClassName}
      onOpenChange={onOpenChange}
    />
  );
}
