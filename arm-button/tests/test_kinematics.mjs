import assert from 'node:assert/strict';
import {test} from 'node:test';
import {model} from '../web/model.mjs';
import {hand} from '../web/hand-geometry.mjs';
import {rad,forward,solveHeight,smoothPose,previewDuration,exportPose,importPose} from '../web/kinematics.mjs';
const config={shoulderHeight:1.05,targetHeight:1.3,tipOffset:[.16,0,0]};
const zero=Array(7).fill(0);
test('URDF offsets accumulate in the shoulder frame for an identity fixture',()=>{
  const fixture={joints:Array.from({length:7},()=>({xyz:[.1,0,0],rpy:[0,0,0],axis:[0,1,0],lower:-Math.PI,upper:Math.PI}))};
  const result=forward(fixture,zero,config);
  assert.ok(Math.abs(result.tip[0]-.76)<1e-12);
  assert.equal(result.tip[2],1.05);
  const rotated=forward(fixture,[-Math.PI/2,0,0,0,0,0,0],config);
  assert.ok(Math.abs(rotated.tip[2]-1.81)<1e-12);
});
test('130 cm candidate meets height tolerance and faces forward horizontally',()=>{
  const result=solveHeight(model,zero,config);
  assert.ok(result.error<.005);
  assert.ok(Math.abs(result.pose.direction[2])<1e-9);
  assert.ok(result.pose.direction[0]>=.85);
  for(const i of [1,2,4,6])assert.equal(result.q[i],zero[i]);
});
test('locked axes remain unchanged with nonzero seed',()=>{
  const seed=[0,-.03,.02,0,.01,0,-.02];
  const result=solveHeight(model,seed,config);
  for(const i of [1,2,4,6])assert.equal(result.q[i],seed[i]);
});
test('unreachable target fails instead of claiming success',()=>{
  assert.throws(()=>solveHeight(model,zero,{...config,shoulderHeight:.5,targetHeight:1.8}),/未找到/);
});
test('quintic preview has exact endpoints and respects selected angular rate',()=>{
  const target=[-1.8,0,0,.7,0,1.1,0],duration=previewDuration(zero,target);
  assert.deepEqual(smoothPose(zero,target,0),zero);
  assert.deepEqual(smoothPose(zero,target,1),target);
  let previous=zero;
  for(let i=1;i<=1000;i++){
    const q=smoothPose(zero,target,i/1000),dt=duration/1000;
    q.forEach((v,j)=>assert.ok(Math.abs(v-previous[j])/dt<=rad(15)+1e-6));previous=q;
  }
});
test('invalid geometry and out-of-limit joint input are rejected',()=>{
  assert.throws(()=>forward(model,[NaN,...zero.slice(1)],config));
  assert.throws(()=>forward(model,[10,...zero.slice(1)],config));
  assert.throws(()=>forward(model,zero,{...config,tipOffset:[.5,0,0]}));
  assert.throws(()=>forward(model,zero,{...config,shoulderHeight:NaN}));
});
test('measured shoulder top is distinct from the joint axis height',()=>{
  const measured={...config,shoulderTopHeight:1.1,shoulderTopToJoint:.04,shoulderHeight:1.06};
  const base=forward(model,zero,measured);
  assert.equal(base.points[0][2],1.06);
  const data=importPose(model,exportPose(model,zero,measured));
  assert.equal(data.config.shoulderTopHeight,1.1);assert.equal(data.config.shoulderTopToJoint,.04);
  assert.throws(()=>forward(model,zero,{...measured,shoulderHeight:1.1}),/不一致/);
});
test('offline export round trip cannot authorize execution or calibration',()=>{
  const value=exportPose(model,zero,config);
  assert.equal(value.execution_authorized,false);assert.equal(value.calibration_verified,false);
  const loaded=importPose(model,{...value,execution_authorized:true,calibration_verified:true});
  assert.deepEqual(loaded.q,zero);
  assert.equal(exportPose(model,loaded.q,loaded.config).execution_authorized,false);
  assert.throws(()=>importPose(model,{...value,urdf_sha256:'wrong'}),/模型版本/);
  assert.throws(()=>importPose(model,{...value,joints:[...value.joints].reverse()}),/关节顺序/);
});
test('official FTP attachment includes base, two finger joints and mesh tip',()=>{
  const c={...config,shoulderHeight:1.1,endpoint:'official_ftp',fingerBend:0,tipOffset:hand.tip};
  const pose=forward(model,zero,c),body=forward(model,zero,config);
  assert.equal(pose.points.length,11);
  for(let i=0;i<7;i++)for(let j=0;j<3;j++)assert.ok(Math.abs(pose.points[i][j]-body.points[i][j]-(j===2?.05:0))<1e-10);
  const spacing=Math.hypot(...pose.points[9].map((v,i)=>v-pose.points[8][i]));
  assert.ok(Math.abs(spacing-.03278015416086386)<1e-12);
  const result=solveHeight(model,zero,c);assert.ok(result.error<.005);
  assert.ok(Math.abs(result.pose.direction[2])<1e-9);
  const bent=forward(model,zero,{...c,fingerBend:.5});
  assert.deepEqual(bent.points.slice(0,9),pose.points.slice(0,9));
  assert.notDeepEqual(bent.tip,pose.tip);
  // The former example offset cannot silently override official hand geometry.
  assert.deepEqual(forward(model,zero,{...c,tipOffset:[.3,.1,0]}).tip,pose.tip);
});
test('FTP exports pin the hand source and reject another hand model',()=>{
  const c={...config,endpoint:'official_ftp',fingerBend:.2};
  const value=exportPose(model,zero,c);
  assert.equal(value.hand_geometry_sha256,hand.official_urdf_sha256);
  assert.equal(importPose(model,value).config.fingerBend,.2);
  assert.throws(()=>importPose(model,{...value,hand_geometry_sha256:'wrong'}),/灵巧手模型/);
  assert.throws(()=>forward(model,zero,{...c,fingerBend:2}),/食指模型/);
});
