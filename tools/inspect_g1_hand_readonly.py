"""Bounded read-only hand probe via the existing navigation management API.

No remote files are installed. Only FC03 at the two repository-documented
telemetry addresses is sent. No arbitrary register or firmware probing.
"""
import json
from pathlib import Path
import shlex
import urllib.request


SOURCE = r'''
import concurrent.futures,json,os,pathlib,platform,socket,struct,subprocess,sys,time

def command(args):
 try:
  p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=3)
  return {'exit_code':p.returncode,'stdout':p.stdout[:6000],'stderr':p.stderr[:1000]}
 except Exception as e:return {'error':str(e)}

def exact(s,n,deadline):
 result=bytearray()
 while len(result)<n:
  remaining=deadline-time.monotonic()
  if remaining<=0:raise TimeoutError('frame deadline exceeded')
  s.settimeout(remaining)
  part=s.recv(n-len(result))
  if not part:raise EOFError('connection closed before complete response')
  result.extend(part)
 return bytes(result)

def read(ip,addr,tid):
 result={'ip':ip,'port':6000,'function':3,'address':addr,'count':6,'request_tid':tid}
 start=time.monotonic();deadline=start+3
 pdu=struct.pack('>BHH',3,addr,6)
 frame=struct.pack('>HHHB',tid,0,len(pdu)+1,1)+pdu
 try:
  with socket.create_connection((ip,6000),timeout=3) as s:
   result['tcp_connected']=True
   remaining=deadline-time.monotonic()
   if remaining<=0:raise TimeoutError('connection exhausted deadline')
   s.settimeout(remaining);s.sendall(frame)
   header=exact(s,7,deadline)
   rt,proto,length,unit=struct.unpack('>HHHB',header)
   result['mbap']={'transaction_id':rt,'protocol':proto,'length':length,'unit_id':unit}
   result['transaction_delta']=(rt-tid+32768)%65536-32768
   if proto!=0 or unit!=1 or not 2<=length<=254:raise ValueError('invalid MBAP')
   reply=exact(s,length-1,deadline)
   result['pdu_hex']=reply.hex()
   if reply[0]==0x83:raise ValueError('Modbus exception '+reply.hex())
   if len(reply)!=14 or reply[0]!=3 or reply[1]!=12:raise ValueError('invalid telemetry PDU')
   result['values']=list(struct.unpack('>6H',reply[2:]))
   result['payload_valid']=True
   result['strict_tid_match']=(rt==tid)
 except Exception as e:result['error']=str(e)
 result['elapsed_seconds']=round(time.monotonic()-start,3)
 return result

def inspect(side,ip):
 out={'route':command(['ip','-j','route','get',ip])}
 out['angle']=read(ip,1546,1)
 if out['angle'].get('payload_valid'):out['force_raw']=read(ip,1582,2)
 else:out['force_raw']={'skipped':'angle read failed'}
 return side,out

out={'scope':'management API execution environment; may be navigation container',
     'python':sys.version,'architecture':platform.machine(),'hostname':socket.gethostname(),
     'kernel':platform.release(),'os_release':pathlib.Path('/etc/os-release').read_text(),
     'motion_commands_sent':0,'register_write_commands_sent':0,'remote_files_installed':False,
     'firmware':'not queried; no verified query register in repository',
     'pid1_name':pathlib.Path('/proc/1/comm').read_text().strip(),
     'interfaces':command(['ip','-j','address','show']),
     'hands':{}}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 jobs=[pool.submit(inspect,side,ip) for side,ip in [('L','192.0.2.3'),('R','192.0.2.4')]]
 for job in jobs:
  side,result=job.result();out['hands'][side]=result
out['marker']='G1_HAND_READONLY_COMPLETE'
print(json.dumps(out))
'''


def main():
    request = urllib.request.Request(
        "http://192.0.2.5:9000/api/system/execute_cmd",
        data=json.dumps({"command": "python3 -c " + shlex.quote(SOURCE)}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        envelope = json.load(response)
    output = envelope.get("output", "")
    if not isinstance(output, str):
        raise RuntimeError("management API returned an unexpected output type")
    result = json.loads(output)
    if result.get("marker") != "G1_HAND_READONLY_COMPLETE":
        raise RuntimeError("remote probe did not complete")
    target = Path(__file__).resolve().parents[1] / "artifacts" / "g1-hand-readonly-20261012.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
