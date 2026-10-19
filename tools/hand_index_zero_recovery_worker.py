"""One authorized right-index close; no retries, no feedback-as-target."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

JOB = Path(__file__).resolve().parent
OLD = Path('/tmp/g1-hand-web-right-20261012-h4dsbi40')
sys.path.insert(0, str(OLD/'hand-teleop'))
import step_server as step


def save(result):
    temporary = JOB/'result.pending'
    temporary.write_text(json.dumps(result, indent=2))
    os.replace(str(temporary), str(JOB/'result.json'))


def status(token, side):
    request = urllib.request.Request('http://192.0.2.5:8769/api/status/'+side,
                                     headers={'X-Hand-Token': token})
    with urllib.request.urlopen(request, timeout=4) as response:
        return json.load(response)


def launch(work, token_file, log_path):
    with log_path.open('xb') as log:
        os.chmod(str(log_path), 0o600)
        process = subprocess.Popen([sys.executable,'-u','step_server.py',
            '--bind','192.0.2.5','--port','8769',
            '--plan','manual-range1000-right.json','--plan','manual-range1000-left.json',
            '--builtin-connection','--token-file',str(token_file)],cwd=str(work),
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(40):
        assert process.poll() is None, 'Service launch failed; private log retained'
        try:
            with urllib.request.urlopen('http://192.0.2.5:8769/api/config',timeout=1) as response:
                config = json.load(response)
            return process, config
        except OSError:
            time.sleep(.1)
    raise RuntimeError('Service did not become ready')


def main():
    result = dict(marker='HAND_INDEX_ZERO_RECOVERY', phase='preflight',
                  authorized_side='R', authorized_target=[0,0,0,0,0,1000],
                  motion_attempts=0, automatic_retries=0)
    save(result)
    stopped = False
    process_lock = None
    try:
        metadata = json.loads((OLD/'runtime.json').read_text())
        pid = metadata['pid']
        assert pid == 836589
        assert Path(os.readlink('/proc/%s/cwd'%pid)) == OLD/'hand-teleop'
        command = Path('/proc/%s/cmdline'%pid).read_bytes().split(b'\0')
        assert b'step_server.py' in command and b'manual-range1000-right.json' in command
        token = (OLD/'connection.token').read_text().strip()
        driver = step.RepositoryDriver(OLD/'hand-teleop/vendor/repository/inspire_hand_ctl.py')
        for side in ('L','R'):
            state = status(token,side)
            assert not state['armed'] and not state['busy'], 'Existing session must be idle'
        targets = {side:driver.read_targets(side) for side in ('L','R')}
        before = {side:driver.read(side) for side in ('L','R')}
        assert targets == {'L':[0,0,0,0,0,1000],'R':[0,0,0,0,0,1000]}, 'Targets changed again'
        assert abs(before['R']['actual'][3]-501)<8, 'Index changed since authorization'
        assert all(abs(a-b)<8 for i,(a,b) in enumerate(zip(before['R']['actual'],[1,0,0,501,1,987])) if i!=3)
        result.update(before_targets=targets,before=before)
        work = JOB/'hand-teleop'
        shutil.copytree(OLD/'hand-teleop',work,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        credential = JOB/'connection.token'
        credential.write_text(token);os.chmod(str(credential),0o600)
        for side in ('L','R'):
            current = status(token,side)
            assert not current['armed'] and not current['busy']
        os.kill(pid,signal.SIGTERM);stopped=True
        for _ in range(30):
            try:
                if Path('/proc/%s/stat'%pid).read_text().split()[2]=='Z':break
            except FileNotFoundError:break
            time.sleep(.1)
        else:raise RuntimeError('Own service did not exit; no actuator command sent')
        process_lock = step.claim_live_process()
        assert {side:driver.read_targets(side) for side in ('L','R')} == targets
        # Exclusive create prevents this particular job from ever repeating the write.
        with (JOB/'motion-attempt.json').open('x') as output:
            json.dump({'side':'R','values':[0,0,0,0,0,1000],'speed':300},output)
        result.update(phase='one-command-in-progress',motion_attempts=1);save(result)
        actual = driver.move('R',[0,0,0,0,0,1000])
        after_targets = {side:driver.read_targets(side) for side in ('L','R')}
        after = {side:driver.read(side) for side in ('L','R')}
        assert after_targets == targets, 'Device targets changed during execution; no retry'
        result.update(returned_actual=actual,after=after,after_targets=after_targets,
                      index_reached=abs(after['R']['actual'][3])<8,
                      all_six_reached=max(abs(a-b) for a,b in zip(actual,targets['R']))<8)
        assert result['index_reached'], 'Right index did not reach0; original guard retained'
        for side,label in [('R','right'),('L','left')]:
            path = work/('manual-range1000-'+label+'.json')
            plan = step.load_plan(path)
            plan.update(hold_targets=after_targets[side],reference_actual=after[side]['actual'],
                        evidence=plan['evidence']+' 2026-10-13 explicit user authorization: right index to0 once; other channels retain device command targets. Reference refreshed after readback; no feedback compensation.')
            path.write_text(json.dumps(plan,indent=2));step.load_plan(path)
        process_lock.close();process_lock=None
        process, config = launch(work,credential,JOB/'step-server.log')
        assert config['single_step_scopes']['R']['hold_targets']==targets['R']
        assert config['single_step_scopes']['L']['hold_targets']==targets['L']
        result.update(phase='complete',pid=process.pid,directory=str(JOB),
                      prior_pid=pid,prior_directory=str(OLD),url='http://192.0.2.5:8769',
                      armed=False,driver_sha256=config['driver_sha256'])
        (JOB/'runtime.json').write_text(json.dumps(result,indent=2))
        os.chmod(str(JOB/'runtime.json'),0o600)
    except Exception as error:
        result.update(phase='failed-no-retry',error=str(error))
        if process_lock is not None:
            process_lock.close();process_lock=None
        if stopped:
            try:
                process,_ = launch(OLD/'hand-teleop',OLD/'connection.token',JOB/'guarded-fallback.log')
                result.update(fallback_pid=process.pid,fallback_directory=str(OLD),
                              fallback_preserves_original_guard=True)
                metadata['pid']=process.pid
                (OLD/'runtime.json').write_text(json.dumps(metadata,indent=2))
            except Exception as fallback_error:
                result['fallback_error']=str(fallback_error)
    finally:
        if process_lock is not None:process_lock.close()
        save(result)


if __name__=='__main__': main()
