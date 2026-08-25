import { Suspense } from "react";
import { StorySettingsPage } from "@/components/story/settings/story-settings-page";

export default async function StorySettingsRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return (
    <Suspense fallback={null}>
      <StorySettingsPage workId={workId} />
    </Suspense>
  );
}
