export const MAX_MAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024;
export const MAX_CHAT_ATTACHMENTS = 12;
export const MAX_CHAT_ATTACHMENT_BYTES = 100 * 1024 * 1024;

type AttachmentCandidate = Pick<File, "name" | "size">;

export function isMailAttachment(file: Pick<AttachmentCandidate, "name">): boolean {
  return /\.(msg|eml)$/i.test(file.name);
}

export function isOversizedMailAttachment(file: AttachmentCandidate): boolean {
  return isMailAttachment(file) && file.size > MAX_MAIL_ATTACHMENT_BYTES;
}

export function chatAttachmentError(files: readonly Pick<File, 'name' | 'size' | 'type'>[]): string | null {
  if (files.length > MAX_CHAT_ATTACHMENTS) return '添付は合計12件までです';
  if (files.reduce((total, file) => total + file.size, 0) > MAX_CHAT_ATTACHMENT_BYTES) return '添付は合計100 MBまでです';
  for (const file of files) {
    if (file.size > 50 * 1024 * 1024) return 'ファイルは1件50 MBまでです';
    if (file.type.startsWith('image/') && file.size > 10 * 1024 * 1024) return '画像は1枚10 MBまでです';
    if ((file.type.startsWith('audio/') || /\.(wav|mp3|m4a|flac|ogg)$/i.test(file.name)) && file.size > 25 * 1024 * 1024) return '音声は1件25 MBまでです';
    if (isOversizedMailAttachment(file)) return 'メールファイルは 10 MB までです';
  }
  return null;
}
