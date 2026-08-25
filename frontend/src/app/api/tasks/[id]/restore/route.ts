import { NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { fetchPythonApi } from "@/lib/server/python-api-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await getSession();
  if (!user) {
    return NextResponse.json({ detail: "認証が必要です" }, { status: 401 });
  }
  const { id } = await params;
  const body = await request.text().catch(() => "");
  let response: Response;
  try {
    response = await fetchPythonApi(
      `/api/tasks/${encodeURIComponent(id)}/restore`,
      {
        method: "POST",
        user,
        headers: { "content-type": "application/json" },
        body: body || "{}",
      },
    );
  } catch (error) {
    console.error("正規タスク復元APIへの接続に失敗しました:", error);
    return NextResponse.json(
      { detail: "正規タスク復元サービスに接続できません" },
      { status: 502 },
    );
  }
  const responseBody = await response.text().catch(() => "");
  return new NextResponse(responseBody || null, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/json",
    },
  });
}
