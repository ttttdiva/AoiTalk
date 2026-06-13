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
