export interface OsNotificationCandidate {
  id: string;
  is_read: boolean;
  created_at: string;
  delivered_at?: string | null;
}

function notificationTimestamp(
  notification: OsNotificationCandidate,
  fallbackNow: number,
): number {
  const raw = notification.delivered_at || notification.created_at;
  const time = new Date(raw).getTime();
  return Number.isFinite(time) ? time : fallbackNow;
}

export function isFreshUnreadOsNotification(
  notification: OsNotificationCandidate,
  now: number,
  staleMs: number,
): boolean {
  return (
    !notification.is_read &&
    notificationTimestamp(notification, now) >= now - staleMs
  );
}

export function claimOsNotificationCandidates<T extends OsNotificationCandidate>(
  notifications: T[],
  previouslySeenIds: Iterable<string>,
  options: {
    now: number;
    staleMs: number;
    seenLimit: number;
    displayLimit: number;
  },
): { seenIds: string[]; claimed: T[] } {
  const seenIds = new Set(previouslySeenIds);
  const staleBefore = options.now - options.staleMs;
  const claimed: T[] = [];
  const protectedIds = new Set(
    notifications
      .filter((notification) =>
        isFreshUnreadOsNotification(
          notification,
          options.now,
          options.staleMs,
        ),
      )
      .map((notification) => notification.id),
  );

  for (const notification of [...notifications].sort(
    (a, b) =>
      notificationTimestamp(a, options.now) -
      notificationTimestamp(b, options.now),
  )) {
    if (notificationTimestamp(notification, options.now) < staleBefore) {
      seenIds.add(notification.id);
      continue;
    }
    if (!notification.is_read && !seenIds.has(notification.id)) {
      seenIds.add(notification.id);
      claimed.push(notification);
    }
  }

  const protectedSeenIds = Array.from(seenIds).filter((id) =>
    protectedIds.has(id),
  );
  const historicalSeenIds = Array.from(seenIds)
    .filter((id) => !protectedIds.has(id))
    .slice(-options.seenLimit);

  return {
    seenIds: [...historicalSeenIds, ...protectedSeenIds],
    claimed: claimed.slice(-options.displayLimit),
  };
}
