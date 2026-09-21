import { apiFetch as defaultApiFetch } from "./docs-utils";

export type MeetingImportRole = "minutes" | "memo";

export type MeetingImportSubmission = {
  file: File;
  role: MeetingImportRole;
  title?: string;
  meetingDate?: string;
  participants?: string[];
};

export type MeetingImportNode = {
  id: string;
  title?: string;
  [key: string]: unknown;
};

export type MeetingImportResponse = {
  action: "create" | "duplicate_skip";
  status?: "created" | "duplicate_skip";
  duplicate_skip?: boolean;
  node?: MeetingImportNode;
  node_id: string;
  idempotency_key: string;
  meeting_import?: Record<string, unknown>;
  [key: string]: unknown;
};

export type MeetingImportApiFetch = <T>(
  path: string,
  init?: RequestInit,
) => Promise<T>;

/** The only file types accepted by the completed meeting import contract. */
export function isMeetingImportFile(
  file: Pick<File, "name"> | null | undefined,
): file is File {
  if (!file || typeof file.name !== "string") return false;
  return /\.(?:md|txt)$/iu.test(file.name.trim());
}

/** Filter a DataTransfer/FileList without ever routing files through ClipIngest. */
export function filterMeetingImportFiles(files: Iterable<File>): File[] {
  return Array.from(files).filter((file) => isMeetingImportFile(file));
}

export function buildMeetingImportFormData(
  submission: MeetingImportSubmission,
): FormData {
  const form = new FormData();
  form.append("file", submission.file, submission.file.name);
  form.append("role", submission.role);
  const title = submission.title?.trim();
  if (title) form.append("title", title);
  const meetingDate = submission.meetingDate?.trim();
  if (meetingDate) form.append("meeting_date", meetingDate);
  const participants = (submission.participants ?? [])
    .map((participant) => participant.trim())
    .filter(Boolean);
  if (participants.length > 0) {
    form.append("participants", JSON.stringify(participants));
  }
  return form;
}

export async function submitMeetingImport(
  submission: MeetingImportSubmission,
  fetcher: MeetingImportApiFetch = defaultApiFetch,
): Promise<MeetingImportResponse> {
  if (!isMeetingImportFile(submission.file)) {
    throw new Error(".md または .txt ファイルを指定してください");
  }
  return fetcher<MeetingImportResponse>("/api/docs/meeting-import", {
    method: "POST",
    headers: {},
    body: buildMeetingImportFormData(submission),
  });
}
