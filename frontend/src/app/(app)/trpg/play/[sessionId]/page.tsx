import { TrpgPlaySessionPage } from "@/components/trpg/play/trpg-play-session-page";

export default async function TrpgPlaySessionRoute({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  return <TrpgPlaySessionPage sessionId={sessionId} />;
}
