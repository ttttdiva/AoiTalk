export type StoryAssistFieldKind =
  | "episode_body"
  | "episode_plot"
  | "work_plot"
  | "character_description"
  | "character_summary"
  | "character_role_note"
  | "character_notes"
  | "rulebook"
  | "world_note";

export type StoryAssistSelection = {
  start: number;
  end: number;
  text: string;
};

export type StoryAssistTarget = {
  fieldKind: StoryAssistFieldKind;
  fieldLabel: string;
  workId?: string;
  episodeId?: string;
  characterId?: string;
  rulebookId?: string;
  noteId?: string;
  getCurrentText: () => string;
  getSelection?: () => StoryAssistSelection | null;
  requiresNotesConfirmation?: boolean;
  /** episode_body 適用時の etag（選択範囲修正など revise ジョブ以外） */
  getBodyEtag?: () => string | undefined;
};

export type StoryAssistApplyContext = {
  target: StoryAssistTarget;
  proposal: string;
  selection: StoryAssistSelection | null;
  nextText: string;
};
