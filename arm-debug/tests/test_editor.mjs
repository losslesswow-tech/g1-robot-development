import assert from 'node:assert/strict';
import {test} from 'node:test';
import {model} from '../../arm-button/web/model.mjs';
import {rad,deg,exportAngles,importAngles,validateAngles,liveTargetStatus} from '../web/editor.mjs';
import {legDiagnostics} from '../web/diagnostics.mjs';
test('historical right ankle speeds are warnings while motor flags and invalid data remain faults',()=>{
 for(const speed of [-.336218089,-.544559896]){
  const sample={index:11,q_rad:.04,dq_rad_s:speed,motorstate:0};
  const d=legDiagnostics([sample]);assert.equal(d.faults.length,0);assert.equal(d.speedWarnings.length,1);
  assert.equal(d.speedWarnings[0].label,'右踝侧摆');
  assert.equal(legDiagnostics([{...sample,motorstate:1}]).faults[0].reason,'leg_motor_state');
  assert.equal(legDiagnostics([{...sample,dq_rad_s:NaN}]).faults[0].reason,'leg_data_invalid');
  assert.equal(legDiagnostics([{...sample,q_rad:Infinity}]).faults[0].reason,'leg_data_invalid');
 }
});
test('live send uses 20 degree step and 50 degree session bounds with a time budget',()=>{
 const limits={step_deg:20,session_deg:50,preview_rate_deg_s:5,settle_budget_seconds:2};
 const initial={target_rad:0,seed_rad:0};
 const full=liveTargetStatus(rad(20),initial,limits,30);
 assert.equal(full.allowed,true);assert.ok(Math.abs(full.duration-7.5)<1e-12);
 assert.equal(liveTargetStatus(rad(20.1),initial,limits,30).error,'step_exceeds_20_degrees');
 for(const sign of [-1,1]){
  assert.equal(liveTargetStatus(sign*rad(50),{target_rad:sign*rad(30),seed_rad:0},limits,30).allowed,true);
  assert.equal(liveTargetStatus(sign*rad(50.1),{target_rad:sign*rad(50),seed_rad:0},limits,30).error,'session_exceeds_50_degrees');
 }
 assert.equal(liveTargetStatus(rad(20),initial,limits,9).error,'insufficient_session_time');
});
test('degrees and radians round trip for all seven debug angles',()=>{
 const q=[.296,-.205,.028,.99,-.129,.04,.043];
 q.forEach(x=>assert.ok(Math.abs(rad(deg(x))-x)<1e-12));
 assert.deepEqual(importAngles(model,exportAngles(model,q)),q);
});
test('import cannot turn a pose file into live execution or swap joints',()=>{
 const value=exportAngles(model,Array(7).fill(0));value.execution_authorized=true;value.calibration_verified=true;
 const loaded=importAngles(model,value),saved=exportAngles(model,loaded);
 assert.equal(saved.execution_authorized,false);assert.equal(saved.calibration_verified,false);
 assert.throws(()=>importAngles(model,{...value,urdf_sha256:'wrong'}));
 assert.throws(()=>importAngles(model,{...value,joints:[...value.joints].reverse()}));
});
test('invalid editing values are rejected without clipping',()=>{
 for(const q of [[NaN,0,0,0,0,0,0],[4,0,0,0,0,0,0],Array(6).fill(0),['0',0,0,0,0,0,0]])assert.throws(()=>validateAngles(model,q));
 const limit=model.joints.map(j=>j.upper);assert.deepEqual(validateAngles(model,limit),limit);
});
