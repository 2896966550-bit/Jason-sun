const fs=require('fs'), vm=require('vm'), assert=require('assert');
const base=__dirname+'/../../static/js/';
const context={document:{getElementById:()=>null,addEventListener(){}},Number,Event:function(){}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(base+'composer.js','utf8'),context);
vm.runInContext(fs.readFileSync(base+'cards.js','utf8'),context);
let checks=[];function check(name,fn){fn();checks.push(name)}
const values={mode:'consistency',measured:'10.03',reference:'10',precision:'0.01',temperature:'20'};
check('empty numeric input never becomes an observation',()=>{
 for(const key of ['measured','reference','precision','temperature'])for(const raw of ['', '  ',null])assert.equal(context.measurementDraft({...values,[key]:raw}).field,key);
});
check('nonfinite malformed and dimensioned values rejected',()=>{
 for(const raw of ['NaN','Infinity','1e999','10mm','0x10','<img src=x>','1,2'])assert.equal(context.measurementDraft({...values,measured:raw}).field,'measured');
});
check('physical domains validated without default observations',()=>{
 assert.equal(context.measurementDraft({...values,precision:'-0.01'}).field,'precision');
 assert.equal(context.measurementDraft({...values,temperature:'-273.16'}).field,'temperature');
 assert(!context.measurementDraft({...values,temperature:'-273.15'}).error);
});
check('reference is omitted only for uncertainty-only request',()=>{
 const d=context.measurementDraft({...values,mode:'uncertainty',reference:''});assert(d.text.includes('不确定度预算'));assert(!d.text.includes('参考值'));
 assert(context.measurementDraft({...values,reference:''}).error);
});
check('valid signed and scientific inputs keep explicit canonical units',()=>{
 const d=context.measurementDraft({...values,measured:'−1.2e-3',temperature:'-5'});assert(d.text.includes('实测值-1.2e-3mm'));assert(d.text.includes('环境温度-5°C'));assert(d.text.includes('参考一致性'));
});
check('invalid mode cannot silently select a task',()=>assert(context.measurementDraft({...values,mode:'other'}).error));
check('preparing a request preserves existing draft and never submits',()=>{
 let changed=0,focused=0;const input={value:'已有说明',dispatchEvent(){changed++},focus(){focused++},setSelectionRange(){}};
 context.document.getElementById=()=>input;context.autoResize=()=>{};context.navigateTo=()=>{};
 context.sendMessage=()=>{throw Error('must not submit')};context.prepareDraft('新参数');assert.equal(input.value,'已有说明\n\n新参数');assert.equal(changed,1);assert.equal(focused,1);
});
const valid={execution_status:'success',measured_value:10,reference_value:10,deviation:0,U:0.01,k:2,u_a:0.004,u_b:0.003,u_ood:0,u_c:0.005,verdict:'pass'};
check('missing and invalid results never acquire success styling or fake metrics',()=>{
 for(const data of [{execution_status:'needs_input',error:{message:'missing'}},{...valid,U:NaN},{...valid,reference_value:null},{...valid,verdict:'invented'}]){
  const html=context.renderComplianceCard(data);assert(html.includes('compliance-card unavailable'));assert(!html.includes('result-metrics'));assert(!html.includes('NaN'));assert(html.includes('下一步'));
 }
});
check('three-state conclusions stay distinct with numerical values unchanged',()=>{
 for(const verdict of ['pass','fail','indeterminate']){const html=context.renderComplianceCard({...valid,verdict});assert(html.includes('compliance-card '+verdict));assert(html.includes('10.0000'));assert(html.includes('0.0100'));assert(html.includes('<details'));assert(html.includes('data-card-detail'));assert(!html.includes('role="button"'));}
});
check('uncalibrated visual score cannot render a normal or anomalous verdict',()=>{
 const html=context.renderAnomalyCard({execution_status:'success',success:true,anomaly_score:-3,class_used:'bottle',decision_execution_status:'needs_calibration',threshold:null,is_anomaly:null});assert(html.includes('anomaly-card neutral'));assert(html.includes('待标定'));assert(!html.includes('anomaly-card pass'));assert(!html.includes('anomaly-card fail'));assert(html.includes('-3.0000'));
});
check('result labels and backend messages are escaped',()=>{
 const attack='<img src=x onerror=alert(1)>';const html=context.renderComplianceCard({...valid,reason:attack,event_id:attack});assert(!html.includes(attack));assert(html.includes('&lt;img'));const anomaly=context.renderAnomalyCard({execution_status:'success',success:true,anomaly_score:1,class_used:attack,is_anomaly:false});assert(!anomaly.includes(attack));
});
check('uncertainty-only result does not show fabricated reference or deviation',()=>{
 const html=context.renderComplianceCard({...valid,assessment_type:'uncertainty_only',reference_value:null,deviation:null});assert(html.includes('不确定度预算估计'));assert(!html.includes('偏差 d'));assert(!html.includes('result-metric"><span>参考值'));
});
console.log(JSON.stringify({passed:true,checks}));
