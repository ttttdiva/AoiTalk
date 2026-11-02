import { performAdminRestart } from "@/components/layout/global-admin-restart";

document.querySelector<HTMLButtonElement>("[data-restart-once]")?.addEventListener("click", () => {
  void performAdminRestart();
});

document.querySelector<HTMLButtonElement>("[data-restart-twice]")?.addEventListener("click", () => {
  void performAdminRestart();
  void performAdminRestart();
});

document.querySelector<HTMLButtonElement>("[data-replace-screen]")?.addEventListener("click", () => {
  document.querySelector("[data-restart-overlay]")?.remove();
  const screen = document.querySelector<HTMLElement>("[data-new-screen]");
  if (screen) screen.hidden = false;
});
