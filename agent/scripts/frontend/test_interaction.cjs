const fs=require('fs'),vm=require('vm'),assert=require('assert');
const base=__dirname+'/../../static/js/';
const app=fs.readFileSync(base+'app.js','utf8');
const sse=fs.readFileSync(base+'sse.js','utf8');
const checks=[];
function check(name,fn){fn();checks.push(name);}
const keyboard=app.slice(app.indexOf('(function()'),app.indexOf('/* ============================================\r\n   Theme')>0?app.indexOf('/* ============================================\r\n   Theme'):app.indexOf('/* ============================================\n   Theme'));
const handlers={};let sends=0,prevents=0;
vm.runInNewContext(keyboard,{document:{getElementById:()=>({addEventListener:(k,f)=>handlers[k]=f})},sendMessage:()=>sends++});
const enter=extras=>handlers.keydown({key:'Enter',preventDefault(){prevents++},stopPropagation(){},...extras});
check('IME composition Enter never submits',()=>{handlers.compositionstart();enter({});assert.equal(sends,0);handlers.compositionend();enter({isComposing:true});enter({keyCode:229});assert.equal(sends,0);assert.equal(prevents,0)});
check('Enter sends and Shift Enter stays multiline',()=>{enter({shiftKey:true});assert.equal(sends,0);enter({});assert.equal(sends,1)});
const legacy=sse.slice(sse.indexOf('function legacyCopy('),sse.indexOf('// ── KaTeX'));
const scroll=sse.slice(sse.indexOf('function scrollToBottom()'),sse.indexOf('var scrollBtn ='));
check('static mode disables scripted smooth scrolling',()=>{
 for(const mode of ['full','reduced']){
  let behavior;const c={document:{documentElement:{dataset:{motion:mode}},getElementById:()=>({scrollHeight:100,scroll:opts=>behavior=opts.behavior})}};
  vm.runInNewContext(scroll,c);c.scrollToBottom();assert.equal(behavior,mode==='reduced'?'auto':'smooth');
 }
});
function legacyFixture(result,throws=false){let removed=false;const ctx={document:{createElement:()=>({style:{},select(){},remove(){removed=true}}),body:{appendChild(){}},execCommand:()=>{if(throws)throw Error('denied');return result}},console:{warn(){}}};vm.runInNewContext(legacy,ctx);return {run:()=>ctx.legacyCopy('test'),removed:()=>removed};}
check('copy fallback reports actual success',()=>{const f=legacyFixture(true);assert.equal(f.run(),true);assert(f.removed())});
check('copy fallback reports rejection and cleans temporary DOM',()=>{for(const f of [legacyFixture(false),legacyFixture(null,true)]){assert.equal(f.run(),false);assert(f.removed())}});
const copyHandler=sse.slice(sse.indexOf("document.addEventListener('click'"),sse.indexOf('// 剪贴板降级'));
async function copyCase(writeSucceeds,fallbackSucceeds){
 let callback;const states=new Set();const bubble={classList:{add:k=>states.add(k),remove:k=>states.delete(k)}};
 const button={dataset:{action:'copy'},closest:()=>bubble};
 const ctx={document:{addEventListener:(_,f)=>callback=f},bubbleText:()=> 'fixture',navigator:{clipboard:{writeText:()=>writeSucceeds?Promise.resolve():Promise.reject(Error('denied'))}},legacyCopy:()=>fallbackSucceeds,setTimeout(){}};
 vm.runInNewContext(copyHandler,ctx);callback({target:{closest:()=>button}});await new Promise(r=>setImmediate(r));return states;
}
(async()=>{
 const fail=await copyCase(false,false);assert(fail.has('copy-failed'));assert(!fail.has('copied'));checks.push('failed copy never displays success');
 for(const args of [[true,false],[false,true]]){const success=await copyCase(...args);assert(success.has('copied'));assert(!success.has('copy-failed'))}checks.push('native and fallback success display copied feedback');
 console.log(JSON.stringify({passed:true,checks}));
})().catch(e=>{console.error(e);process.exitCode=1});
