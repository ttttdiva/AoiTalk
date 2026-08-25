import { StoryManuscriptPage } from "@/components/story/manuscript/story-manuscript-page";

export default async function StoryManuscriptRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return <StoryManuscriptPage workId={workId} />;
}
