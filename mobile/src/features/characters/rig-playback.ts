export type RigConversationState='idle'|'listening'|'thinking'|'speaking';
export type RigReaction={expression?:string};
const conversations=new Map<string,{owner:object;state:RigConversationState}>();
const reactions=new Map<string,{owner:object;cue:RigReaction;until:number}>();
export function setRigConversation(character:string,owner:object,state:RigConversationState){conversations.set(character,{owner,state});}
export function rigConversation(character:string){return conversations.get(character)?.state;}
export function setRigReaction(character:string,owner:object,cue:RigReaction){reactions.set(character,{owner,cue,until:Date.now()+2200});}
export function rigReaction(character:string){const entry=reactions.get(character);if(entry&&Date.now()>=entry.until){reactions.delete(character);return undefined;}return entry?.cue;}
export function clearRigState(character:string,owner:object){if(conversations.get(character)?.owner===owner)conversations.delete(character);if(reactions.get(character)?.owner===owner)reactions.delete(character);}
export type NativeViseme={start:number;end:number;open:number;shape:number};
type Meter={sampleRate:number;channels:number;samples:Float32Array};
const players=new Map<string,{owner:object;meter:Meter|null;position:number;running:boolean;visemes:NativeViseme[]}>();
/** Decode PCM WAV solely for RMS, retaining the existing expo-av transport. */
export function decodeWaveMeter(encoded:string):Meter|null{
 if(encoded.length>12*1024*1024)return null;
 try{const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/',bytes:number[]=[];let bits=0,value=0;for(const ch of encoded){if(ch==='=')break;const digit=alphabet.indexOf(ch);if(digit<0)continue;value=(value<<6)|digit;bits+=6;if(bits>=8){bits-=8;bytes.push((value>>bits)&255);}}
 const data=Uint8Array.from(bytes),view=new DataView(data.buffer),tag=(i:number)=>String.fromCharCode(...data.slice(i,i+4));if(tag(0)!=='RIFF'||tag(8)!=='WAVE')return null;
 let format=0,channels=0,sampleRate=0,bitDepth=0,offset=0,length=0;
 for(let i=12;i+8<=data.length;){const size=view.getUint32(i+4,true),start=i+8;if(start+size>data.length)return null;if(tag(i)==='fmt '&&size>=16){format=view.getUint16(start,true);channels=view.getUint16(start+2,true);sampleRate=view.getUint32(start+4,true);bitDepth=view.getUint16(start+14,true);}if(tag(i)==='data'){offset=start;length=size;}i=start+size+(size%2);}
 if(channels<1||channels>8||sampleRate<8000||sampleRate>192000||!offset||!((format===1&&bitDepth===16)||(format===3&&bitDepth===32)))return null;
 const stride=bitDepth/8,samples=new Float32Array(Math.floor(length/stride));for(let i=0;i<samples.length;i++)samples[i]=format===1?view.getInt16(offset+i*stride,true)/32768:view.getFloat32(offset+i*stride,true);return {channels,sampleRate,samples};
 }catch{return null;}
}
export function observeRigPlayback(character:string,owner:object,meter:Meter|null,visemes:NativeViseme[]=[]){const playback={owner,meter,visemes,position:0,running:false};players.set(character,playback);return {update(positionMillis:number,isPlaying:boolean){if(players.get(character)===playback){playback.position=Math.max(0,positionMillis/1000);playback.running=isPlaying;}},dispose(){if(players.get(character)===playback)players.delete(character);}};}
export function sampleLip(character:string){const p=players.get(character);if(!p?.running)return {open:0,form:0,speaking:false};if(p.visemes.length){const v=p.visemes.find(v=>p.position>=v.start&&p.position<v.end);return {open:v?.open??0,form:v?.shape??0,speaking:true};}if(!p.meter)return {open:0,form:0,speaking:true};const {samples,sampleRate,channels}=p.meter,offset=Math.floor(p.position*sampleRate)*channels;let sum=0,count=0;for(let i=offset;i<Math.min(samples.length,offset+512*channels);i+=channels){sum+=samples[i]*samples[i];count++;}return {open:Math.min(1,Math.max(0,(Math.sqrt(sum/Math.max(1,count))-.012)*7)),form:0,speaking:true};}
