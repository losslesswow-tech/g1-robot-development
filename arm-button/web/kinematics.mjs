// Geometry only: SI units, URDF origin * joint rotation. No device transport.
import {hand} from './hand-geometry.mjs';
export const rad = degrees => degrees * Math.PI / 180;
export const deg = radians => radians * 180 / Math.PI;
const I = () => [[1,0,0],[0,1,0],[0,0,1]];
const mv = (a,b) => a.map(row => row.reduce((v,x,i) => v+x*b[i],0));
const mm = (a,b) => a.map(row => b[0].map((_,j) => row.reduce((v,x,k) => v+x*b[k][j],0)));
const add = (a,b) => a.map((x,i) => x+b[i]);
function rotation(axis,q) {
  const [x,y,z]=axis, c=Math.cos(q), s=Math.sin(q), t=1-c;
  return [[t*x*x+c,t*x*y-s*z,t*x*z+s*y],[t*x*y+s*z,t*y*y+c,t*y*z-s*x],[t*x*z-s*y,t*y*z+s*x,t*z*z+c]];
}
function rpy([r,p,y]) {return mm(mm(rotation([0,0,1],y),rotation([0,1,0],p)),rotation([1,0,0],r));}
export function validate(model,q,config) {
  if (!Array.isArray(q) || q.length!==7 || q.some((v,i)=>!Number.isFinite(v)||v<model.joints[i].lower-1e-9||v>model.joints[i].upper+1e-9)) throw Error('关节角度超出模型范围');
  if (!Number.isFinite(config.shoulderHeight) || config.shoulderHeight<0.5 || config.shoulderHeight>1.6) throw Error('肩部高度须为 50–160 cm');
  if (!Array.isArray(config.tipOffset) || config.tipOffset.length!==3 || config.tipOffset.some(v=>!Number.isFinite(v)||Math.abs(v)>0.4)) throw Error('指尖偏移须在 ±40 cm 内');
  if (!Number.isFinite(config.targetHeight)||config.targetHeight<0.3||config.targetHeight>1.8) throw Error('目标高度须为 30–180 cm');
  if(config.shoulderTopHeight!==undefined || config.shoulderTopToJoint!==undefined) {
    if(!Number.isFinite(config.shoulderTopHeight)||!Number.isFinite(config.shoulderTopToJoint)||config.shoulderTopToJoint<0||config.shoulderTopToJoint>.2||Math.abs(config.shoulderHeight-(config.shoulderTopHeight-config.shoulderTopToJoint))>1e-8)throw Error('肩顶、轴心偏移与轴心高度不一致');
  }
  if(config.endpoint!==undefined && !['official_ftp','legacy_example'].includes(config.endpoint))throw Error('末端模型不支持');
  if(config.endpoint==='official_ftp' && (!Number.isFinite(config.fingerBend)||config.fingerBend<hand.joints[1].lower||config.fingerBend>hand.joints[1].upper))throw Error('食指模型角度超出官方范围');
}
export function forward(model,q,config) {
  validate(model,q,config);
  let R=I(), p=[0,0,config.shoulderHeight];
  const points=[];
  model.joints.forEach((joint,i)=>{
    // The measured shoulder height anchors the first URDF joint origin.
    if(i>0)p=add(p,mv(R,joint.xyz));
    R=mm(R,rpy(joint.rpy));points.push([...p]);
    R=mm(R,rotation(joint.axis,q[i]));
  });
  if(config.endpoint==='official_ftp') {
    hand.joints.forEach((joint,i)=>{
      p=add(p,mv(R,joint.xyz));R=mm(R,rpy(joint.rpy));points.push([...p]);
      if(joint.type==='revolute')R=mm(R,rotation(joint.axis,i===1?config.fingerBend:config.fingerBend*joint.mimic.multiplier+joint.mimic.offset));
    });
    const tip=add(p,mv(R,hand.tip));points.push(tip);
    return {points,tip,direction:mv(R,[0,1,0]),rotation:R};
  }
  const tip=add(p,mv(R,config.tipOffset));points.push(tip);
  return {points,tip,direction:mv(R,[1,0,0]),rotation:R};
}
// Height alone is underdetermined. Prefer small changes from the seed and a
// forward horizontal fingertip; retain shoulder roll/yaw and wrist roll/yaw.
// Search windows are visible preview choices, never hardware safety limits.
export function solveHeight(model,seed,config) {
  validate(model,seed,config);
  let best=null;
  function consider(sp,el) {
    const q=[...seed];q[0]=rad(sp);q[3]=rad(el);q[5]=0;
    const f0=forward(model,q,config).direction[2];
    q[5]=Math.PI/2;const f90=forward(model,q,config).direction[2];
    // pi exceeds the wrist's URDF limit, so derive its sinusoid's constant
    // using -pi/2, which is inside the model's ±1.614 rad wrist pitch limit.
    q[5]=-Math.PI/2;const fn90=forward(model,q,config).direction[2];
    const C=(f90+fn90)/2,A=f0-C,B=(f90-fn90)/2,r=Math.hypot(A,B);
    if(r<1e-9||Math.abs(C)>r)return;
    const phi=Math.atan2(B,A),alpha=Math.acos(Math.max(-1,Math.min(1,-C/r)));
    for(const base of [phi+alpha,phi-alpha])for(const turn of [-2*Math.PI,0,2*Math.PI]) {
      q[5]=base+turn;
      if(q[5]<model.joints[5].lower||q[5]>model.joints[5].upper)continue;
      const pose=forward(model,q,config);
      if(pose.direction[0]<0.85||pose.tip[0]<=0)continue;
      const error=Math.abs(pose.tip[2]-config.targetHeight);
      const travel=q.reduce((sum,v,i)=>sum+(v-seed[i])**2,0);
      // Meet 1 mm height tolerance first, then favor smaller joint changes.
      const score=Math.max(0,error-0.001)*10000+travel;
      if(!best||score<best.score)best={q:[...q],pose,error,score};
    }
  }
  for(let sp=-140;sp<=20;sp+=2)for(let el=0;el<=100;el+=2)consider(sp,el);
  if(best){const sp=deg(best.q[0]),el=deg(best.q[3]);for(let a=Math.max(-140,sp-2);a<=Math.min(20,sp+2);a+=0.1)for(let b=Math.max(0,el-2);b<=Math.min(100,el+2);b+=0.2)consider(a,b);}
  if(!best||best.error>0.005)throw Error('当前几何参数和预览范围内未找到高度候选；请核对参数或手动调整。');
  return best;
}
export function smoothPose(a,b,t) {const x=Math.max(0,Math.min(1,t)),s=x*x*x*(10+x*(-15+6*x));return a.map((v,i)=>v+(b[i]-v)*s);}
export function previewDuration(a,b,rate=rad(15)) {
  if(!Number.isFinite(rate)||rate<=0)throw Error('预览速度必须为正数');
  // The quintic derivative peaks at 1.875, so account for it explicitly.
  return Math.max(3,1.875*Math.max(...a.map((v,i)=>Math.abs(b[i]-v)))/rate);
}
export function exportPose(model,q,config) {
  validate(model,q,config);
  return {schema:'g1-right-arm-height-preview/v1',mode:'offline',execution_authorized:false,calibration_verified:false,side:'R',source_commit:model.source_commit,urdf_sha256:model.urdf_sha256,hand_geometry_sha256:config.endpoint==='official_ftp'?hand.official_urdf_sha256:null,official_source_commit:config.endpoint==='official_ftp'?hand.official_repository_commit:null,angle_space:'URDF model; not calibrated to physical SDK/Modbus zero',config:{...config},joints:model.joints.map((j,i)=>({name:j.name,q_rad:q[i],q_deg:deg(q[i])})),estimated_tip_height_m:forward(model,q,config).tip[2],omitted_checks:['floor frame calibration','Inspire mount and fingertip calibration','collision','stability','contact force','control ownership','navigation interlock']};
}
export function importPose(model,value) {
  if(value?.schema!=='g1-right-arm-height-preview/v1'||value.side!=='R'||value.mode!=='offline'||value.urdf_sha256!==model.urdf_sha256)throw Error('姿态格式或模型版本不匹配');
  if(!Array.isArray(value.joints)||value.joints.length!==7||value.joints.some((j,i)=>j.name!==model.joints[i].name))throw Error('关节顺序不匹配');
  const q=value.joints.map(j=>j.q_rad),config={shoulderHeight:value.config?.shoulderHeight,targetHeight:value.config?.targetHeight,tipOffset:value.config?.tipOffset};
  if(value.config?.shoulderTopHeight!==undefined||value.config?.shoulderTopToJoint!==undefined){config.shoulderTopHeight=value.config.shoulderTopHeight;config.shoulderTopToJoint=value.config.shoulderTopToJoint;}
  if(value.config?.endpoint!==undefined){config.endpoint=value.config.endpoint;config.fingerBend=value.config.fingerBend;}
  if(config.endpoint==='official_ftp' && value.hand_geometry_sha256!==hand.official_urdf_sha256)throw Error('灵巧手模型版本不匹配');
  validate(model,q,config);return {q,config};
}
