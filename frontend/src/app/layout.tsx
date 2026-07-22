import type { Metadata } from "next";
import { ThemeProvider } from "@/contexts/theme-context";
import "./globals.css";

export const metadata: Metadata = {
  title: "AoiTalk",
  description: "タスク管理 + チャットUI + エージェント",
};

// first paint 前にテーマを適用し、フルロード経路での白フラッシュを防ぐ。
// theme-context.tsx の localStorage キー "aoitalk-theme" と値("light"/"dark"/"system")に一致させる。
const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("aoitalk-theme");var d=t==="dark"||((t==="system"||!t)&&window.matchMedia("(prefers-color-scheme: dark)").matches);var e=document.documentElement;if(d){e.classList.add("dark");e.style.colorScheme="dark";}else{e.style.colorScheme="light";}}catch(_){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="antialiased h-dvh overflow-hidden">
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
