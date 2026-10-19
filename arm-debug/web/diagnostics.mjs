// Unitree G1 SDK PR joint order. Speed reference belongs to this tool.
export const legNames=['左髋前后摆','左髋侧摆','左髋旋转','左膝','左踝俯仰','左踝侧摆','右髋前后摆','右髋侧摆','右髋旋转','右膝','右踝俯仰','右踝侧摆'];
export function legDiagnostics(legs,reference=.3){
 const faults=[],speedWarnings=[];
 for(const j of legs||[]){
  const sample={...j,label:legNames[j.index]||('SDK '+j.index)};
  if(!Number.isFinite(j.q_rad)||!Number.isFinite(j.dq_rad_s))faults.push({...sample,reason:'leg_data_invalid'});
  else if(j.motorstate!==0)faults.push({...sample,reason:'leg_motor_state'});
  if(Number.isFinite(j.dq_rad_s)&&Math.abs(j.dq_rad_s)>reference)speedWarnings.push(sample);
 }
 return {faults,speedWarnings};
}
