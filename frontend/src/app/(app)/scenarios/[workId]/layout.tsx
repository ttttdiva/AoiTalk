import { StoryWorkspaceShell } from "@/components/story/shell/story-workspace-shell";

export default async function StoryWorkLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ workId: string }>;
}) {
  const { workId } = await params;
  return <StoryWorkspaceShell workId={workId}>{children}</StoryWorkspaceShell>;
}
