import React,{useCallback,useEffect,useRef,useState} from 'react';
import {AppState,View,Image} from 'react-native';
import {Text} from 'react-native-paper';
import {useFocusEffect} from 'expo-router';
import type {ExpoWebGLRenderingContext,GLView as GLViewType} from 'expo-gl';
import {RigRuntime,filterPlaybackSettings,type CanvasHost,type PlaybackSettings} from '@aoitalk/seethrough-rig-runtime';
import {useRigPlaybackSettings} from './rig-playback-settings';
import {loadRigAssets} from './rig-assets';
import {sampleLip,rigConversation,rigReaction,type RigConversationState} from './rig-playback';
import {assertRigScope,type RigScope} from './rig-api';
import {createRigKeyPreviewController,isRigJitomePreviewActive,isRigKeyPreviewActive,type RigKeyPreview} from './rig-key-preview';
/** Host adapter only; the canonical runtime owns WebGL, deformation and physics. */
export function NativeRigCanvas({scope,character,settingsCharacter=character,audioCharacter=character,revision,state='idle',paused=false,expression='neutral',view='full',onCapabilities,playbackSettings,keyPreview}:{scope:RigScope;character:string;settingsCharacter?:string;audioCharacter?:string;revision:string;state?:RigConversationState;paused?:boolean;expression?:string;view?:string;playbackSettings?:PlaybackSettings;keyPreview?:RigKeyPreview;onCapabilities?:(caps:Record<string,unknown>)=>void}){
 const [NativeGLView]=useState<typeof GLViewType|null>(()=>{try{return (require('expo-gl') as typeof import('expo-gl')).GLView;}catch{return null;}});
 const [foreground,setForeground]=useState(AppState.currentState==='active'),[focused,setFocused]=useState(false),[gl,setGL]=useState<ExpoWebGLRenderingContext|null>(null),[failure,setFailure]=useState(''),[preview,setPreview]=useState<string|null>(null),[ready,setReady]=useState(false);
 const {data:savedSettings}=useRigPlaybackSettings(scope,settingsCharacter,revision);
 const playback=playbackSettings??(savedSettings?.editable?savedSettings.effective as PlaybackSettings:undefined);
 const playbackRef=useRef(playback);playbackRef.current=playback;
 const runtimeRef=useRef<RigRuntime|null>(null);
 useEffect(()=>{if(playback&&runtimeRef.current?.capabilities.playbackSettings?.length)runtimeRef.current.setPlaybackSettings(filterPlaybackSettings(playback,runtimeRef.current.capabilities));},[playback]);
 const inputs=useRef({state,paused,expression,view,keyPreview});inputs.current={state,paused,expression,view,keyPreview};
 const capsCallback=useRef(onCapabilities);capsCallback.current=onCapabilities;
 useFocusEffect(useCallback(()=>{setFocused(true);return()=>{setFocused(false);setGL(null);};},[]));
 useEffect(()=>{const sub=AppState.addEventListener('change',next=>{setForeground(next==='active');if(next!=='active')setGL(null);});return()=>sub.remove();},[]);
 const active=focused&&foreground;
 useEffect(()=>{
  if(!gl||!active)return;
  const controller=new AbortController();let runtime:RigRuntime|null=null,frame=0,previous:number|null=null,disposed=false;
  const previewController=createRigKeyPreviewController();let staticInputs:string|null=null;
  setReady(false);setFailure('');
  const stop=(message?:string)=>{if(disposed)return;disposed=true;controller.abort();if(frame)cancelAnimationFrame(frame);frame=0;runtime?.setMouth({open:0,speaking:false});runtime?.pause();runtime?.dispose();if(runtimeRef.current===runtime)runtimeRef.current=null;if(message)setFailure(message);};
  void(async()=>{
   const assets=await loadRigAssets(scope,character,revision,controller.signal);
   await assertRigScope(scope,controller.signal);if(disposed)return;setPreview(assets.localPreview);
   const canvas:CanvasHost={width:gl.drawingBufferWidth,height:gl.drawingBufferHeight,clientWidth:gl.drawingBufferWidth,clientHeight:gl.drawingBufferHeight,getContext:()=>gl};
   const created=await RigRuntime.create(canvas,assets.modelUrl,{gl,signal:controller.signal,fetch:assets.fetch,loadImage:assets.loadImage,settingsUrl:assets.settingsUrl,profile:{...assets.manifest.profile,id:'conversation-v1'},playbackSettings:playbackRef.current,manifest:assets.manifest,pixelRatio:1});
   if(disposed){created.dispose();return;}runtime=created;runtimeRef.current=created;if(playbackRef.current&&created.capabilities.playbackSettings?.length)created.setPlaybackSettings(filterPlaybackSettings(playbackRef.current,created.capabilities));capsCallback.current?.(created.capabilities as unknown as Record<string,unknown>);
   const draw=(dt:number)=>{const v=inputs.current,lip=sampleLip(audioCharacter),caps=created.capabilities;
    const supportedView=caps.views.includes(v.view)?v.view:caps.views[0];if(supportedView)created.setView(supportedView);
    const reaction=rigReaction(audioCharacter);const effectiveExpression=reaction?.expression&&caps.expressions.includes(reaction.expression)?reaction.expression:v.expression;
    created.setExpression(!isRigJitomePreviewActive(caps,v.keyPreview)&&caps.expressions.includes(effectiveExpression)?effectiveExpression:null);
    created.setConversation(lip.speaking?'speaking':rigConversation(audioCharacter)??v.state);
    const preview=previewController.apply(created,v.keyPreview,{open:lip.open,form:lip.form,speaking:lip.speaking});
    created.resize(gl.drawingBufferWidth,gl.drawingBufferHeight,1);
    if(preview.seek)created.frame(0);else created.step(dt);
    gl.flush();gl.endFrameEXP();
   };
   const tick=(now:number)=>{frame=0;if(disposed)return;try{
    const v=inputs.current,staticPreview=isRigKeyPreviewActive(created.capabilities,v.keyPreview);
    if(v.paused||staticPreview){
     const signature=JSON.stringify([v.state,v.expression,v.view,v.keyPreview,playbackRef.current]);
     if(!created.paused||signature!==staticInputs){if(created.paused)created.resume();draw(0);created.pause();staticInputs=signature;}previous=null;
    }
    else{staticInputs=null;if(created.paused)created.resume();const dt=previous===null?1/60:Math.max(0,Math.min(.05,(now-previous)/1000));previous=now;draw(dt);}frame=requestAnimationFrame(tick);
   }catch(e){stop(e instanceof Error?e.message:'GL描画を利用できません');}};
   draw(0);setReady(true);frame=requestAnimationFrame(tick);
  })().catch(e=>{if(!controller.signal.aborted)stop(e instanceof Error?e.message:'モデルを読み込めません');});
  return()=>stop();
 },[gl,active,scope.server,scope.authScope,character,audioCharacter,revision]);
 const touchWidth=useRef(1);
 const onContext=useCallback((context:ExpoWebGLRenderingContext)=>setGL(context),[]);
 return <View style={{height:240,width:'100%'}} accessibilityLabel="キャラクターアニメーション" onLayout={event=>{touchWidth.current=Math.max(1,event.nativeEvent.layout.width);}}
 onTouchMove={event=>{const current=runtimeRef.current;if(!current)return;const gaze=current.getPlaybackSettings().gaze;if(gaze.enabled&&gaze.tracking)current.setGaze(event.nativeEvent.locationX/touchWidth.current*2-1,1-event.nativeEvent.locationY/220*2);}}
 onTouchEnd={()=>{const current=runtimeRef.current;if(current?.getPlaybackSettings().gaze.tracking)current.setGaze(0,0);}}>
 {preview&&(!ready||failure||!active)&&<Image source={{uri:preview}} style={{position:'absolute',width:'100%',height:220}} resizeMode="contain"/>}
 {active&&NativeGLView&&<NativeGLView key={character+':'+revision+':'+scope.server+':'+scope.authScope} style={{height:220,width:'100%',opacity:failure?0:1}} onContextCreate={onContext}/>}
 {!NativeGLView&&<Text style={{fontSize:12}}>このアプリにはGL描画機能がありません。対応するNativeアプリが必要です。</Text>}
 {failure?<Text accessibilityLiveRegion="polite" style={{fontSize:12}}>{failure}</Text>:null}
 </View>;
}
