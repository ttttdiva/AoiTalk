import type {ReactionCue} from './reactions';

export type ConversationState = "idle" | "listening" | "thinking" | "speaking";

const states = new Map<string, { owner: object; state: ConversationState }>();
export function setRigConversation(character: string | null, owner: object, state: ConversationState) {
  if (character) states.set(character, { owner, state });
}
export function clearRigConversation(character: string | null, owner: object) {
  if (character && states.get(character)?.owner === owner) states.delete(character);
}
export function rigConversation(character: string): ConversationState | undefined { return states.get(character)?.state; }

const reactions=new Map<string,{owner:object;cue:ReactionCue}>();
export function setRigReaction(character:string,owner:object,cue:ReactionCue){reactions.set(character,{owner,cue});}
export function clearRigReaction(character:string,owner:object){if(reactions.get(character)?.owner===owner)reactions.delete(character);}
export function rigReaction(character:string){return reactions.get(character)?.cue;}
