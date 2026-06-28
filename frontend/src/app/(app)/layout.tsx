import { TooltipProvider } from "@/components/ui/tooltip";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/layout/app-header";
import { CommandPalette } from "@/components/layout/command-palette";
import { KeyboardShortcuts } from "@/components/layout/keyboard-shortcuts";
import { GlobalCreateTask } from "@/components/layout/global-create-task";
import { AudioPlayerBar } from "@/components/layout/audio-player-bar";
import { ShortcutsHelpDialog } from "@/components/layout/shortcuts-help-dialog";
import { GlobalAdminRestart } from "@/components/layout/global-admin-restart";
import { GlobalMemoPad } from "@/components/layout/global-memo-pad";
import { HomeTodayOverlay } from "@/components/layout/home-today-overlay";
import { TaskCompletionUndoProvider } from "@/components/tasks/task-completion-undo-provider";
import { ProjectProvider } from "@/contexts/project-context";
import { ChatSessionProvider } from "@/contexts/chat-session-context";
import { AudioPlayerProvider } from "@/contexts/audio-player-context";
import { SnippetsProvider } from "@/contexts/snippets-context";
import { UserSettingsProvider } from "@/contexts/user-settings-context";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProjectProvider>
      <ChatSessionProvider>
        <UserSettingsProvider>
          <AudioPlayerProvider>
            <SnippetsProvider>
              <TooltipProvider>
                <SidebarProvider className="ao-app-shell !h-dvh !min-h-0">
                  <AppSidebar />
                  <SidebarInset className="ao-main-panel min-h-0 overflow-hidden bg-transparent">
                    <AppHeader />
                    <main className="ao-main-scroll flex-1 min-w-0 min-h-0 overflow-auto">
                      {children}
                    </main>
                  </SidebarInset>
                  <CommandPalette />
                  <KeyboardShortcuts />
                  <GlobalCreateTask />
                  <ShortcutsHelpDialog />
                  <GlobalAdminRestart />
                  <GlobalMemoPad />
                  <HomeTodayOverlay />
                  <AudioPlayerBar />
                  <TaskCompletionUndoProvider />
                </SidebarProvider>
              </TooltipProvider>
            </SnippetsProvider>
          </AudioPlayerProvider>
        </UserSettingsProvider>
      </ChatSessionProvider>
    </ProjectProvider>
  );
}
