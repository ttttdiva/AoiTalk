import { AppsWorkspaceShell } from "@/components/apps/apps-workspace-shell";

export default function AppsLayout({ children }: { children: React.ReactNode }) {
  return <AppsWorkspaceShell>{children}</AppsWorkspaceShell>;
}
