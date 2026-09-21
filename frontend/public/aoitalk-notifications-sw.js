const KNOWLEDGE_CAPTURE_OPEN_MODE = "knowledge_capture_review";
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function knowledgeCaptureDeepLink(notificationId) {
  if (
    typeof notificationId !== "string" ||
    !UUID_RE.test(notificationId.trim())
  ) {
    return null;
  }
  const url = new URL("/chat", self.location.origin);
  url.searchParams.set("open_notification", notificationId.trim());
  return url.href;
}

function isKnowledgeCapturePush(payload, notificationId) {
  if (payload.openMode === KNOWLEDGE_CAPTURE_OPEN_MODE) return true;
  return (
    typeof notificationId === "string" &&
    UUID_RE.test(notificationId.trim()) &&
    typeof payload.candidateId === "string" &&
    UUID_RE.test(payload.candidateId.trim()) &&
    typeof payload.projectId === "string" &&
    UUID_RE.test(payload.projectId.trim())
  );
}

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    const parsed = event.data ? event.data.json() : {};
    payload = parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    payload = { body: event.data ? event.data.text() : "" };
  }

  const title = typeof payload.title === "string" ? payload.title : "AoiTalk";
  const body = typeof payload.body === "string" ? payload.body : "通知があります";
  const notificationId =
    typeof payload.notificationId === "string" ? payload.notificationId : null;
  const scheduledFor = Date.parse(String(payload.scheduledFor || ""));
  // The scheduler owns timing. A provider that wakes a service worker hours
  // late must not resurrect an old reminder as a fresh Windows toast.
  if (Number.isFinite(scheduledFor) && Date.now() - scheduledFor > 15 * 60 * 1000) {
    return;
  }

  // Older server workers identify Knowledge Capture pushes by their typed
  // UUID fields. Never use the payload URL to make this decision: it may be
  // a legacy Project Information route.
  const openMode = isKnowledgeCapturePush(payload, notificationId)
    ? KNOWLEDGE_CAPTURE_OPEN_MODE
    : null;
  const targetUrl =
    openMode === KNOWLEDGE_CAPTURE_OPEN_MODE
      ? knowledgeCaptureDeepLink(notificationId) || "/"
      : typeof payload.url === "string"
        ? payload.url
        : "/";
  const tag =
    typeof payload.tag === "string"
      ? payload.tag
      : notificationId
        ? `aoitalk-${notificationId}`
        : `aoitalk-push-${Date.now()}`;
  const options = {
    body,
    tag,
    data: {
      url: targetUrl,
      notificationId,
      ...(openMode ? { openMode } : {}),
    },
    icon: "/favicon.ico",
    requireInteraction: true,
  };

  event.waitUntil(
    (async () => {
      await self.registration.showNotification(title, options);
      if (!notificationId) return;
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      windows.forEach((client) => {
        client.postMessage({ type: "aoitalk-push-delivered", notificationId });
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const data = event.notification.data || {};
  if (data.openMode === KNOWLEDGE_CAPTURE_OPEN_MODE) {
    event.waitUntil(
      (async () => {
        const notificationId =
          typeof data.notificationId === "string"
            ? data.notificationId.trim()
            : "";
        const coldStartUrl = knowledgeCaptureDeepLink(notificationId);
        if (!coldStartUrl) return;

        const windows = await self.clients.matchAll({
          type: "window",
          includeUncontrolled: true,
        });
        for (const client of windows) {
          client.postMessage({
            type: "aoitalk-notification-click",
            openMode: KNOWLEDGE_CAPTURE_OPEN_MODE,
            notificationId,
          });
          if ("focus" in client) return client.focus();
          return;
        }
        return self.clients.openWindow(coldStartUrl);
      })(),
    );
    return;
  }

  const targetUrl = typeof data.url === "string" ? data.url : "/";
  const url = new URL(targetUrl, self.location.origin).href;

  event.waitUntil(
    (async () => {
      if (typeof data.notificationId === "string") {
        await fetch(`/api/notifications/${data.notificationId}/read`, {
          method: "POST",
          credentials: "include",
        }).catch(() => undefined);
      }

      const windows = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of windows) {
        if ("focus" in client) {
          await client.navigate(url);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })(),
  );
});
