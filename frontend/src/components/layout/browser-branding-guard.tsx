"use client";

import { useEffect } from "react";
import {
  CANONICAL_PORTAL_ICON,
  CANONICAL_PORTAL_TITLE,
} from "@/lib/browser-branding";

function enforceCanonicalBranding(head: HTMLHeadElement): void {
  if (document.title !== CANONICAL_PORTAL_TITLE) {
    document.title = CANONICAL_PORTAL_TITLE;
  }

  const iconLinks = Array.from(head.querySelectorAll<HTMLLinkElement>("link[rel]"))
    .filter((link) => link.rel.toLowerCase().split(/\s+/u).includes("icon"));
  if (iconLinks.length === 0) {
    const icon = document.createElement("link");
    icon.rel = "icon";
    icon.href = CANONICAL_PORTAL_ICON;
    head.appendChild(icon);
    return;
  }

  for (const icon of iconLinks) {
    if (icon.getAttribute("href") !== CANONICAL_PORTAL_ICON) {
      icon.setAttribute("href", CANONICAL_PORTAL_ICON);
    }
  }
}

export function BrowserBrandingGuard() {
  useEffect(() => {
    const head = document.head;
    if (!head) return;

    enforceCanonicalBranding(head);
    const observer = new MutationObserver(() => enforceCanonicalBranding(head));
    observer.observe(head, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["href", "rel"],
    });
    return () => observer.disconnect();
  }, []);

  return null;
}
