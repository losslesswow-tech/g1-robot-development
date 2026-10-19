// Offline geometry only. No device transport, actuator commands or navigation.
import {forward,validate,rad,smoothPose,previewDuration} from './kinematics.mjs';
import {hand} from './hand-geometry.mjs';
const norm=v=>Math.hypot(...v);
const difference=(a,b)=>a.map((v,i)=>v-b[i]);
function solve(matrix,rhs){
  const a=matrix.map((row,i)=>[...row,rhs[i]]),n=rhs.length;
  for(let i=0;i<n;i++){
    let pivot=i;for(let j=i+1;j<n;j++)if(Math.abs(a[j][i])>Math.abs(a[pivot][i]))pivot=j;
    [a[i],a[pivot]]=[a[pivot],a[i]];
    if(Math.abs(a[i][i])<1e-14)throw Error('当前姿态无法生成稳定的按压几何路径');
    const divisor=a[i][i];for(let k=i;k<=n;k++)a[i][k]/=divisor;
    for(let j=0;j<n;j++)if(j!==i){const scale=a[j][i];for(let k=i;k<=n;k++)a[j][k]-=scale*a[i][k];}
  }
  return a.map(row=>row[n]);
}
// Damped least squares: 3-D tip position and direction. All seven right-arm
// joints may change; direction is the FTP index direction, not the palm axis.
export function solveTip(model,seed,config,target,direction){
  validate(model,seed,config);
  if(target.length!==3||direction.length!==3||[...target,...direction].some(v=>!Number.isFinite(v))||Math.abs(norm(direction)-1)>1e-6)throw Error('按压几何目标无效');
  let q=[...seed];const scale=.12,epsilon=1e-5;
  const feature=pose=>[...pose.tip,...pose.direction.map(v=>v*scale)];
  const wanted=[...target,...direction.map(v=>v*scale)];
  for(let iteration=0;iteration<160;iteration++){
    const pose=forward(model,q,config);
    if(norm(difference(pose.tip,target))<.00015&&norm(difference(pose.direction,direction))<.0015)return q;
    const current=feature(pose),error=difference(wanted,current);
    const columns=q.map((v,i)=>{
      const next=[...q],step=v+epsilon<=model.joints[i].upper?epsilon:-epsilon;next[i]+=step;
      return difference(feature(forward(model,next,config)),current).map(x=>x/step);
    });
    const matrix=Array.from({length:6},(_,r)=>Array.from({length:6},(_,c)=>columns.reduce((sum,col)=>sum+col[r]*col[c],r===c?.000004:0)));
    const dual=solve(matrix,error),delta=columns.map(col=>col.reduce((sum,v,i)=>sum+v*dual[i],0));
    const factor=Math.min(1,rad(3)/Math.max(...delta.map(Math.abs)));
    q=q.map((v,i)=>Math.max(model.joints[i].lower,Math.min(model.joints[i].upper,v+delta[i]*factor)));
  }
  throw Error('按压/撤回路径在模型限位内不可达，请减小间隙或行程，或调整准备姿态');
}
function pathDuration(path,rate,minimum=1){
  const intervals=path.length-1;
  const slope=Math.max(...path.slice(1).flatMap((q,i)=>q.map((v,j)=>Math.abs(v-path[i][j])*intervals)));
  return Math.max(minimum,1.875*slope/rate);
}
export function buildActionGroup(model,reference,ready,config,options){
  validate(model,reference,config);validate(model,ready,config);
  if(config.endpoint!=='official_ftp')throw Error('动作组需要已核对的 RH56DFTP 几何');
  const {gapM,strokeM,holdS,previewRateRadS}=options;
  if(!Number.isFinite(gapM)||gapM<.001||gapM>.05||!Number.isFinite(strokeM)||strokeM<.001||strokeM>.015||!Number.isFinite(holdS)||holdS<.1||holdS>3||!Number.isFinite(previewRateRadS)||previewRateRadS<rad(1)||previewRateRadS>rad(30))throw Error('预览参数超出页面允许范围');
  const start=forward(model,ready,config);
  if(Math.abs(start.tip[2]-config.targetHeight)>.005)throw Error('请先生成或调整到目标高度，再生成动作组');
  if(start.direction[0]<.85||Math.abs(start.direction[2])>.05)throw Error('请先把食指调整为近水平向前，再生成动作组');
  const trace=(initial,origin,distance)=>{
    const count=Math.max(4,Math.ceil(distance/.001)),path=[[...initial]];let seed=initial;
    for(let i=1;i<=count;i++){
      const tip=[origin[0]+distance*i/count,origin[1],origin[2]];
      seed=solveTip(model,seed,config,tip,start.direction);path.push(seed);
    }
    return path;
  };
  const approach=trace(ready,start.tip,gapM),contact=[start.tip[0]+gapM,start.tip[1],start.tip[2]];
  const press=trace(approach.at(-1),contact,strokeM),pressed=press.at(-1);
  // Retraction exactly reverses the approach and pressing geometry, so it
  // clears the virtual button before the lowering phase can begin.
  const retract=[...approach,...press.slice(1)].reverse().map(q=>[...q]);
  const stages=[
    {id:'lift',label:'抬手到准备位',group:'lift',path:[[...reference],[...ready]],durationS:previewDuration(reference,ready,previewRateRadS)},
    {id:'approach',label:'食指接近按钮',group:'press',path:approach,durationS:pathDuration(approach,previewRateRadS)},
    {id:'press',label:'食指向前按压',group:'press',path:press,durationS:pathDuration(press,previewRateRadS)},
    {id:'hold',label:'按压保持（预演）',group:'press',path:[[...pressed],[...pressed]],durationS:holdS},
    {id:'retract',label:'原路撤回准备位',group:'retract',path:retract,durationS:pathDuration(retract,previewRateRadS)},
    {id:'lower',label:'放下回参考姿态',group:'lower',path:[[...ready],[...reference]],durationS:previewDuration(ready,reference,previewRateRadS)}
  ];
  const result={schema:'g1-right-arm-action-group/v1',mode:'offline',execution_authorized:false,calibration_verified:false,physical_retry_blocked:true,
    robot:'Unitree G1',hand:'RH56DFTP',side:'R',urdf_sha256:model.urdf_sha256,hand_geometry_sha256:hand.official_urdf_sha256,
    frame:'upright torso; X forward Y left Z up; floor and shoulder axis uncalibrated',config:JSON.parse(JSON.stringify(config)),options:{...options},
    button:{source:'virtual; position derived from selected preparation pose, not a measured robot-to-button distance',facePointM:contact,inwardDirection:[1,0,0]},
    stages,totalDurationS:stages.reduce((sum,s)=>sum+s.durationS,0),
    omittedChecks:['stationary locomotion policy','waist ownership','collision','contact force','actual button activation','hardware joint calibration','arm hand-back','navigation']};
  return JSON.parse(JSON.stringify(result));
}
export function sampleActionGroup(plan,timeS){
  if(!Number.isFinite(timeS))throw Error('预演时间无效');
  let t=Math.max(0,timeS);
  for(const stage of plan.stages){
    if(t<stage.durationS){
      const ratio=t/stage.durationS,s=ratio**3*(10+ratio*(-15+6*ratio));
      const position=s*(stage.path.length-1),index=Math.min(stage.path.length-2,Math.floor(position)),fraction=position-index;
      const a=stage.path[index],b=stage.path[index+1];
      return {q:a.map((v,i)=>v+(b[i]-v)*fraction),stage:stage.id,group:stage.group,label:stage.label,progress:ratio,complete:false};
    }
    t-=stage.durationS;
  }
  const last=plan.stages.at(-1);
  return {q:[...last.path.at(-1)],stage:'complete',group:'lower',label:'动作组预演完成',progress:1,complete:true};
}
