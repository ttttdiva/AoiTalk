import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/layout/app-header";
import {
  SharedAppShell,
} from "@/components/layout/shared-app-shell";
import { CommandPalette } from "@/components/layout/command-palette";
import { PageSwitcher } from "@/components/layout/page-switcher";
import { KeyboardShortcuts } from "@/components/layout/keyboard-shortcuts";
import { GlobalCreateTask } from "@/components/layout/global-create-task";
import { AudioPlayerBar } from "@/components/layout/audio-player-bar";
import { ShortcutsHelpDialog } from "@/components/layout/shortcuts-help-dialog";
import { GlobalAdminRestart } from "@/components/layout/global-admin-restart";
import { GlobalMemoPad } from "@/components/layout/global-memo-pad";
import { HomeTodayOverlay } from "@/components/layout/home-today-overlay";
import { TaskCompletionUndoProvider } from "@/components/tasks/task-completion-undo-provider";
import { TaskCompletionConfirmationProvider } from "@/components/tasks/task-completion-confirmation-provider";
import { DocsCommandProvider } from "@/components/docs/hooks/use-docs-command-palette";
import { DocsClipIngestProvider } from "@/components/docs/docs-clip-ingest-provider";
import { ProjectProvider } from "@/contexts/project-context";
import { ChatSessionProvider } from "@/contexts/chat-session-context";
import { AudioPlayerProvider } from "@/contexts/audio-player-context";
import { SnippetsProvider } from "@/contexts/snippets-context";
import { UserSettingsProvider } from "@/contexts/user-settings-context";
import { RuntimeProvider } from "@/contexts/runtime-context";
import { ConfirmProvider } from "@/hooks/use-confirm";
import { SwrGlobalProvider } from "@/components/providers/swr-global-provider";
import { getSession } from "@/lib/auth";

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const user = await getSession({ allowPasswordReset: true });
  const userId = user?.id ?? null;

  return (
    <SwrGlobalProvider key={userId ?? "anon"} userId={userId}>
      <ConfirmProvider>
        <TaskCompletionConfirmationProvider>
          <UserSettingsProvider userId={userId}>
          <ProjectProvider>
            <ChatSessionProvider>
              <RuntimeProvider>
                <AudioPlayerProvider>
                  <SnippetsProvider>
                    <TooltipProvider>
                      <SidebarProvider className="ao-app-shell !h-dvh !min-h-0">
                        <DocsCommandProvider>
                          <DocsClipIngestProvider>
                            <SharedAppShell
                              contextBar={<AppHeader />}
                              // Workspace pages own local navigation slots. The
                              // legacy domain sidebar is intentionally omitted so
                              // it cannot mount a second data owner.
                              legacyWorkspaceNavigation={null}
                            >
                              {children}
                            </SharedAppShell>
                            <CommandPalette />
                            <PageSwitcher />
                            <KeyboardShortcuts />
                            <GlobalCreateTask />
                            <ShortcutsHelpDialog />
                            <GlobalAdminRestart />
                            <GlobalMemoPad />
                            <HomeTodayOverlay />
                            <AudioPlayerBar />
                            <TaskCompletionUndoProvider />
                          </DocsClipIngestProvider>
                        </DocsCommandProvider>
                      </SidebarProvider>
                    </TooltipProvider>
                  </SnippetsProvider>
                </AudioPlayerProvider>
              </RuntimeProvider>
            </ChatSessionProvider>
          </ProjectProvider>
          </UserSettingsProvider>
        </TaskCompletionConfirmationProvider>
      </ConfirmProvider>
    </SwrGlobalProvider>
  );
}
