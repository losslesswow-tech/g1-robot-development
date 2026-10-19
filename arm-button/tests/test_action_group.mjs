import assert from 'node:assert/strict';
import {test} from 'node:test';
import {model} from '../web/model.mjs';
import {hand} from '../web/hand-geometry.mjs';
import {rad,forward,solveHeight} from '../web/kinematics.mjs';
import {buildActionGroup,sampleActionGroup} from '../web/action-group.mjs';
const config={shoulderHeight:1.1,shoulderTopHeight:1.1,shoulderTopToJoint:0,targetHeight:1.3,tipOffset:hand.tip,endpoint:'official_ftp',fingerBend:0};
const reference=[.294,-.206,.043,.986,-.125,.079,.056];
const ready=solveHeight(model,reference,config).q;
const options={gapM:.02,strokeM:.005,holdS:.5,previewRateRadS:rad(10)};
const build=()=>buildActionGroup(model,reference,ready,config,options);
test('straight fingertip approach and press preserve height and lateral location',()=>{
  const plan=build(),start=forward(model,ready,config),all=plan.stages.filter(s=>['approach','press'].includes(s.id));
  let previousX=start.tip[0];
  for(const stage of all)for(const q of stage.path){
    const p=forward(model,q,config);
    assert.ok(Math.abs(p.tip[1]-start.tip[1])<.00016);
    assert.ok(Math.abs(p.tip[2]-start.tip[2])<.00016);
    assert.ok(p.tip[0]>=previousX-.00016);previousX=p.tip[0];
    assert.ok(Math.hypot(...p.direction.map((v,i)=>v-start.direction[i]))<.0016);
  }
  assert.ok(Math.abs(previousX-start.tip[0]-.025)<.00016);
});
test('withdrawal reverses the pressing path before lowering and returns exactly home',()=>{
  const plan=build(),[lift,approach,press,hold,retract,lower]=plan.stages;
  assert.deepEqual(retract.path,[...approach.path,...press.path.slice(1)].reverse());
  assert.deepEqual(retract.path.at(-1),ready);
  assert.deepEqual(lower.path[0],ready);assert.deepEqual(lower.path.at(-1),reference);
  assert.deepEqual(sampleActionGroup(plan,plan.totalDurationS+1).q,reference);
  for(let i=1;i<plan.stages.length;i++)assert.deepEqual(plan.stages[i-1].path.at(-1),plan.stages[i].path[0]);
});
test('entire preview stays within URDF limits and chosen angular rate',()=>{
  const plan=build(),dt=.002;let previous=sampleActionGroup(plan,0).q;
  for(let t=dt;t<=plan.totalDurationS;t+=dt){
    const q=sampleActionGroup(plan,t).q;forward(model,q,config);
    q.forEach((v,i)=>assert.ok(Math.abs(v-previous[i])/dt<=options.previewRateRadS+1e-6));previous=q;
  }
});
test('model preview cannot claim physical press success or navigation readiness',()=>{
  const plan=build();assert.equal(plan.mode,'offline');assert.equal(plan.execution_authorized,false);
  assert.equal(plan.calibration_verified,false);assert.equal(plan.physical_retry_blocked,true);
  assert.ok(plan.omittedChecks.includes('actual button activation'));
  assert.equal(plan.button.source.includes('not a measured'),true);
  const old=plan.stages[0].path[0][0];reference[0]+=.001;
  assert.equal(plan.stages[0].path[0][0],old);reference[0]-=.001;
});
test('rejects incorrect ready height, invalid parameters and non-finite time',()=>{
  assert.throws(()=>buildActionGroup(model,reference,reference,config,options),/目标高度/);
  for(const change of [{gapM:NaN},{strokeM:-.005},{strokeM:.1},{holdS:0},{previewRateRadS:0}])assert.throws(()=>buildActionGroup(model,reference,ready,config,{...options,...change}));
  assert.throws(()=>sampleActionGroup(build(),NaN));
});
