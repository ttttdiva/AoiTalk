import type { Metadata } from "next";
import { headers } from "next/headers";
import { ThemeProvider } from "@/contexts/theme-context";
import {
  resolveBrowserBranding,
} from "@/lib/browser-branding";
import {
  browserBasePathBootstrap,
  withBasePath,
} from "@/lib/base-path";
import { BrowserBrandingGuard } from "@/components/layout/browser-branding-guard";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const branding = resolveBrowserBranding(await headers());
  return {
    title: branding?.title ?? "AoiTalk",
    description: "タスク管理 + チャットUI + エージェント",
    icons: {
      icon: branding?.icon ?? withBasePath("/favicon.ico"),
    },
  };
}

// first paint 前にテーマを適用し、フルロード経路での白フラッシュを防ぐ。
// theme-context.tsx の localStorage キーと値("light"/"dark"/"system")に一致させる。
const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("aoitalk-theme");var d=t==="dark"||((t==="system"||!t)&&window.matchMedia("(prefers-color-scheme: dark)").matches);var e=document.documentElement;if(d){e.classList.add("dark");e.style.colorScheme="dark";}else{e.style.colorScheme="light";}}catch(_){}})();`;
const BASE_PATH_INIT_SCRIPT = browserBasePathBootstrap();

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const browserBranding = resolveBrowserBranding(await headers());
  return (
    <html lang="ja" suppressHydrationWarning>
      <head>
        {BASE_PATH_INIT_SCRIPT ? (
          <script
            id="public-base-path-bootstrap"
            dangerouslySetInnerHTML={{ __html: BASE_PATH_INIT_SCRIPT }}
          />
        ) : null}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="antialiased h-dvh overflow-hidden">
        {browserBranding?.kind === "canonical-portal" ? (
          <BrowserBrandingGuard />
        ) : null}
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
