import {reactionNames,type ReactionCue,type ReactionName} from './reactions';

export type ReactionTurn={session:string;generation?:string|null;live:boolean;last?:{id:string;role:string;metadata?:Record<string,unknown>}};
/** Observe turn lifecycle once. Loading history never replays old reactions. */
export class ChatReactionTracker {
  private previous:ReactionTurn|undefined;
  private sequence=0;
  private awaitingReply=false;
  observe(turn:ReactionTurn):ReactionCue|undefined {
    const previous=this.previous;this.previous=turn;
    if(!previous || previous.session!==turn.session) {
      this.awaitingReply=turn.live;
      return !turn.last && turn.session==='new-chat' ? {id:`${turn.session}:welcome-${++this.sequence}`,kind:'greeting'} : undefined;
    }
    const fresh=turn.last?.id!==previous.last?.id;
    const completed=previous.live&&!turn.live;
    if(turn.live)this.awaitingReply=true;
    if(fresh&&turn.last?.role==='user'){this.awaitingReply=true;return {id:`${turn.session}:listen-${++this.sequence}`,kind:'listen'};}
    if(this.awaitingReply&&(completed || fresh&&!turn.live)&&turn.last?.role==='assistant') {
      this.awaitingReply=false;
      const hint=turn.last.metadata?.character_reaction;
      const expression=typeof hint==='string' && reactionNames.includes(hint as ReactionName)?hint as ReactionName:undefined;
      const error=turn.last.metadata?.generation_status==='failed' || turn.last.metadata?.status==='error' || turn.last.metadata?.is_error===true;
      if(turn.last.metadata?.generation_status==='cancelled')return;
      return {id:`${turn.session}:reply-${++this.sequence}`,kind:error?'error':'reply',expression};
    }
  }
}
