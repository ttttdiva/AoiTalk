const KNOWLEDGE_CAPTURE_OPEN_MODE = "knowledge_capture_review";
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const APP_BASE_PATH = (() => {
  try {
    const pathname = new URL(self.registration.scope).pathname.replace(/\/+$/, "");
    return pathname === "/" ? "" : pathname;
  } catch {
    return "";
  }
})();

function withAppBasePath(value) {
  if (typeof value !== "string" || !value || !value.startsWith("/") || value.startsWith("//")) {
    return value;
  }
  if (!APP_BASE_PATH || value === APP_BASE_PATH || value.startsWith(`${APP_BASE_PATH}/`)) {
    return value;
  }
  return `${APP_BASE_PATH}${value}`;
}

function sameOriginTarget(value, fallback = "/") {
  try {
    const url = new URL(typeof value === "string" ? value : fallback, self.location.origin);
    if (url.origin !== self.location.origin) return withAppBasePath(fallback);
    url.pathname = withAppBasePath(url.pathname);
    return url.href;
  } catch {
    return new URL(withAppBasePath(fallback), self.location.origin).href;
  }
}

function knowledgeCaptureDeepLink(notificationId) {
  if (
    typeof notificationId !== "string" ||
    !UUID_RE.test(notificationId.trim())
  ) {
    return null;
  }
  const url = new URL(withAppBasePath("/chat"), self.location.origin);
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
  if (Number.isFinite(scheduledFor) && Date.now() - scheduledFor > 15 * 60 * 1000) {
    return;
  }

  const openMode = isKnowledgeCapturePush(payload, notificationId)
    ? KNOWLEDGE_CAPTURE_OPEN_MODE
    : null;
  const targetUrl =
    openMode === KNOWLEDGE_CAPTURE_OPEN_MODE
      ? knowledgeCaptureDeepLink(notificationId) || sameOriginTarget("/")
      : sameOriginTarget(payload.url, "/");
  const tag =
    typeof payload.tag === "string"
      ? payload.tag
      : notificationId
        ? `at-${notificationId}`
        : `at-push-${Date.now()}`;
  const options = {
    body,
    tag,
    data: {
      url: targetUrl,
      notificationId,
      ...(openMode ? { openMode } : {}),
    },
    icon: withAppBasePath("/favicon.ico"),
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

  const url = sameOriginTarget(data.url, "/");

  event.waitUntil(
    (async () => {
      if (typeof data.notificationId === "string") {
        await fetch(withAppBasePath(`/api/notifications/${data.notificationId}/read`), {
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
