export const reactionNames = ['neutral','happy','surprised','worried','shy'] as const;
export type ReactionName = typeof reactionNames[number];
export type ReactionCue = { id: string; kind: 'listen' | 'reply' | 'error' | 'greeting'; expression?: ReactionName };
