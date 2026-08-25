import { StoryRulesPage } from "@/components/story/rules/story-rules-page";

export default async function StoryRulesRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return <StoryRulesPage workId={workId} />;
}
