"""Package reviewed offline source, verified policy, and referenced model assets."""
import hashlib
import json
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET

root=Path(__file__).resolve().parent
workspace=root.parent
artifact=workspace/'artifacts/dreamcontrol-button-20261015'
model_root=root/'vendor/DreamControl/Sim2Real/resources/robots/g1_description'
model_file=model_root/'g1_27dof_with_hand_rev_1_0_fixed.xml'
files=[root/'offline_run.py',root/'config/target-z031.json',root/'assets/Button_Press_UB.pt',
       root/'requirements-offline.txt',root/'vendor/DreamControl/LICENSE',
       model_file,model_root/'scene_fixed_base_button_press.xml',
       root/'vendor/DreamControl/Sim2Real/deploy/deploy_mujoco/configs/g1.yaml']
for name in ['deploy_real_Button_Press_UB.py','deploy_real_base_UB.py','common/command_helper.py']:
    files.append(root/'vendor/DreamControl/Sim2Real/deploy/deploy_real'/name)
files.append(root/'vendor/DreamControl/Sim2Real/deploy/deploy_mujoco/deploy_mujoco_Button_Press_UB.py')
for mesh in ET.parse(model_file).findall('.//mesh[@file]'):
    files.append(model_root/'meshes'/mesh.attrib['file'])
files=sorted(set(files))
manifest={'revision':json.loads((root/'config/target-z031.json').read_text())['revision'],
          'scope':'offline simulation only',
          'files':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
manifest_path=artifact/'bundle-manifest.json'
manifest_path.write_text(json.dumps(manifest,indent=2))
archive=artifact/'dreamcontrol-z031-offline.tar.gz'
with tarfile.open(archive,'w:gz') as stream:
    for p in files:
        stream.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
    stream.add(manifest_path,arcname='bundle-manifest.json',recursive=False)
print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,
                  'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':len(files)}))
