import * as THREE from './vendor/three.module.min.js';
import {degreesToRaw, rawToDegrees, validatePose} from './mapping.mjs';
import {displayedPose, displayAngleScale, visualReference} from './visual-reference.mjs';
import {allowsTrialEdit} from './trial-edit.mjs';

const $ = id => document.getElementById(id);
const config = await fetch('/api/config').then(r=>r.json());
const modeNames={simulation:'离线仿真',readonly:'只读观察',live:'实物模式 · 默认关闭','single-step':'逐路 · 单步控制'};
let step=config.mode==='single-step'?config.single_step:null;
const scopes=step?(config.single_step_scopes||{[step.side]:step}):{};
const baselineTargets={};
const client=crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`;
let side='R', selected=3, lease=null, sequence=0, connected=false;
if(step)side=step.side;
let token='', polling=false, sending=false, pending=null, sendTimer=null, arming=false;
const targets={}, measured={L:null,R:null};
for (const s of ['L','R']) targets[s]=!config.mapping.hands[s].verified?[...visualReference[s].raw]:config.mapping.hands[s].channels.map((c,i)=>degreesToRaw(c,c.points[0].degrees+(c.points.at(-1).degrees-c.points[0].degrees)*(i===4?.1:.16)));
for(const [s,scope] of Object.entries(scopes)){targets[s]=[...scope.hold_targets];baselineTargets[s]=[...scope.hold_targets];}
const enabledChannel=i=>step&&side===step.side&&(step.channels||[step.channel]).includes(i);
const stepBounds=i=>step.schema>=2?[step.min_raw[i],step.max_raw[i]]:[step.min_raw,step.max_raw];
const channels=()=>config.mapping.hands[side].channels;
const calibrated=()=>config.mapping.hands[side].verified;
const message=(text,error=false)=>{$('message').textContent=text;$('message').classList.toggle('error',error);};
$('mode').textContent=modeNames[config.mode];
$('connection-description').textContent=config.mode==='simulation'?'离线编辑不会连接或驱动实物。此模式无法开启实物联动。':config.mode==='readonly'?'仅显示实测位置；鼠标编辑不会发送动作。':'开启联动前须核实标定和现场条件。每个目标沿用仓库的速度 300 和到位等待。';
if(step){$('single-step-panel').hidden=false;$('arm').textContent='准备单步控制';$('disarm').textContent='取消准备';}
function builtinConnected(value){if(config.builtin_connection){$('token').hidden=value;document.querySelector('label[for="token"]').hidden=value;$('connect').textContent=value?'重新连接读取':'连接并记住本浏览器';}}
builtinConnected(false);
if(config.mode==='simulation') {
  $('connect').disabled=true;$('token').disabled=true;$('onsite').disabled=true;
}

async function api(path,data) {
  const response=await fetch(path,{method:data?'POST':'GET',headers:{'X-Hand-Token':token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined,signal:AbortSignal.timeout(data?30000:9000)});
  const value=await response.json();
  if(!response.ok) throw Error(value.error || '连接失败');
  return value;
}
function sessionBody(extra={}) {return {client,lease,...extra};}
function clearSession() {
  lease=null;pending=null;clearTimeout(sendTimer);updateConnection();
}
function updateConnection() {
  const writable=step?side===step.side:config.mode==='live'&&config.mapping.hands[side].verified;
  $('arm').disabled=!writable||!connected||!!lease||sending||arming||!$('onsite').checked;
  $('send-step').disabled=!step||!lease||sending||arming;
  $('disarm').disabled=!lease;
  $('adopt').disabled=!!step||!measured[side]||!!lease||sending||arming;
  $('left').disabled=$('right').disabled=!!lease||sending||arming;
  $('import').disabled=!!step||!!lease||sending||arming;
  $('token').disabled=!!lease||sending||arming||config.mode==='simulation';
  $('connect').disabled=!!lease||sending||arming||config.mode==='simulation';
  $('onsite').disabled=!!lease||arming||config.mode==='simulation';
  $('connection-state').textContent=lease?(step?'已准备 · 尚未发送':'联动中'):sending?'等待动作结果':connected?'已连接 · 联动关闭':'离线';
  $('connection-state').classList.toggle('neutral',!lease);
}
const rows=config.names.map((name,index)=>{
  const row=document.createElement('div');row.className='channel';
  row.innerHTML=`<div class="channel-top"><label for="angle-${index}">${name}</label><div class="value"><input id="number-${index}" type="number" step="0.1" aria-label="${name}编辑位置"><span id="unit-${index}"></span></div></div><input id="angle-${index}" type="range" step="0.1" aria-label="${name}位置"><div class="raw-row"><span id="raw-${index}"></span><span class="measured" id="actual-${index}">实测 —</span></div>`;
  $('controls').append(row);
  row.addEventListener('pointerdown',()=>{selected=index;renderControls();});
  for(const input of [row.querySelector('input[type=range]'),row.querySelector('input[type=number]')]) {
    input.addEventListener('input',()=>{
      const value=input.valueAsNumber;
      if(Number.isFinite(value)) editPosition(index,value);
    });
    input.addEventListener('change',()=>renderControls());
    input.addEventListener('blur',()=>renderControls());
  }
  return row;
});
function renderControls() {
  $('selection').textContent=config.names[selected];
  const handName=side==='R'?'右手':'左手';
  $('side-label').textContent=step&&side===step.side?handName+(step.schema===4?' · 四指可组合控制':step.schema>=2?' · 全部手指逐路试控':' · 仅食指可调'):handName+(step?' · 只读观察':' · 6 路控制');
  if(step){
    $('mode').textContent=step.schema===4?'四指多路 · 手动控制':modeNames[config.mode];
    $('arm').textContent=step.schema===4?'准备手动控制':'准备单步控制';
    $('step-title').textContent=step.schema===4?'右手四指多路控制':step.schema>=2?'右手逐路试控':'食指单步控制';
    $('step-description').textContent=step.schema===4?'小指、无名指、中指、食指可同时编辑，各自范围 0–500，一次发送组合目标。拇指弯曲和旋转须各自单独发送；四指组合时两路拇指保持设备目标。':step.schema===3?'前五路可调 0–500，拇指旋转保持 950–1000。每次只改一路并手动发送；其他路保持设备目标。此范围用于试控，尚未完成实物角度标定。':step.schema===2?'全部手指可选，拇指弯曲和旋转分开控制。每次只改一路；本次窗口为基准目标附近 50。其他路保持设备目标，不用反馈值替换。':'仅左手食指，原始位置 82–200。其他五路保持设备目标，不使用反馈值替换。';
    $('connection-description').textContent=step.schema===4?'先准备，再编辑四指并手动发送一次。鼠标编辑不会自动发送。速度沿用仓库的 300，仍按六路误差小于 8 判断整手到位；模型角度尚未标定。':'先准备，再调整一路并手动发送。鼠标编辑不会自动发送。速度沿用仓库的 300，仍按六路误差小于 8 判断整手到位；运动范围尚未标定。';
    $('send-step').textContent=step.schema>=2?'发送一次右手目标':'发送一次食指目标';
    if([5,6].includes(step.schema)){
      $('mode').textContent='左右手全路 · 手动控制';
      $('side-label').textContent=handName+' · 六路可组合控制';
      $('arm').textContent='准备'+handName+'控制';
      $('step-title').textContent=handName+'全路控制';
      $('step-description').textContent=`小指、无名指、中指、食指、拇指弯曲和旋转均可组合编辑。前五路 0–${step.max_raw[0]}，拇指旋转 950–1000。每次只发送当前这只手；另一只手保持设备目标。`;
      $('connection-description').textContent='先准备当前手，再编辑六路并手动发送一次。切换左右手需先取消准备。速度沿用仓库的 300，仍按六路误差小于 8 判断到位；模型角度尚未标定。';
      $('send-step').textContent='发送一次'+handName+'目标';
    }
  }
  $('left').classList.toggle('active',side==='L');$('right').classList.toggle('active',side==='R');
  $('model-note').textContent=calibrated()?'已加载现场标定':'初始外观已对齐 · 运动范围未标定';
  channels().forEach((channel,i)=>{
    rows[i].classList.toggle('selected',i===selected);
    const value=calibrated()?rawToDegrees(channel,targets[side][i]):targets[side][i];
    const rawBounds=channel.points.map(point=>point.raw);
    $('unit-'+i).textContent=calibrated()?'°':'/1000';
    for(const prefix of ['angle-','number-']) {
      const input=$(prefix+i);input.min=calibrated()?channel.points[0].degrees:Math.min(...rawBounds);input.max=calibrated()?channel.points.at(-1).degrees:Math.max(...rawBounds);input.step=calibrated()?'0.1':'1';
      input.disabled=!!step&&(!enabledChannel(i)||sending||arming);
      if(enabledChannel(i)){[input.min,input.max]=stepBounds(i);}
      if(document.activeElement!==input) input.value=calibrated()?value.toFixed(1):value;
    }
    $('raw-'+i).textContent='原始位置 '+targets[side][i]+' / 1000'+(enabledChannel(i)?' · 可调 '+stepBounds(i).join('–'):'');
    $('actual-'+i).textContent=measured[side]?'实测 '+measured[side][i]:'实测 —';
  });
  updateConnection();
}
function editAngle(index,value) {
  const channel=channels()[index];
  value=Math.max(channel.points[0].degrees,Math.min(channel.points.at(-1).degrees,value));
  setRawPosition(index,degreesToRaw(channel,value));
}
function editPosition(index,value) {
  if(calibrated())return editAngle(index,value);
  const rawBounds=channels()[index].points.map(point=>point.raw);
  setRawPosition(index,Math.round(Math.max(Math.min(...rawBounds),Math.min(Math.max(...rawBounds),value))));
}
function setRawPosition(index,value) {
  if(step&&(!enabledChannel(index)||sending||arming))return message('此通道当前不能编辑。',true);
  if(step){
    const [low,high]=stepBounds(index);
    if(value<low||value>high)return message(`目标须在 ${low}–${high}；保留上次有效目标。`,true);
    if(step.schema>=2&&!allowsTrialEdit(step,baselineTargets[side],targets[side].map((v,i)=>i===index?value:v)))return message(step.schema===4?'四指可以组合编辑；拇指须单独控制，请先还原其他编辑或发送当前目标。':'每次只改一路；请先还原上一处编辑或发送该目标。',true);
  }
  targets[side][index]=value;selected=index;renderControls();
  if(lease&&!step) {
    pending=[...targets[side]];clearTimeout(sendTimer);sendTimer=setTimeout(flush,200);
  }
}
async function flush() {
  if(!lease||sending||!pending) return;
  const values=pending;pending=null;sending=true;updateConnection();
  message(step?'正在执行这一次目标，等待仓库控制脚本返回。':'正在等待仓库控制脚本返回，期间只保留最新编辑目标。');
  try {
    const result=await api('/api/pose',sessionBody({side,values,seq:++sequence}));
    measured[side]=result.actual;
    if(step){const changed=result.target.map((v,i)=>v!==baselineTargets[side][i]?`${config.names[i]}目标 ${v}，反馈 ${result.actual[i]}`:'').filter(Boolean).join('；');baselineTargets[side]=[...result.target];clearSession();const offsets=result.errors.map((v,i)=>v>=8?`${config.names[i]}偏差 ${v}`:'').filter(Boolean).join('，');message(`单步结束：${changed||'目标未改变'}。${result.reached?'整手已到位。':`整手未到位（${offsets}），不自动重试。`}`,!result.reached);}
    else if(!result.reached||!result.session_active) {clearSession();message(result.reached?'本次会话已关闭，后续目标已清除。':'目标未到位，已关闭联动；请查看实物，不自动重试。',true);}
    else message('目标已到位 · 实测位置已更新');
  } catch(error) {clearSession();message(error.message+'；已关闭联动，已发出的目标可能仍在执行。',true);}
  finally {sending=false;renderControls();if(pending&&lease&&!step) sendTimer=setTimeout(flush,200);}
}
$('send-step').onclick=()=>{if(!step||!lease||sending)return;pending=[...targets[side]];flush();};
async function poll() {
  if(!connected||polling) return;
  polling=true;const requestedSide=side;
  try {
    const status=await api('/api/status/'+requestedSide);
    if(side!==requestedSide)return;
    measured[requestedSide]=status.actual;
    $('feedback-age').textContent=status.age_seconds===null?'尚无实物读数':`实测更新于 ${status.age_seconds.toFixed(1)} 秒前${status.busy?' · 动作执行中':''}`;
    if(status.error) throw Error(status.error);
    if(lease&&!status.armed) {clearSession();message(status.fault||'控制会话已结束，未发送后续目标。',true);}
    renderControls();
  } catch(error) {connected=false;clearSession();message(error.message+'；连接已断开，后续目标已清除。',true);}
  finally {polling=false;}
}
$('connect').onclick=async()=>{
  token=$('token').value.trim();
  if(!config.builtin_connection&&!token)return message('请输入启动窗口显示的连接口令。',true);
  try {
    if(config.builtin_connection&&token){await api('/api/connect',{});token='';$('token').value='';}
    connected=true;await poll();builtinConnected(connected);
    if(connected)message(config.builtin_connection?'内置连接已就绪，实物联动仍关闭。':'已连接读取，当前实物联动仍关闭。');
    else if(config.builtin_connection)message('本浏览器尚未认证，请首次连接后再使用自动读取。');
  }catch(error){connected=false;builtinConnected(false);message(error.message,true);}
};
$('onsite').onchange=updateConnection;
$('adopt').onclick=()=>{
  if(step)return message('试控使用设备保存的目标，不能采用实测反馈替换。',true);
  try {measured[side].forEach((v,i)=>rawToDegrees(channels()[i],v));targets[side]=[...measured[side]];renderControls();message('已采用实测位置；没有发送动作。');}
  catch(error){message(error.message+'；保留原编辑姿态。',true);}
};
$('arm').onclick=async()=>{
  if(!connected||!$('onsite').checked) return;
  arming=true;updateConnection();
  try {
    const result=await api('/api/arm',{client,side});
    lease=result.lease;sequence=0;targets[side]=[...(result.target||result.actual)];measured[side]=[...result.actual];pending=null;
    if(step)baselineTargets[side]=[...result.target];
    message(step?(step.schema===4?'已准备，尚未发送。可以编辑四指后一次发送；两路拇指保持设备目标。':'已准备，尚未发送。调整一路后点击发送；其他路保持设备保存的目标。'):'实物联动已开启，从当前实测姿态开始；调整手指后才发送目标。');
    if([5,6].includes(step?.schema))message('已准备，尚未发送。当前手六路可以组合编辑，点击发送后执行一次；另一只手保持原目标。');
  } catch(error){message(error.message,true);}
  finally {arming=false;renderControls();}
};
$('disarm').onclick=async()=>{
  const body=sessionBody();clearSession();
  try{await api('/api/disarm',body);message('后续发送已关闭；已发出的目标可能继续执行。');}
  catch(error){message(error.message+'；本页已清除后续发送，服务端会话将超时失效。',true);}
};
setInterval(poll,1000);
setInterval(async()=>{if(!lease)return;try{await api('/api/heartbeat',sessionBody());}catch(error){clearSession();message(error.message+'；后续发送已关闭。',true);}},2000);
function switchSide(value) {if(lease||sending||arming)return;side=value;if(step)step=scopes[value]||config.single_step;renderControls();if(connected)poll();}
$('left').onclick=()=>switchSide('L');$('right').onclick=()=>switchSide('R');
$('export').onclick=()=>{
  const data={schema:1,side,raw:targets[side],model_degrees:channels().map((c,i)=>rawToDegrees(c,targets[side][i])),mapping_sha256:config.mapping_sha256,exported_at:new Date().toISOString()};
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download=`hand-${side}-pose.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
$('import').onclick=()=>$('file').click();
$('file').onchange=async()=>{
  try {
    if(lease||sending||arming)throw Error('请先关闭实物联动');
    const file=$('file').files[0];if(!file)return;if(file.size>16000)throw Error('姿态文件过大');
    const pose=validatePose(JSON.parse(await file.text()),config.mapping_sha256);
    pose.raw.forEach((v,i)=>rawToDegrees(config.mapping.hands[pose.side].channels[i],v));
    side=pose.side;targets[side]=pose.raw;renderControls();message('已导入离线姿态；没有发送动作。');
  } catch(error){message(error.message,true);}
  finally{$('file').value='';}
};
document.addEventListener('visibilitychange',()=>{if(document.hidden&&lease)$('disarm').click();});
window.addEventListener('pagehide',()=>{if(lease){fetch('/api/disarm',{method:'POST',headers:{'X-Hand-Token':token,'Content-Type':'application/json'},body:JSON.stringify(sessionBody()),keepalive:true});clearSession();}});

// Parameterized schematic kinematics. These axes and mimic ratios require field calibration.
const viewport=$('viewport'), pickable=[];
const scene=new THREE.Scene(), camera=new THREE.PerspectiveCamera(33,1,.01,100);
const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setClearColor(0,0);viewport.appendChild(renderer.domElement);
scene.add(new THREE.HemisphereLight(0xffffff,0x83959e,3));
const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(-3,6,5);scene.add(light);
const grid=new THREE.GridHelper(7,35,0xc6d5d9,0xdce5e7);grid.position.y=-.68;scene.add(grid);
const geo=config.mapping.model;
const axis=array=>new THREE.Vector3(...array).normalize();
function makeHand(feedback=false) {
  const root=new THREE.Group();scene.add(root);
  const links=[],balls=[],pivots=[],secondary=[];
  const material=new THREE.MeshStandardMaterial({color:feedback?0xd69c4c:0x409095,roughness:.36,metalness:.18,transparent:feedback,opacity:feedback?.6:1,wireframe:feedback,depthWrite:!feedback});
  const palmMaterial=new THREE.MeshStandardMaterial({color:feedback?0xd69c4c:0xd5dfe2,roughness:.46,metalness:.2,transparent:feedback,opacity:feedback?.25:1,wireframe:feedback,depthWrite:!feedback});
  const ballMaterial=new THREE.MeshStandardMaterial({color:feedback?0xd69c4c:0x596f79,roughness:.42,transparent:feedback,opacity:feedback?.65:1,wireframe:feedback,depthWrite:!feedback});
  function box(parent,length,width,channel) {
    const object=new THREE.Mesh(new THREE.BoxGeometry(width,length,width*.88),material.clone());
    object.position.y=length/2;parent.add(object);object.userData.channel=channel;links.push(object);if(!feedback)pickable.push(object);return object;
  }
  function ball(parent,channel,radius=.095) {
    const object=new THREE.Mesh(new THREE.SphereGeometry(radius,20,14),ballMaterial.clone());
    parent.add(object);object.userData.channel=channel;balls.push(object);if(!feedback)pickable.push(object);return object;
  }
  const palm=new THREE.Mesh(new THREE.BoxGeometry(.99,1.02,.23),palmMaterial);palm.position.y=-.08;root.add(palm);
  const wrist=new THREE.Mesh(new THREE.CylinderGeometry(.28,.3,.24,24),palmMaterial);wrist.position.y=-.68;root.add(wrist);
  const xs=[.36,.12,-.12,-.36],lengths=[[.39,.37],[.52,.44],[.59,.47],[.49,.43]];
  for(let i=0;i<4;i++) {
    const pivot=new THREE.Group();pivot.position.set(xs[i],.44,0);root.add(pivot);pivots[i]=pivot;ball(pivot,i);box(pivot,lengths[i][0],.145,i);
    const next=new THREE.Group();next.position.y=lengths[i][0];pivot.add(next);secondary[i]=next;ball(next,i,.085);box(next,lengths[i][1],.135,i);const tip=new THREE.Group();tip.position.y=lengths[i][1];next.add(tip);ball(tip,i,.067);
  }
  const thumbRest=new THREE.Group();thumbRest.position.set(...geo.thumb_base);thumbRest.rotation.z=THREE.MathUtils.degToRad(geo.thumb_rest_yaw_degrees);root.add(thumbRest);
  const rotate=new THREE.Group();thumbRest.add(rotate);pivots[5]=rotate;ball(rotate,5,.13);
  const bend=new THREE.Group();bend.position.y=.1;rotate.add(bend);pivots[4]=bend;ball(bend,4,.09);box(bend,.31,.17,4);
  const middle=new THREE.Group();middle.position.y=.31;bend.add(middle);ball(middle,4,.085);box(middle,.23,.15,4);
  const end=new THREE.Group();end.position.y=.23;middle.add(end);ball(end,4,.075);box(end,.23,.135,4);
  const tip=new THREE.Group();tip.position.y=.23;end.add(tip);ball(tip,4,.065);
  return {root,links,balls,pivots,secondary,middle,end,feedback,thumbRest};
}
const hand=makeHand(), actualHand=makeHand(true);
function poseHand(model,raw) {
  if(!raw){model.root.visible=false;return;}
  let angles,display;
  try{display=displayedPose(config.mapping,side,raw);angles=display.angles.map(THREE.MathUtils.degToRad);}
  catch{model.root.visible=false;return;}
  model.root.visible=true;model.root.scale.x=side==='L'?-1:1;
  model.thumbRest.position.set(...display.thumbBase);model.thumbRest.rotation.z=THREE.MathUtils.degToRad(display.thumbRestYaw);
  for(let i=0;i<4;i++) {model.pivots[i].quaternion.setFromAxisAngle(axis(geo.finger_axis),angles[i]);model.secondary[i].quaternion.setFromAxisAngle(axis(geo.finger_axis),angles[i]*geo.finger_secondary_ratio);}
  model.pivots[5].quaternion.setFromAxisAngle(axis(display.thumbRotationAxis),angles[5]);
  model.pivots[4].quaternion.setFromAxisAngle(axis(geo.thumb_bend_axis),angles[4]);
  model.middle.quaternion.setFromAxisAngle(axis(geo.thumb_bend_axis),angles[4]*geo.thumb_secondary_ratio);
  model.end.quaternion.setFromAxisAngle(axis(geo.thumb_bend_axis),angles[4]*geo.thumb_secondary_ratio*.75);
  if(!model.feedback) for(const object of [...model.links,...model.balls])object.material.color.setHex(object.userData.channel===selected?0xdda46a:model.balls.includes(object)?0x596f79:0x409095);
}
let yaw=.48,pitch=.2,distance=4.8;
const focus=new THREE.Vector3(0,.43,0);
function updateCamera(){camera.position.set(focus.x+distance*Math.sin(yaw)*Math.cos(pitch),focus.y+distance*Math.sin(pitch),focus.z+distance*Math.cos(yaw)*Math.cos(pitch));camera.lookAt(focus);}
function resize(){const w=viewport.clientWidth,h=viewport.clientHeight;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();}
new ResizeObserver(resize).observe(viewport);resize();updateCamera();
$('front').onclick=()=>{yaw=0;pitch=0;updateCamera();};
$('oblique').onclick=$('reset-camera').onclick=()=>{yaw=.48;pitch=.2;distance=4.8;updateCamera();};
const raycaster=new THREE.Raycaster();let drag=null;
viewport.addEventListener('pointerdown',event=>{
  const rect=viewport.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),camera);
  const hit=raycaster.intersectObjects(pickable,false)[0];
  if(hit){selected=hit.object.userData.channel;renderControls();}
  drag={x:event.clientX,y:event.clientY,index:hit?selected:null,value:hit?rawToDegrees(channels()[selected],targets[side][selected]):0,direction:hit?displayAngleScale(config.mapping,side,selected):1,yaw,pitch};
  viewport.setPointerCapture(event.pointerId);viewport.classList.toggle('dragging',!!hit);
});
viewport.addEventListener('pointermove',event=>{
  if(!drag)return;
  if(drag.index!==null)editAngle(drag.index,drag.value+(event.clientY-drag.y)*.3*drag.direction);
  else{yaw=drag.yaw-(event.clientX-drag.x)*.008;pitch=Math.max(-1.1,Math.min(1.1,drag.pitch+(event.clientY-drag.y)*.008));updateCamera();}
});
for(const name of ['pointerup','pointercancel','lostpointercapture'])viewport.addEventListener(name,()=>{drag=null;viewport.classList.remove('dragging');});
viewport.addEventListener('wheel',event=>{event.preventDefault();distance=Math.max(2.8,Math.min(8,distance+event.deltaY*.003));updateCamera();},{passive:false});
function animate(){requestAnimationFrame(animate);poseHand(hand,targets[side]);poseHand(actualHand,measured[side]);renderer.render(scene,camera);}
renderControls();animate();
if(config.builtin_connection)$('connect').click();
