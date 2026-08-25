import { StoryReviewPage } from "@/components/story/review/story-review-page";

export default async function StoryReviewRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return <StoryReviewPage workId={workId} />;
}
