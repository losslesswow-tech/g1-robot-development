const $=id=>document.getElementById(id);
const pageId=crypto.randomUUID().replaceAll('-','');
const nodes=['prepared','raised','pressed','withdrawn','lowered','released'];
const labels={prepared:'准备完成',raised:'抬手节点',pressed:'按压段结束',withdrawn:'撤回节点',lowered:'放下节点',released:'已交还'};
const stages={acquire:'匹配实测姿态并接管',default:'过渡到默认姿态',raise:'抬手',press:'执行按压段',withdraw:'撤回',lower:'放下'};
const errors={connect_readonly_first:'请先连接读取。',action_already_active:'已有动作正在执行。',target_locked_during_action:'本次坐标已锁定，请结束后修改。',node_order_required:'请按节点顺序执行下一段。',page_heartbeat_timeout:'页面心跳中断，已退出动作。',node_hold_timeout:'节点停留超时，已退出动作。',session_deadline:'本次接管已到时限，已退出动作。',worker_disconnected:'动作连接已断开，请检查现场。',regular_standing_mode_unconfirmed:'未确认常规运控站立状态，动作未启动。',builtin_arm_service_stop_unconfirmed:'未确认内置手臂服务停止，动作未启动。',control_not_enabled_on_server:'当前服务仅允许读取。',this_page_does_not_own_control:'动作由另一页面控制。',cannot_pause_this_stage:'准备阶段暂不支持暂停，可停止并交还。',use_release_during_acquisition:'准备过程中请使用停止并交还。'};
let config=null,state={},busy=false,polling=false,lastHeartbeat=0,message='';
const targetInputs=['dc-x','dc-y','dc-z'].map($);
function target(){
  const xyz=targetInputs.map(input=>{
    if(!input.value.trim()||!input.checkValidity())throw Error('请填写范围内的 XYZ 坐标，单位 m。');
    const value=Number(input.value);if(!Number.isFinite(value))throw Error('坐标必须是有限数值。');return value;
  });
  return xyz;
}
function note(){
  try{const xyz=target();const tested=xyz[0]===.5&&xyz[1]===-.23&&[.31,.5].includes(xyz[2]);$('dc-target-note').textContent=(tested?'已完成整组抬臂实测':'该坐标组合尚未实测')+'：['+xyz.map(v=>v.toFixed(2)).join(', ')+'] m。尚未验证实体按钮按下。';}
  catch(error){$('dc-target-note').textContent=error.message;}
}
async function jsonFetch(url,options={}){
  const response=await fetch(url,{...options,signal:AbortSignal.timeout(10000)});
  const value=await response.json();if(!response.ok)throw Error(errors[value.error]||value.error||'请求失败');return value;
}
async function post(action,data={},quiet=false){
  if(!config)throw Error('页面配置尚未加载。');
  if(!quiet){busy=true;message='';render();}
  try{
    const result=await jsonFetch('/api/dream/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,csrf:config.csrf,page_id:pageId,request_id:crypto.randomUUID().replaceAll('-','')})});
    if(result.state)state=result.state;
    if(action==='configure')message='目标坐标已保存；未启动动作。';
    if(action==='connect')message='正在只读连接，请稍候。';
    return result;
  }catch(error){if(!quiet)message=error.message;throw error;}
  finally{if(!quiet){busy=false;render();}}
}
function on(id,handler){$(id).onclick=()=>Promise.resolve().then(handler).catch(error=>{message=error.message;render();});}
function render(){
  const active=!!state.active,owned=!!state.owned_by_this_page,can=!!config?.control_enabled;
  $('dc-backend').textContent=!config?'读取页面配置中':config.backend==='mock'?'界面模拟 · 不连接机器人':active?'真机后端 · 正在控制上肢':state.connected?'真机后端 · 已只读连接':'真机后端 · 未连接';
  for(const input of targetInputs)input.disabled=active||busy;
  for(const id of ['dc-preset031','dc-preset050','dc-save','dc-stop-at'])$(id).disabled=active||busy||!config;
  $('dc-connect').disabled=active||busy||!config;
  $('dc-disconnect').disabled=active||busy||!state.connected;
  $('dc-once').disabled=active||busy||!state.connected||!can;
  const initial=['acquire','default'].includes(state.stage)||['starting','queued'].includes(state.phase);
  $('dc-pause').disabled=busy||!owned||state.phase!=='running'||initial;
  $('dc-resume').disabled=busy||!owned||state.phase!=='paused';
  $('dc-continue').disabled=busy||!owned||state.phase!=='holding';
  $('dc-return').disabled=busy||!owned||!active||initial;
  $('dc-release').disabled=busy||!active;
  const current=nodes.indexOf(state.node);
  for(const button of document.querySelectorAll('[data-run-node]')){
    const node=button.dataset.runNode;
    button.disabled=busy||!can||(!active? !state.connected||!['prepared','raised'].includes(node):!owned||state.phase!=='holding'||nodes[current+1]!==node);
    if(node==='released')button.disabled=busy||!active;
  }
  for(const item of document.querySelectorAll('.dc-nodes li')){
    const index=nodes.indexOf(item.dataset.node);
    item.classList.toggle('done',index<=current);
    const stageNode={acquire:'prepared',default:'prepared',raise:'raised',press:'pressed',withdraw:'withdrawn',lower:'lowered'}[state.stage];
    item.classList.toggle('current',active&&(state.phase==='holding'?item.dataset.node===state.node:item.dataset.node===stageNode));
  }
  let text=message;
  const problem=state.last_error||state.error;
  if(!text&&problem)text=errors[problem]||'执行已停止：'+problem;
  if(!text){
    if(state.phase==='holding')text=(labels[state.node]||'节点')+' · 正在保持，可单独执行下一段。';
    else if(state.phase==='paused')text='当前动作已暂停，保持实测姿态；可继续当前段。';
    else if(state.phase==='running')text=stages[state.stage]||'执行中';
    else if(['starting','queued'].includes(state.phase))text='正在核实运控状态并准备动作…';
    else if(state.phase==='finished')text='本次动作已结束，控制权已交还。';
    else if(state.connected)text='只读连接完成；点击执行按钮才会接管并运动。';
    else text='未连接；可先调整目标坐标。';
  }
  $('dc-status').textContent=text;$('dc-status').className=problem?'error':'';
  $('dc-timer').textContent=active?(state.hold_remaining_s!=null?'停留剩余 '+state.hold_remaining_s.toFixed(1)+' s':'策略 '+(state.policy_elapsed_s||0).toFixed(2)+' / 10.00 s'):'';
  $('dc-progress').value=state.policy_elapsed_s||0;
  const t=state.telemetry||{};
  $('dc-fsm').textContent='FSM '+(t.fsm??'—')+' / '+(t.fsm_mode??'—');
  $('dc-weight').textContent='接管权重 '+(typeof t.weight==='number'?t.weight.toFixed(3):'—');
  $('dc-angle').textContent='右肩 pitch '+(t.q? (t.q[22]*180/Math.PI).toFixed(2)+'°':'—');
  $('dc-log').textContent=state.log_dir?'本次日志：'+state.log_dir:'尚无执行日志。连接读取结束后显示的是该次状态快照。';
}
on('dc-connect',()=>post('connect'));
on('dc-disconnect',()=>post('disconnect'));
on('dc-save',()=>post('configure',{target_xyz_m:target()}));
for(const [id,z] of [['dc-preset031',.31],['dc-preset050',.5]])on(id,()=>{targetInputs.forEach((input,i)=>input.value=[.5,-.23,z][i].toFixed(2));note();message='已填入预设；点击执行时使用本次坐标。';render();});
for(const input of targetInputs)input.addEventListener('input',note);
on('dc-once',()=>post('start',{target_xyz_m:target(),stop_at:$('dc-stop-at').value}));
for(const action of ['pause','resume','continue','return','release'])on('dc-'+action,()=>post(action));
for(const button of document.querySelectorAll('[data-run-node]'))button.onclick=()=>{
  const node=button.dataset.runNode;
  Promise.resolve().then(()=>node==='released'?post('release'):state.active?post('next',{node}):post('start',{target_xyz_m:target(),stop_at:node})).catch(error=>{message=error.message;render();});
};
on('dc-download',()=>{
  const result={saved_at:new Date().toISOString(),backend:config?.backend,state,coordinate_frame:'robot_root_relative',units:'m'};
  const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download='g1-dreamcontrol-state.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
async function poll(){
  if(polling||!config)return;polling=true;
  try{
    state=await jsonFetch('/api/dream/state?page_id='+pageId);
    if(state.active&&state.owned_by_this_page&&Date.now()-lastHeartbeat>700){lastHeartbeat=Date.now();await post('heartbeat',{},true);}
    if(state.connected&&message==='正在只读连接，请稍候。')message='';
    render();
  }catch(error){message='页面与控制服务连接中断；请检查现场。';render();}
  finally{polling=false;}
}
try{config=await jsonFetch('/api/dream/config');config.target_xyz_m.forEach((v,i)=>targetInputs[i].value=v.toFixed(2));note();await poll();render();}
catch(error){message='动作面板未连接服务：'+error.message;render();}
setInterval(poll,300);
