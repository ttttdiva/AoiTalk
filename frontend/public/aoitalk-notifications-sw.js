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

  const targetUrl = typeof payload.url === "string" ? payload.url : "/";
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

      const windows = await clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of windows) {
        if ("focus" in client) {
          await client.navigate(url);
          return client.focus();
        }
      }
      return clients.openWindow(url);
    })(),
  );
});
