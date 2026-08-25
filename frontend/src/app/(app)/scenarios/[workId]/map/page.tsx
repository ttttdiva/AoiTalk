import { StoryMapPage } from "@/components/story/map/story-map-page";

export default async function StoryMapRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return <StoryMapPage workId={workId} />;
}
