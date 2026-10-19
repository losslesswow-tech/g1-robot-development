"""Navigation task adapter: existing authenticated hand session, one explicit pose."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request
import uuid

ACTION = 'right_index_extend'
TARGET = {'handAction': ACTION}
ACTIONS = {'right_index_extend': 1000, 'right_index_retract': 0}
STEP_ACTIONS = {'U':'right_index_extend', 'V':'right_index_retract'}


def validate_hand_steps(task):
    defaults = task.get('defaults', {})
    steps = list(task.get('subTasks', []))
    for name in ('beforeSteps', 'afterSteps'):
        steps += defaults.get(name, [])
    if any(s.get('action') in STEP_ACTIONS for s in defaults.get('errorSteps', [])):
        raise ValueError('手指动作不能作为自动错误恢复动作')
    for step in steps:
        action = step.get('action')
        if action in STEP_ACTIONS and step.get('target') != {'handAction':STEP_ACTIONS[action]}:
            raise ValueError('手部步骤与动作不符：右手伸食指1000、收食指0，其余保持设备目标')


def post(path, payload):
    root = Path('/opt/g1-development')
    profile = json.loads((root/'hand-teleop/environments/backpack-20261013.json').read_text())
    token = (root/'connection.token').read_text().strip()
    url = 'http://'+profile['management_host']+':'+str(profile['control_port'])+path
    request = urllib.request.Request(url,data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','X-Hand-Token':token})
    # No retries: a timeout after dispatch is an uncertain physical result.
    try:
        with urllib.request.urlopen(request,timeout=28 if path in ('/api/pose','/api/task/pose') else 5) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Preserve the service's guard explanation; never retry a rejected pose.
        detail = ''
        try:
            body = json.loads(error.read(4096))
            if isinstance(body, dict) and isinstance(body.get('error'), str):
                detail = body['error'][:800]
        except (ValueError, UnicodeError):
            pass
        raise RuntimeError('手部服务 HTTP %s%s' %
                           (error.code, '：'+detail if detail else '')) from error


class TaskHand:
    def __init__(self, transport=None):
        self.transport = transport or post
        self.lock = threading.RLock()
        self.run_id = uuid.uuid4().hex
        self.jobs = {}
        self.cancelled = True
        self.paused = False

    def start(self):
        with self.lock:
            if any(j['thread'].is_alive() for j in self.jobs.values()):
                raise RuntimeError('上一手部动作仍在处理，不接受新任务')
            self.run_id = uuid.uuid4().hex
            self.jobs = {}
            self.cancelled = self.paused = False

    def cancel(self):
        with self.lock:
            self.cancelled = True
            for job in self.jobs.values():
                job['cancel'].set()

    def pause(self):
        with self.lock:
            self.paused = True
            for job in self.jobs.values():
                job['cancel'].set()

    def resume(self):
        with self.lock:
            self.paused = False

    def step(self, loop, index, target):
        if not isinstance(target,dict) or target not in [{'handAction':a} for a in ACTIONS]:
            raise ValueError('无效的手指动作')
        with self.lock:
            if self.cancelled or self.paused:
                return {'state':'failed','error':'手部步骤已暂停或取消；不自动重试'}
            key = (loop,index)
            if key not in self.jobs:
                job = {'state':'running','action':target['handAction'],
                       'cancel':threading.Event(),'keepalive_stop':threading.Event()}
                job['thread'] = threading.Thread(target=self._execute,args=(job,),daemon=True)
                self.jobs[key] = job
                job['thread'].start()
            job = self.jobs[key]
            if job['action'] != target['handAction']:
                raise ValueError('同一步骤不能在提交后更换动作')
            return {k:job[k] for k in ('state','error','result') if k in job}

    def _execute(self, job):
        client = 'nav-hand-'+uuid.uuid4().hex
        session = None
        heartbeat = None
        outcome = {'state':'failed','error':'手部步骤未完成'}
        try:
            if job['cancel'].is_set():raise RuntimeError('发送前已取消')
            prepared = self.transport('/api/task/arm',{'action':job['action'],'client':client})
            session = {'client':client,'lease':prepared['lease']}
            values = prepared['target'][:]
            if len(values)!=6 or any(type(v) is not int or not 0<=v<=1000 for v in values):
                raise ValueError('设备目标无效')
            values[3] = ACTIONS[job['action']]
            if job['cancel'].is_set():raise RuntimeError('发送前已取消')
            if values == prepared['target'] and abs(prepared['actual'][3]-values[3]) < 8:
                actual = prepared['actual']
                errors = [abs(a-b) for a,b in zip(actual,values)]
                result = {'target':values,'actual':actual,'errors':errors,
                          'reached':True,'repository_reached':max(errors)<8,'motion_sent':False,
                          'completion':'right_index_below8_and_targets_unchanged'}
            else:
                def keepalive():
                    while not job['keepalive_stop'].wait(2):
                        if job['cancel'].is_set():return
                        try:self.transport('/api/heartbeat',session)
                        except Exception:
                            job['cancel'].set();return
                heartbeat = threading.Thread(target=keepalive,daemon=True);heartbeat.start()
                result = self.transport('/api/task/pose',dict(session,side='R',seq=1,values=values))
                if result.get('target')!=values:raise RuntimeError('返回目标与发送值不符')
            with self.lock:
                job['result'] = result
                if job['cancel'].is_set():raise RuntimeError('步骤已取消，已发送的目标可能继续执行')
                if result.get('reached') is not True:
                    raise RuntimeError('食指未到位或设备目标被改变，已停止任务；不自动重试，偏差='+str(result.get('errors')))
                print('Hand task done: '+json.dumps({'action':job['action'],
                    'result':result},ensure_ascii=True),flush=True)
                outcome = {'state':'done'}
        except Exception as error:
            with self.lock:
                outcome = {'state':'failed','error':str(error)}
            print('Hand task failed: '+json.dumps(
                {'action':job['action'],'error':str(error)},ensure_ascii=True),flush=True)
        finally:
            job['keepalive_stop'].set()
            if heartbeat:heartbeat.join(timeout=6)
            if session:
                try:self.transport('/api/disarm',session)
                except Exception:pass  # Lease expires; never issue a retry or motor-stop fiction.
            with self.lock:
                job.update(outcome)
