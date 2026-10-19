import * as THREE from '/geometry/three.module.min.js';
import {model} from '/geometry/model.mjs';
import {snapshot} from '/geometry/snapshot.mjs';
import {hand} from '/geometry/hand-geometry.mjs';
import {forward} from '/geometry/kinematics.mjs';
import {rad,deg,validateAngles,exportAngles,importAngles,liveTargetStatus} from './editor.mjs';
import {legDiagnostics,legNames} from './diagnostics.mjs';
const $=id=>document.getElementById(id);
const labels=['肩前后摆','肩侧摆','肩旋转','肘弯曲','腕旋转','腕俯仰','腕偏转'];
const geometry={shoulderHeight:1.1,targetHeight:1.3,tipOffset:hand.tip,endpoint:'official_ftp',fingerBend:0};
let q=[...snapshot.right_q_rad],selected=0,config=null,state={},busy=false,sessionIntent=false,adoptWhenReady=false,polling=false,invalid=false,lastFault='';
const phases={readonly:'只读 · 未接管',acquiring:'接管过渡',holding:'等待单次发送',moving:'移动中',settling:'核对实际到位',releasing:'正在交还',fault:'保护中止',disconnected:'已断开'};
const errors={right_arm_only:'仅允许右臂七个关节',nonfinite_target:'目标角度不是有效数字',model_joint_limit:'目标超出官方模型范围',step_exceeds_20_degrees:'单次目标变化不能超过 20°',session_exceeds_50_degrees:'本次接管相对初始姿态不能超过 ±50°',observed_session_envelope:'实测右臂关节偏移超出本次调试范围',left_arm_hold_envelope:'左臂偏离保持姿态超过 3°',insufficient_session_time:'剩余调试时间不足以完成轨迹和到位核对，请交还后重新准备',not_ready_or_busy:'尚未准备好或正在移动，请等待读数更新',not_readonly:'当前不是只读状态',builtin_arm_service_not_stopped:'内置手臂服务尚未确认停止，请展开下方服务设置',mode_changed_or_query_failed:'运控状态改变或查询失败，已停止调节',another_arm_sdk_writer:'检测到其他上肢控制来源',another_local_arm_controller:'已有另一个机械臂调试进程',stale_lowstate:'关节状态不新鲜，未发送目标',body_rotation:'躯干角速度触发保护',body_tilt:'躯干倾角触发保护',leg_state_or_speed:'腿部状态或速度触发保护',arm_state_fault:'手臂状态异常',tracking_error:'目标跟踪误差触发保护',browser_heartbeat_lost:'页面心跳丢失，正在交还控制权',session_timeout:'本次 30 秒调试已结束，请断开后重新连接',this_page_does_not_own_control:'本页尚未取得调试控制权',another_page_owns_control:'其他页面正在调试，请先在该页面交还控制权',onsite_monitoring_required:'请确认现场监护和停止手段',control_not_enabled_on_server:'当前服务只允许读取',control_disabled_in_build:'当前程序未编译实物调节入口',connection_failed_or_no_fresh_state:'无法取得新鲜机械臂状态，请检查背包连接',service_stop_failed_or_not_verified:'内置手臂服务停止未获验证',release_before_service_change:'请先交还控制权再调整服务',backend_transport_failure:'调试通道异常，未自动重试',worker_ack_timeout:'目标回执超时，未自动重试；等待保护交还',worker_connection_closed:'调试通道已断开',not_connected:'请先连接读取',input_closed:'调试输入已关闭',operator_interrupt:'调试进程收到停止信号',machine_mode_changed:'底层机型状态改变，已停止调节',duplicate_request_rejected:'重复请求已拒绝',invalid_imu:'躯干状态数据无效'};
errors.target_not_reached='未在期限内确认选中关节到位，已停止调节';
Object.assign(errors,{leg_data_invalid:'腿部角度或速度数据无效，已中止调节',leg_motor_state:'腿部电机返回非零状态码，已中止调节'});
Object.assign(errors,{fsm_query_stale:'运控状态查询持续失败或超过 1.5 秒未更新',fsm_mode_query_stale:'运控子状态查询持续失败或超过 1.5 秒未更新',fsm_changed:'机器人实际返回了不同的运控状态',fsm_mode_changed:'机器人实际返回了不同的运控子状态',mode_query_pending:'状态查询暂未恢复，暂停发送目标',service_query_stale:'内置手臂服务查询超过 1.5 秒未更新',service_query_pending:'内置手臂服务查询暂未恢复'});
Object.assign(errors,{fsm_mode_unknown:'机器人返回未识别的运控子状态',not_standing_at_acquire:'请在开始接管前等待机器人进入静态状态；接管后允许动态标记'});
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
function faultText(s){
  const at=s.fault_context,detail=at?' 触发时 FSM '+at.fsm_id+' / '+at.fsm_mode+'；查询返回码 '+at.fsm_query_rc+' / '+at.fsm_mode_query_rc+'；接管权重 '+Number(at.weight).toFixed(6)+'。':'';
  let reason=errors[s.fault]||s.fault,legDetail='';
  if(s.fault.startsWith('leg_')&&at?.legs){
    const diagnosis=legDiagnostics(at.legs,at.leg_speed_monitor?.reference_rad_s||.3);
    const relevant=diagnosis.faults.length?diagnosis.faults:diagnosis.speedWarnings;
    legDetail=relevant.map(j=>' SDK '+j.index+' '+j.label+'，dq='+Number(j.dq_rad_s).toFixed(3)+' rad/s，motorstate='+j.motorstate).join('；')+'。';
    if(s.fault==='leg_state_or_speed'&&!diagnosis.faults.length&&diagnosis.speedWarnings.length)reason='旧版腿速参考值触发交还；该读数不代表已确认电机故障';
  }
  return reason+'。'+legDetail+detail+'未自动重试；请断开并检查现场。';
}
function token(){return Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');}
async function api(action,data={}){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
  try{
    const response=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,csrf:config.csrf,request_id:token()}),signal:controller.signal});
    const value=await response.json();if(!response.ok)throw Error(errors[value.error]||value.error||'请求失败');return value;
  }finally{clearTimeout(timer);}
}
async function action(fn){if(busy)return;busy=true;refresh();try{await fn();}catch(e){message(e.message,true);}finally{busy=false;refresh();}}
function select(i){selected=i;document.querySelectorAll('.channel').forEach((row,j)=>row.classList.toggle('selected',j===i));$('selection').textContent=labels[i];refresh();draw();}
function controls(){q.forEach((v,i)=>{$('q'+i).value=deg(v).toFixed(3);$('n'+i).value=deg(v).toFixed(3);$('n'+i).setCustomValidity('');});invalid=false;}
function edit(i,degrees){
  try{const next=[...q];if(degrees===''||!Number.isFinite(Number(degrees)))throw Error('请输入有效角度');next[i]=rad(Number(degrees));validateAngles(model,next);q=next;controls();select(i);update();message('已编辑 '+labels[i]+'，尚未发送。');}
  catch(e){invalid=true;$('n'+i).setCustomValidity(e.message);refresh();message(e.message,true);}
}
model.joints.forEach((j,i)=>{
  const row=document.createElement('div');row.className='channel'+(i===0?' selected':'');
  row.innerHTML=`<div class="channel-top"><label for="q${i}">${labels[i]}</label><div class="value"><button class="adjust" id="minus${i}" aria-label="减小${labels[i]}">−</button><input id="n${i}" aria-label="${labels[i]}目标角度" type="number" min="${deg(j.lower)}" max="${deg(j.upper)}" step="any"><span>°</span><button class="adjust" id="plus${i}" aria-label="增大${labels[i]}">＋</button></div></div><input id="q${i}" aria-label="${labels[i]}角度滑条" type="range" min="${deg(j.lower)}" max="${deg(j.upper)}" step="any"><div class="channel-meta"><span id="actual${i}">实测 —</span><span>模型 ${deg(j.lower).toFixed(1)}° ～ ${deg(j.upper).toFixed(1)}°</span></div>`;
  row.onclick=()=>select(i);$('controls').append(row);
  $('q'+i).oninput=e=>edit(i,e.target.value);$('n'+i).onchange=e=>edit(i,e.target.value);
  $('plus'+i).onclick=e=>{e.stopPropagation();edit(i,deg(q[i])+Number($('step').value));};
  $('minus'+i).onclick=e=>{e.stopPropagation();edit(i,deg(q[i])-Number($('step').value));};
});

const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(40,1,.01,30);
let renderer=null;
try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));$('viewport').append(renderer.domElement);}
catch(e){$('viewport').textContent='当前浏览器未提供三维画布，角度编辑和连接功能仍可使用。';}
scene.add(new THREE.HemisphereLight(0xffffff,0x879d99,2.8));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(2,4,3);scene.add(light);
scene.add(new THREE.GridHelper(1.8,18,0xadc2be,0xd7e1df));
const body=new THREE.Mesh(new THREE.BoxGeometry(.18,.32,.26),new THREE.MeshStandardMaterial({color:0xcddbd7,transparent:true,opacity:.55}));body.position.set(-.045,.97,-.14);scene.add(body);
const V=p=>new THREE.Vector3(p[0],p[2],-p[1]);
function skeleton(color,actual=false){
  const group=new THREE.Group(),parts=[],joints=[];scene.add(group);
  for(let i=0;i<10;i++){
    const m=new THREE.MeshStandardMaterial({color,roughness:.4,metalness:.1,wireframe:actual,transparent:actual,opacity:actual?.65:1});
    const c=new THREE.Mesh(new THREE.CylinderGeometry(i>=8?.009:.017,i>=8?.009:.017,1,16),m);c.userData.index=Math.min(i,6);group.add(c);parts.push(c);
  }
  for(let i=0;i<11;i++){
    const sphere=new THREE.Mesh(new THREE.SphereGeometry(actual?.027:(i>=9?.013:.024),18,12),new THREE.MeshStandardMaterial({color:actual?color:0xe5f0ec,wireframe:actual,transparent:actual,opacity:actual?.75:1}));
    sphere.userData.index=Math.min(i,6);group.add(sphere);joints.push(sphere);
  }
  return {group,parts,joints,actual};
}
const edited=skeleton(0x238b89),measured=skeleton(0xd89c44,true);measured.group.visible=false;
function setPose(skeleton,angles){
  const pose=forward(model,angles,geometry);
  pose.points.forEach((p,i)=>skeleton.joints[i].position.copy(V(p)));
  skeleton.parts.forEach((mesh,i)=>{const a=V(pose.points[i]),b=V(pose.points[i+1]),delta=b.clone().sub(a);mesh.position.copy(a).add(b).multiplyScalar(.5);mesh.scale.y=delta.length();mesh.visible=delta.length()>1e-6;if(mesh.visible)mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),delta.normalize());});
}
let azimuth=1.05,elevation=.14,distance=1.95;
function draw(){if(!renderer)return;const center=new THREE.Vector3(.1,.98,0);camera.position.set(center.x+distance*Math.sin(azimuth)*Math.cos(elevation),center.y+distance*Math.sin(elevation),center.z+distance*Math.cos(azimuth)*Math.cos(elevation));camera.lookAt(center);edited.joints.forEach((j,i)=>j.material.color.setHex(i===selected?0x67b6ab:0xe5f0ec));renderer.render(scene,camera);}
function update(){setPose(edited,q);refresh();draw();}
if(renderer){
  new ResizeObserver(()=>{const r=$('viewport').getBoundingClientRect();camera.aspect=r.width/r.height;camera.updateProjectionMatrix();renderer.setSize(r.width,r.height);draw();}).observe($('viewport'));
  const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2();let drag=null;
  renderer.domElement.onpointerdown=e=>{const r=renderer.domElement.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1);raycaster.setFromCamera(pointer,camera);const hit=raycaster.intersectObjects([...edited.joints,...edited.parts])[0];if(hit)select(hit.object.userData.index);drag={x:e.clientX,y:e.clientY,index:hit?.object.userData.index,value:hit?deg(q[hit.object.userData.index]):0};renderer.domElement.setPointerCapture(e.pointerId);};
  renderer.domElement.onpointermove=e=>{if(!drag)return;if(drag.index!==undefined){edit(drag.index,drag.value-(e.clientY-drag.y)*.2);}else{azimuth-=(e.clientX-drag.x)*.006;elevation=Math.max(-.6,Math.min(.9,elevation+(e.clientY-drag.y)*.005));drag.x=e.clientX;drag.y=e.clientY;draw();}};
  for(const event of ['pointerup','pointercancel'])renderer.domElement.addEventListener(event,()=>{drag=null;});
  renderer.domElement.addEventListener('wheel',e=>{e.preventDefault();distance=Math.max(1.1,Math.min(3.5,distance+e.deltaY*.001));draw();},{passive:false});
}
$('front').onclick=()=>{azimuth=Math.PI/2;elevation=.05;draw();};$('side').onclick=()=>{azimuth=0;elevation=.05;draw();};$('oblique').onclick=()=>{azimuth=1.05;elevation=.14;draw();};

function refresh(){
  $('fault-detail').hidden=!state.fault;$('fault-detail').textContent=state.fault?faultText(state):'';
  const legMonitor=state.leg_speed_monitor;
  const legWarning=legDiagnostics(state.fresh?state.legs:[],legMonitor?.reference_rad_s||.3).speedWarnings;
  let legText='腿速 '+Number(legMonitor?.reference_rad_s||.3).toFixed(2)+' rad/s 为工具参考值，仅监测记录；电机状态异常仍中止。';
  if(legWarning.length)legText+=' 当前：'+legWarning.map(j=>j.label+'（SDK '+j.index+'）'+j.dq_rad_s.toFixed(3)+' rad/s').join('；')+'。';
  else if(state.connected)legText+=state.fresh?' 当前读数未超过参考值。':' 当前读数已过期。';
  if(legMonitor?.peak_abs_dq_rad_s?.length){
    const peak=Math.max(...legMonitor.peak_abs_dq_rad_s),i=legMonitor.peak_abs_dq_rad_s.indexOf(peak);
    legText+=' 本次连接峰值：'+legNames[i]+' '+peak.toFixed(3)+' rad/s。';
  }
  $('leg-speed-status').textContent=legText;$('leg-speed-status').classList.toggle('warning',!!legWarning.length);
  const connected=!!state.connected,fresh=!!state.fresh,owner=!!state.owned_by_this_page,phase=state.phase||'disconnected';
  const current=fresh?state.joints?.find(j=>j.index===22+selected):null;
  const budget=liveTargetStatus(q[selected],current,config?.limits,state.remaining_s||0);
  const limitsMatch=state.limits?.step_deg===config?.limits.step_deg&&state.limits?.session_deg===config?.limits.session_deg;
  const ready=owner&&phase==='holding'&&fresh&&limitsMatch&&!state.query_pending&&!busy&&!invalid&&budget.allowed;
  $('connect').disabled=busy||connected;$('disconnect').disabled=busy||!connected||!!state.another_page_owns_control;
  $('adopt').disabled=busy||!fresh;$('prepare').disabled=busy||!fresh||!limitsMatch||state.query_pending||!$('onsite').checked||phase!=='readonly'||!!state.another_page_owns_control||!config?.control_enabled||!state.builtin_arm_stopped;
  $('release').disabled=busy||!owner||!['acquiring','holding','moving','settling'].includes(phase);$('send').disabled=!ready;
  $('quick-send').disabled=!ready;$('quick-release').disabled=$('release').disabled;
  $('quick-send').textContent='发送'+labels[selected];$('quick-summary').textContent=labels[selected]+' · 编辑目标 '+deg(q[selected]).toFixed(3)+'°'+(owner?' · 本页接管':' · 尚未接管');
  $('stop-service').disabled=busy||!fresh||!$('onsite').checked||phase!=='readonly'||!!state.another_page_owns_control||!config?.control_enabled||!!state.builtin_arm_stopped;
  $('send').textContent='发送'+labels[selected]+'目标';
  $('phase').textContent=(phases[phase]||phase)+(state.query_pending&&owner?' · 查询暂缺，保持目标':'');$('fsm').textContent=connected?(state.fsm_id+' / '+state.fsm_mode+(state.fsm_mode===1?' · 动态':state.fsm_mode===0?' · 静态':'')):'—';
  $('remaining').textContent=owner&&state.remaining_s>0?Math.ceil(state.remaining_s)+' 秒':'—';
  $('connection-state').textContent=connected?(phase==='releasing'?'正在交还 · 请等待':phase==='fault'?'保护中止':owner?'本页调试中':state.another_page_owns_control?'其他页面调试中':'已连接 · 只读'):'离线';
  $('connection-state').className='pill '+(owner?'live':connected?'':'neutral');
  $('service-state').textContent=connected?(state.builtin_arm_stopped?'已确认停止':'尚未确认停止'):'尚未查询';
  $('readout-age').textContent=connected?(fresh?((config?.backend==='simulation'?'模拟反馈':'新鲜读数')+' · '+Math.round((state.age_ms||0)+(state.transport_age_ms||0))+' ms'):'读数已过期'):'尚未连接实物';
  if(current){
    const delta=deg(q[selected]-current.target_rad);
    let text='目标 '+deg(q[selected]).toFixed(3)+'° · 当前 '+deg(current.q_rad).toFixed(3)+'°';
    if(owner)text+=' · 指令变化 '+(delta>=0?'+':'')+delta.toFixed(3)+'°';
    if(owner&&budget.error)text+=' · '+errors[budget.error];
    else if(owner)text+=' · 轨迹约 '+budget.duration.toFixed(1)+' 秒';
    if(!limitsMatch)text+=' · 调试程序限值版本不一致，请断开并刷新';
    $('send-summary').textContent=text;
  }else $('send-summary').textContent='离线编辑目标 '+deg(q[selected]).toFixed(3)+'°；连接只读取，准备后才能发送。';
}
function adopt(){if(!state.fresh)throw Error('尚无新鲜关节读数');const next=model.joints.map((_,i)=>state.joints.find(j=>j.index===22+i)?.q_rad);validateAngles(model,next);q=next;controls();update();}
$('onsite').onchange=refresh;
$('connect').onclick=()=>action(async()=>{lastFault='';await api('connect');await poll();message(config.backend==='simulation'?'已连接模拟器，所有反馈均为模拟数据。':'已连接读取，尚未接管手臂；金色为当前实测姿态。');});
$('adopt').onclick=()=>{try{adopt();message('已采用当前读数作为编辑值，未发送目标。');}catch(e){message(e.message,true);}};
$('prepare').onclick=()=>action(async()=>{lastFault='';sessionIntent=true;try{await api('acquire',{onsite:$('onsite').checked});adoptWhenReady=true;message('正在接管到当前姿态，不发送此前编辑值；请等待“等待单次发送”。');}catch(e){sessionIntent=false;throw e;}});
$('send').onclick=()=>action(async()=>{await api('target',{index:22+selected,q_rad:q[selected]});message('已发送 '+labels[selected]+' 目标 '+deg(q[selected]).toFixed(3)+'°，等待读数到位。');await poll();});
$('release').onclick=()=>action(async()=>{sessionIntent=false;await api('release');message('已请求停止调节并渐降接管权重，请等待交还完成。');await poll();});
$('quick-send').onclick=()=>$('send').click();$('quick-release').onclick=()=>$('release').click();
$('disconnect').onclick=()=>action(async()=>{sessionIntent=false;await api('disconnect');state={};measured.group.visible=false;update();message('已断开调试连接。');});
$('stop-service').onclick=()=>action(async()=>{await api('stop-service',{onsite:$('onsite').checked});message('已验证关闭内置手臂服务，等待状态刷新；尚未接管。');await poll();});
async function poll(){
  if(polling)return;polling=true;
  try{
    const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw Error('状态读取失败');state=await response.json();
    if(adoptWhenReady&&state.owned_by_this_page&&['acquiring','holding'].includes(state.phase)){adopt();adoptWhenReady=false;}
    if(state.fresh&&state.joints?.length===14){const actual=model.joints.map((_,i)=>state.joints.find(j=>j.index===22+i).q_rad);validateAngles(model,actual);setPose(measured,actual);measured.group.visible=true;}
    else measured.group.visible=false;
    for(let i=0;i<7;i++){const j=state.joints?.find(j=>j.index===22+i);$('actual'+i).textContent=j&&state.fresh?(config.backend==='simulation'?'模拟 ':'实测 ')+deg(j.q_rad).toFixed(3)+'°':'实测 —';}
    if(state.joints?.length===14){$('both-arms').innerHTML='<table class="data-table"><thead><tr><th>关节</th><th>左臂 / °</th><th>右臂 / °</th></tr></thead><tbody>'+labels.map((label,i)=>'<tr><td>'+label+'</td><td>'+deg(state.joints.find(j=>j.index===15+i).q_rad).toFixed(3)+'</td><td>'+deg(state.joints.find(j=>j.index===22+i).q_rad).toFixed(3)+'</td></tr>').join('')+'</tbody></table>';}
    if(state.fault&&state.fault!==lastFault){
      lastFault=state.fault;sessionIntent=false;
      message(faultText(state),true);
    }
    refresh();draw();
  }catch(e){state.fresh=false;sessionIntent=false;refresh();message('状态读取异常，已停止页面心跳，等待后端交还控制权。',true);}
  finally{polling=false;}
}
setInterval(()=>{if(config)poll();},500);
setInterval(async()=>{if(!sessionIntent||document.hidden)return;try{await api('heartbeat');}catch(e){sessionIntent=false;message(e.message,true);}},1000);
function leave(){if(!sessionIntent||!config)return;sessionIntent=false;navigator.sendBeacon('/api/release',new Blob([JSON.stringify({csrf:config.csrf,request_id:token()})],{type:'application/json'}));}
addEventListener('pagehide',leave);document.addEventListener('visibilitychange',()=>{if(document.hidden)leave();});
$('export').onclick=()=>{try{const data=exportAngles(model,q),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='g1-right-arm-debug-pose.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出编辑姿态，文件不包含实物执行授权。');}catch(e){message(e.message,true);}};
$('import').onclick=()=>$('file').click();
$('file').onchange=async()=>{try{const file=$('file').files[0];if(!file)return;if(file.size>100000)throw Error('姿态文件过大');q=importAngles(model,JSON.parse(await file.text()));controls();update();message('已导入编辑姿态，没有发送任何目标。');}catch(e){message('导入失败：'+e.message,true);}finally{$('file').value='';}};
controls();update();
try{config=await (await fetch('/api/config',{cache:'no-store'})).json();$('backend').textContent=config.backend==='simulation'?'模拟调试 · 不连接机器人':'实物接口 · 只读起步';$('backend').className='pill '+(config.backend==='simulation'?'neutral':'live');if(config.backend==='simulation'){$('actual-label').textContent='模拟反馈';$('adopt').textContent='采用模拟角度';$('connection-description').textContent='当前为模拟后端。所有读数、接管和单步发送都只在内存中演示，不连接机器人。';}await poll();}catch(e){message('服务配置读取失败，请刷新页面。',true);}
