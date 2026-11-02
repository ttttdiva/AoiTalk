"use client";

import { useEffect, useRef, useState } from "react";

import {
  explorerDownloadResource,
  explorerErrorMessage,
} from "@/lib/explorer-api";
import { readFileTransferCompatibilityPreference } from "@/lib/file-transfer-preference";

type AppDownloadLinkProps = Omit<
  React.AnchorHTMLAttributes<HTMLAnchorElement>,
  "href" | "onClick"
> & {
  href: string;
  fallbackFilename?: string;
};

/**
 * Keep the normal anchor path when compatibility mode is disabled.  When it
 * is enabled, the authenticated transfer helper owns the click and exposes a
 * real failure instead of falling through to an unauthenticated navigation.
 */
export function AppDownloadLink({
  href,
  fallbackFilename,
  children,
  ...anchorProps
}: AppDownloadLinkProps) {
  const [error, setError] = useState<string | null>(null);
  const activeController = useRef<AbortController | null>(null);

  useEffect(() => () => {
    activeController.current?.abort();
    activeController.current = null;
  }, []);

  const handleClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (!readFileTransferCompatibilityPreference()) return;
    event.preventDefault();
    activeController.current?.abort();
    const controller = new AbortController();
    activeController.current = controller;
    setError(null);
    void explorerDownloadResource(href, fallbackFilename, { signal: controller.signal })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(explorerErrorMessage(cause));
      })
      .finally(() => {
        if (activeController.current === controller) activeController.current = null;
      });
  };

  return (
    <>
      <a
        {...anchorProps}
        href={href}
        download={fallbackFilename?.trim() || anchorProps.download}
        onClick={handleClick}
        data-app-download-link="true"
      >
        {children}
      </a>
      {error && <span role="alert" className="w-full break-words text-xs text-destructive">{error}</span>}
    </>
  );
}
