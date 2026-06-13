"use client";

export const CHAT_SESSION_NAVIGATION_EVENT = "aoitalk:chat-session-navigation";

export function readChatSessionIdFromLocation() {
  if (typeof window === "undefined") return null;
  return new URL(window.location.href).searchParams.get("s") || null;
}

export function navigateChatSessionInPlace(href: string) {
  if (typeof window === "undefined") return false;

  const target = new URL(href, window.location.origin);
  if (
    target.origin !== window.location.origin ||
    target.pathname !== "/chat" ||
    window.location.pathname !== "/chat"
  ) {
    return false;
  }

  const nextHref = `${target.pathname}${target.search}${target.hash}`;
  const currentHref = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (currentHref === nextHref) return true;

  window.history.pushState(null, "", nextHref);
  window.dispatchEvent(
    new CustomEvent(CHAT_SESSION_NAVIGATION_EVENT, {
      detail: {
        href: nextHref,
        sessionId: target.searchParams.get("s") || null,
      },
    }),
  );
  return true;
}
