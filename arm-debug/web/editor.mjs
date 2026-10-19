// File and unit boundaries shared by the UI and offline tests. No network IO.
export const rad=value=>value*Math.PI/180;
export const deg=value=>value*180/Math.PI;
export function liveTargetStatus(value,joint,limits,remaining){
  if(!joint||!limits)return {allowed:false,error:'not_ready_or_busy',duration:0};
  const step=Math.abs(value-joint.target_rad),window=Math.abs(value-joint.seed_rad);
  const duration=Math.max(1,1.875*step/rad(limits.preview_rate_deg_s));
  let error='';
  if(!Number.isFinite(value))error='nonfinite_target';
  else if(step>rad(limits.step_deg)+1e-7)error='step_exceeds_20_degrees';
  else if(window>rad(limits.session_deg)+1e-7)error='session_exceeds_50_degrees';
  else if(duration+limits.settle_budget_seconds>remaining)error='insufficient_session_time';
  return {allowed:!error,error,duration};
}
export function validateAngles(model,values){
  if(!Array.isArray(values)||values.length!==7||values.some((v,i)=>typeof v!=='number'||!Number.isFinite(v)||v<model.joints[i].lower||v>model.joints[i].upper))throw Error('关节角度超出模型范围或不是有效数字');
  return [...values];
}
export function exportAngles(model,values){
  validateAngles(model,values);
  return {schema:'g1-right-arm-debug-pose/v1',mode:'offline',side:'R',execution_authorized:false,
    calibration_verified:false,urdf_sha256:model.urdf_sha256,
    joints:model.joints.map((j,i)=>({name:j.name,index:22+i,q_rad:values[i],q_deg:deg(values[i])})),
    notes:'Edited angles only. Import does not acquire control or send targets. SDK/model zero and collision/stability are not calibrated.'};
}
export function importAngles(model,data){
  if(data?.schema!=='g1-right-arm-debug-pose/v1'||data.mode!=='offline'||data.side!=='R'||data.urdf_sha256!==model.urdf_sha256)throw Error('姿态格式或机器人模型版本不匹配');
  if(!Array.isArray(data.joints)||data.joints.length!==7||data.joints.some((j,i)=>j.name!==model.joints[i].name||j.index!==22+i))throw Error('关节顺序不匹配');
  return validateAngles(model,data.joints.map(j=>j.q_rad));
}
