import * as THREE from './three.module.min.js';
import {model} from './model.mjs';
import {snapshot} from './snapshot.mjs';
import {hand} from './hand-geometry.mjs';
import {rad,deg,forward,solveHeight,smoothPose,previewDuration,exportPose,importPose} from './kinematics.mjs';
const $=id=>document.getElementById(id);
const labels=['肩前后摆','肩侧摆','肩旋转','肘弯曲','腕旋转','腕俯仰','腕偏转'];
let q=Array(7).fill(0),reference=[...q],animation=null,lastPose=null;
const config=()=>{const shoulderTopHeight=Number($('shoulder').value)/100,shoulderTopToJoint=Number($('shoulder-offset').value)/100;return {shoulderHeight:shoulderTopHeight-shoulderTopToJoint,shoulderTopHeight,shoulderTopToJoint,targetHeight:Number($('target').value)/100,tipOffset:[...hand.tip],endpoint:'official_ftp',fingerBend:rad(Number($('finger').value))};};
function status(message,error=false){$('status').textContent=message;$('status').className=error?'error':'';}
function controls(){q.forEach((v,i)=>{const d=deg(v).toFixed(2);$('q'+i).value=d;$('n'+i).value=d;});}
function stop(){animation=null;$('stop').disabled=true;$('play').disabled=false;$('solve').disabled=false;}
model.joints.forEach((joint,i)=>{
  const row=document.createElement('div');row.className='joint';
  row.innerHTML=`<div class="joint-head"><label for="q${i}">${labels[i]}</label><input id="n${i}" aria-label="${labels[i]}角度" type="number" min="${deg(joint.lower)}" max="${deg(joint.upper)}" step="0.1" value="0"></div><input id="q${i}" aria-label="${labels[i]}滑条" type="range" min="${deg(joint.lower)}" max="${deg(joint.upper)}" step="any" value="0">`;
  $('joints').append(row);
  for(const id of ['q'+i,'n'+i])$(id).addEventListener(id[0]==='q'?'input':'change',()=>{
    stop();const value=rad(Number($(id).value));
    if(!Number.isFinite(value)||value<joint.lower||value>joint.upper){status('该关节输入超出模型范围。',true);return;}
    q[i]=value;controls();update();
  });
});

const scene=new THREE.Scene();
const camera=new THREE.PerspectiveCamera(40,1,0.01,30);
let renderer;
try {renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});}
catch(error){$('scene').append(document.createTextNode('当前浏览器无法创建三维画布；高度计算和姿态导出仍可使用。'));}
const canvas=renderer?.domElement;
if(renderer){renderer.setPixelRatio(Math.min(devicePixelRatio,2));$('scene').append(canvas);}
scene.add(new THREE.HemisphereLight(0xffffff,0x829b8d,2.7));
const light=new THREE.DirectionalLight(0xffffff,3.3);light.position.set(2,4,3);scene.add(light);
const grid=new THREE.GridHelper(2,20,0x9eb4a9,0xcdd8d0);grid.position.set(0.2,0,0);scene.add(grid);
const arm=new THREE.Group();scene.add(arm);
const targetMaterial=new THREE.MeshBasicMaterial({color:0xc18c4b,transparent:true,opacity:0.055,side:THREE.DoubleSide,depthWrite:false});
const targetPlane=new THREE.Mesh(new THREE.PlaneGeometry(1.15,.85),targetMaterial);targetPlane.rotation.x=-Math.PI/2;targetPlane.position.x=.2;scene.add(targetPlane);
const targetLine=new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-.35,0,0),new THREE.Vector3(.8,0,0)]),new THREE.LineDashedMaterial({color:0xb87b36,dashSize:.025,gapSize:.016}));targetLine.computeLineDistances();scene.add(targetLine);
const bodyMaterial=new THREE.MeshStandardMaterial({color:0xd3dfd7,roughness:.85,transparent:true,opacity:.7});
const body=new THREE.Mesh(new THREE.BoxGeometry(.16,.35,.26),bodyMaterial);scene.add(body);
const armMat=new THREE.MeshStandardMaterial({color:0x218e89,roughness:.45,metalness:.12});
const jointMat=new THREE.MeshStandardMaterial({color:0xe9f2eb,roughness:.35,metalness:.2});
const tipMat=new THREE.MeshStandardMaterial({color:0xe1ac52,roughness:.4});
const V=p=>new THREE.Vector3(p[0],p[2],-p[1]);
const cylinders=[],spheres=[];
for(let i=0;i<10;i++) {const mesh=new THREE.Mesh(new THREE.CylinderGeometry(i>=8?.009:.017,i>=8?.009:.017,1,18),i>=8?tipMat:armMat);arm.add(mesh);cylinders.push(mesh);}
for(let i=0;i<11;i++){const mesh=new THREE.Mesh(new THREE.SphereGeometry(i>=9?.014:.024,20,14),i===10?tipMat:jointMat);arm.add(mesh);spheres.push(mesh);}
let azimuth=1.05,elevation=.18,distance=2.35;
function draw(){
  if(!renderer)return;
  const center=new THREE.Vector3(.13,.90,0);
  camera.position.set(center.x+distance*Math.sin(azimuth)*Math.cos(elevation),center.y+distance*Math.sin(elevation),center.z+distance*Math.cos(azimuth)*Math.cos(elevation));camera.lookAt(center);renderer.render(scene,camera);
}
function update(display=q){
  try {
    const c=config(),pose=forward(model,display,c);lastPose=pose;
    $('target-stat').textContent=(c.targetHeight*100).toFixed(1)+' cm';
    $('axis-note').textContent='轴心估算高度 '+(c.shoulderHeight*100).toFixed(1)+' cm；目标比肩顶高 '+((c.targetHeight-c.shoulderTopHeight)*100).toFixed(1)+' cm。轴心偏移未测量，当前仅作示意。';
    $('height-stat').textContent=(pose.tip[2]*100).toFixed(1)+' cm';
    $('finger-value').textContent=deg(c.fingerBend).toFixed(1)+'°';
    const error=(pose.tip[2]-c.targetHeight)*100;
    $('error-stat').textContent=(error>=0?'+':'')+error.toFixed(1)+' cm';
    pose.points.forEach((point,i)=>spheres[i].position.copy(V(point)));
    cylinders.forEach((mesh,i)=>{const a=V(pose.points[i]),b=V(pose.points[i+1]),delta=b.clone().sub(a);mesh.position.copy(a).add(b).multiplyScalar(.5);mesh.scale.y=delta.length();mesh.visible=delta.length()>1e-6;if(mesh.visible)mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),delta.normalize());});
    body.position.set(-.035,c.shoulderHeight-.13,-.14);
    targetPlane.position.y=c.targetHeight;targetLine.position.y=c.targetHeight;
    draw();return true;
  }catch(error){status(error.message,true);return false;}
}
if(renderer){
  new ResizeObserver(()=>{const r=$('scene').getBoundingClientRect();camera.aspect=r.width/r.height;camera.updateProjectionMatrix();renderer.setSize(r.width,r.height);draw();}).observe($('scene'));
  let pointer=null;
  canvas.addEventListener('pointerdown',e=>{pointer=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!pointer)return;azimuth-=(e.clientX-pointer[0])*.006;elevation=Math.max(-.5,Math.min(.9,elevation+(e.clientY-pointer[1])*.005));pointer=[e.clientX,e.clientY];draw();});
  for(const event of ['pointerup','pointercancel'])canvas.addEventListener(event,()=>{pointer=null;});
  canvas.addEventListener('wheel',e=>{e.preventDefault();distance=Math.max(1.3,Math.min(4,distance+e.deltaY*.001));draw();},{passive:false});
}
$('front').onclick=()=>{azimuth=Math.PI/2;elevation=.05;draw();};
$('side').onclick=()=>{azimuth=0;elevation=.05;draw();};
for(const id of ['shoulder','shoulder-offset','target','finger'])$(id).addEventListener('input',()=>{stop();update();});
$('solve').onclick=()=>{
  stop();try{const result=solveHeight(model,q,config());q=result.q;controls();update();status('已生成几何候选：估算高度误差 '+(result.error*100).toFixed(2)+' cm。仅调整肩前后摆、肘弯曲和腕俯仰；四个其他关节保持原值。未验证实物可执行性。');}catch(error){status(error.message,true);}
};
$('reference').onclick=()=>{stop();if(update()){reference=[...q];status('已将当前模型姿态设为放下参考；仅保存在本次页面中。');}};
$('zero').onclick=()=>{stop();q=Array(7).fill(0);controls();update();status('已载入模型零位。模型零位不等同于实物安全放下姿态。');};
$('snapshot').onclick=()=>{stop();q=[...snapshot.right_q_rad];reference=[...q];controls();update();status('已载入 '+snapshot.observed_date+' 的历史只读关节快照作为放下参考；这是离线历史数据，不代表当前实物姿态。');};
$('play').onclick=()=>{
  if(!update())return;
  const goal=[...q],duration=previewDuration(reference,goal);
  animation={start:performance.now(),goal,duration,reference:[...reference]};$('stop').disabled=false;$('play').disabled=true;$('solve').disabled=true;
  function frame(now){
    if(!animation)return;
    const a=animation,t=(now-a.start)/1000,d=a.duration;
    if(t<d){q=smoothPose(a.reference,a.goal,t/d);status('预演：抬手 · '+Math.round(t/d*100)+'%');}
    else if(t<d+1){q=[...a.goal];status('预演：到达候选高度，保持 1 秒');}
    else if(t<2*d+1){q=smoothPose(a.goal,a.reference,(t-d-1)/d);status('预演：放下 · '+Math.round((t-d-1)/d*100)+'%');}
    else{q=[...a.reference];stop();controls();update();status('预演完成：已回到放下参考。按压、控制权交接与导航尚未接入。');return;}
    controls();update();requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
};
$('stop').onclick=()=>{stop();status('预演已停止，保留当前模型姿态。');};
$('export').onclick=()=>{
  try{const data=exportPose(model,q,config());data.lowering_reference_q_rad=[...reference];
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='g1-right-arm-height-offline.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);status('已导出离线候选姿态。');
  }catch(error){status(error.message,true);}
};
$('import').onclick=()=>$('file').click();
$('file').onchange=async()=>{
  stop();try{const file=$('file').files[0];if(!file)return;if(file.size>100000)throw Error('姿态文件过大');const value=JSON.parse(await file.text());const data=importPose(model,value);if(data.config.endpoint!=='official_ftp')throw Error('需要包含已核对 RH56DFTP 几何的姿态文件');
    // Validate the optional reference too; never honor execution/calibration flags.
    const nextReference=value.lowering_reference_q_rad??data.q;forward(model,nextReference,data.config);
    q=data.q;reference=[...nextReference];$('shoulder').value=(data.config.shoulderTopHeight??data.config.shoulderHeight)*100;$('shoulder-offset').value=(data.config.shoulderTopToJoint??0)*100;$('target').value=data.config.targetHeight*100;$('finger').value=deg(data.config.fingerBend);controls();update();status('已导入离线姿态。实物执行和标定状态始终为未授权、未标定。');
  }catch(error){status('导入失败：'+error.message,true);}finally{$('file').value='';}
};
controls();update();
