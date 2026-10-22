"use client";
import {useEffect,useRef} from 'react';
import {ChatReactionTracker,type ReactionTurn} from '@/lib/aoirig/chat-reactions';
import {clearRigReaction,setRigReaction} from '@/lib/aoirig/conversation';

export function useChatReaction(turn:ReactionTurn,character:string|null|undefined) {
 const tracker=useRef(new ChatReactionTracker());
 const owner=useRef({});
 const {session,generation,live,last}=turn;
 useEffect(()=>{
  tracker.current=new ChatReactionTracker();const token=owner.current;
  return ()=>{if(character)clearRigReaction(character,token)};
 },[character,session]);
 useEffect(()=>{
  const next=tracker.current.observe({session,generation,live,last});
  if(next&&character)setRigReaction(character,owner.current,next);
 },[session,generation,live,last,character]);
}
