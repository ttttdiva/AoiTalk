/** Host scheduling only. Simulation and GL drawing belong to the canonical runtime. */
export class RigFrameLoop {
 private id=0; private previous:number|null=null;private disposed=false; private active=false;
 constructor(private frame:(dt:number)=>void,private pause:()=>void,private resume:()=>void,private onError:(error:unknown)=>void,private request=(cb:FrameRequestCallback)=>requestAnimationFrame(cb),private cancel=(id:number)=>cancelAnimationFrame(id)){}
 setActive(active:boolean){
  if(this.disposed)return;
  this.active=active;
  if(!active){if(this.id)this.cancel(this.id);this.id=0;this.previous=null;this.pause();return;}
  if(!this.id){this.resume();this.id=this.request(this.tick);}
 }
 private tick=(now:number)=>{this.id=0;if(this.disposed||!this.active)return;const dt=this.previous===null?1/60:Math.min(.05,Math.max(0,(now-this.previous)/1000));this.previous=now;
  try{this.frame(dt);if(!this.disposed&&this.active)this.id=this.request(this.tick);}catch(error){this.dispose();this.onError(error);}
 };
 dispose(){if(this.disposed)return;this.setActive(false);this.disposed=true;}
}
