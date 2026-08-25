import { StoryCastPage } from "@/components/story/cast/story-cast-page";

export default async function StoryCastRoute({ params }: { params: Promise<{ workId: string }> }) {
  const { workId } = await params;
  return <StoryCastPage workId={workId} />;
}
