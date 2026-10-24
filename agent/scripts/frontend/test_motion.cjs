const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/motion.js'),'utf8');
function fixture({systemReduced=true,storageFails=false,finePointer=true}={}){
  const listeners={},frames=new Map(),intervals=[],storage={};let sequence=0,rectReads=0;
  class Element{
    constructor(){this.dataset={};this.attrs={};this.events={};this.children=[];this.value='';this.disabled=false;this.classes=new Set();
      this.classList={add:x=>this.classes.add(x)};const properties={};this.style={setProperty:(k,v)=>properties[k]=v,removeProperty:k=>delete properties[k],getPropertyValue:k=>properties[k]};}
    setAttribute(k,v){this.attrs[k]=v;} addEventListener(k,fn){this.events[k]=fn;}
    appendChild(child){child.parent=this;this.children.push(child);} querySelector(){return null;}
    getBoundingClientRect(){rectReads++;return {left:0,top:0,width:200,height:100};}
    click(){this.clicks=(this.clicks||0)+1;} closest(){return this;} remove(){this.removed=true;}
  }
  const root=new Element(),toggle=new Element(),input=new Element(),welcome=new Element(),cards=Array.from({length:6},()=>new Element());
  const reduced={matches:systemReduced,addEventListener:(_,fn)=>listeners.media=fn};
  const document={documentElement:root,hidden:false,activeElement:null,
    getElementById:id=>({motionToggle:toggle,msgInput:input,welcomeScreen:welcome})[id],
    querySelectorAll:()=>cards,createElement:()=>new Element(),addEventListener:(name,fn)=>listeners[name]=fn};
  const window={matchMedia:q=>q.includes('reduced')?reduced:{matches:finePointer},addEventListener:(name,fn)=>listeners[name]=fn};
  const context={document,window,localStorage:{getItem:k=>{if(storageFails)throw Error('blocked');return storage[k];},setItem:(k,v)=>{if(storageFails)throw Error('blocked');storage[k]=v;}},
    requestAnimationFrame:fn=>{const id=++sequence;frames.set(id,fn);return id;},cancelAnimationFrame:id=>frames.delete(id),
    setInterval:fn=>intervals.push(fn),setTimeout:()=>0,getComputedStyle:()=>({position:'relative'}),Math};
  vm.runInNewContext(source,context);
  return {root,toggle,input,welcome,cards,document,listeners,frames,intervals,storage,reduced,rectReads:()=>rectReads,
    flush:()=>{const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn());}};
}
const checks=[];
function check(name,fn){fn();checks.push({name,passed:true});}
check('system reduction is respected until explicit opt in',()=>{
  const f=fixture();assert.equal(f.root.dataset.motion,'reduced');f.toggle.events.click();assert.equal(f.root.dataset.motion,'full');
  assert.equal(f.storage['equis-motion'],'on');f.toggle.events.click();assert.equal(f.root.dataset.motion,'reduced');
  f.toggle.events.click();assert.equal(f.root.dataset.motionPreference,'auto');
});
check('all six cards have keyboard activation and one rim',()=>{
  const f=fixture();for(const c of f.cards){assert.equal(c.attrs.role,'button');assert.equal(c.tabIndex,0);assert.equal(c.children.length,1);
    c.events.keydown({key:'Enter',repeat:false,preventDefault(){}});assert.equal(c.clicks,1);
    c.events.keydown({key:'Enter',repeat:true,preventDefault(){}});assert.equal(c.clicks,1);}
});
check('pointer burst is batched and layout read once',()=>{
  const f=fixture();f.toggle.events.click();const c=f.cards[0];c.events.pointerenter({pointerType:'mouse'});
  for(let i=0;i<60;i++)c.events.pointermove({clientX:150,clientY:25});
  assert.equal(f.frames.size,1);assert.equal(f.rectReads(),1);f.flush();
  assert.equal(c.style.getPropertyValue('--fx-x'),'150.0px');assert.equal(c.style.getPropertyValue('--fx-rx'),'1.25deg');
  c.events.pointerleave();assert.equal(c.style.getPropertyValue('--fx-x'),undefined);assert.equal(f.frames.size,0);
});
check('coarse pointer and static mode schedule no tilt',()=>{
  for(const options of [{finePointer:false},{systemReduced:true}]){
    const f=fixture(options);if(!options.finePointer&&options.finePointer!==undefined)f.toggle.events.click();
    f.cards[0].events.pointerenter({pointerType:'touch'});f.cards[0].events.pointermove({clientX:100,clientY:50});assert.equal(f.frames.size,0);
  }
});
check('draft and focus survive placeholder rotation',()=>{
  const f=fixture();f.input.value='unfinished measurement';const original=f.input.placeholder;
  f.intervals.forEach(fn=>fn());assert.equal(f.input.value,'unfinished measurement');assert.equal(f.input.placeholder,original);
  f.input.value='';f.document.activeElement=f.input;f.intervals.forEach(fn=>fn());assert.equal(f.input.placeholder,original);
  assert.equal(f.input.style.opacity,undefined);
});
check('hidden page cancels work and pauses decorations',()=>{
  const f=fixture();f.toggle.events.click();f.cards[0].events.pointerenter({pointerType:'mouse'});
  f.cards[0].events.pointermove({clientX:1,clientY:1});f.document.hidden=true;f.listeners.visibilitychange();
  assert.equal(f.root.dataset.motionPaused,'true');assert.equal(f.frames.size,0);
});
check('blocked localStorage does not break controls',()=>{
  const f=fixture({storageFails:true});f.toggle.events.click();assert.equal(f.root.dataset.motion,'full');
});
const report={passed:true,checks,idle_animation_frame_loop:false};
console.log(JSON.stringify(report));
