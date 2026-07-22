export const MAX_MAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024;

type AttachmentCandidate = Pick<File, "name" | "size">;

export function isMailAttachment(file: Pick<AttachmentCandidate, "name">): boolean {
  return /\.(msg|eml)$/i.test(file.name);
}

export function isOversizedMailAttachment(file: AttachmentCandidate): boolean {
  return isMailAttachment(file) && file.size > MAX_MAIL_ATTACHMENT_BYTES;
}
