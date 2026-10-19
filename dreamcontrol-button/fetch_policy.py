"""Download exactly one pinned upstream policy and verify its LFS digest."""
import hashlib
import json
from pathlib import Path
import urllib.request

root = Path(__file__).resolve().parent
config = json.loads((root/'config/target-z031.json').read_text())
url = ('https://media.githubusercontent.com/media/GenRobo/DreamControl/'
       + config['revision'] + '/Sim2Real/deploy/policies/Button_Press_UB.pt')
destination = root/'assets/Button_Press_UB.pt'
destination.parent.mkdir(exist_ok=True)
if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == config['policy_sha256']:
    print(json.dumps({'cached':True,'sha256':config['policy_sha256'],'bytes':destination.stat().st_size}))
else:
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(config['policy_size_bytes']+1)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != config['policy_size_bytes'] or digest != config['policy_sha256']:
        raise SystemExit('Policy length/digest mismatch; no policy was installed.')
    destination.write_bytes(data)
    print(json.dumps({'source':url,'sha256':digest,'bytes':len(data)}))
