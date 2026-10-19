"""Reproduce the offline official-model audit from locally saved source files."""
import hashlib
import json
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
REF = ROOT / 'reference'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def joint(root, name):
    element = root.find("joint[@name='%s']" % name)
    origin = element.find('origin')
    axis = element.find('axis')
    limit = element.find('limit')
    mimic = element.find('mimic')
    result = {'name': name, 'type': element.attrib['type'],
              'parent': element.find('parent').attrib['link'],
              'child': element.find('child').attrib['link'],
              'xyz': [float(v) for v in origin.attrib.get('xyz', '0 0 0').split()],
              'rpy': [float(v) for v in origin.attrib.get('rpy', '0 0 0').split()]}
    if axis is not None:
        result['axis'] = [float(v) for v in axis.attrib['xyz'].split()]
    if limit is not None:
        result['lower'] = float(limit.attrib['lower'])
        result['upper'] = float(limit.attrib['upper'])
    if mimic is not None:
        result['mimic'] = {'joint': mimic.attrib['joint'],
                           'multiplier': float(mimic.attrib['multiplier']),
                           'offset': float(mimic.attrib['offset'])}
    return result


def mesh_bounds(path):
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0]
    if len(data) != 84 + count * 50:
        raise ValueError('Unsupported STL encoding')
    vertices = set()
    for triangle in range(count):
        for vertex in range(3):
            vertices.add(struct.unpack_from('<3f', data, 84 + triangle * 50 + 12 + vertex * 12))
    low = [min(p[i] for p in vertices) for i in range(3)]
    high = [max(p[i] for p in vertices) for i in range(3)]
    # Deterministic geometric endpoint, expressly NOT a contact calibration.
    end = sorted(p for p in vertices if p[1] >= high[1] - .0002)
    tip = [sum(p[i] for p in end) / len(end) for i in range(3)]
    return {'min_m': low, 'max_m': high, 'tip_proxy_m': tip,
            'triangles': count, 'sha256': sha(path)}


def main():
    manifest = json.loads((REF / 'manifest.json').read_text())
    for file in manifest['files']:
        if sha(REF / file['name']) != file['sha256']:
            raise ValueError('Source hash mismatch: ' + file['name'])
    body = ET.parse(REF / 'g1_29dof_rev_1_0.urdf').getroot()
    ftp_path = REF / 'g1_29dof_rev_1_0_with_inspire_hand_FTP.urdf'
    ftp = ET.parse(ftp_path).getroot()
    arm_names = ['right_shoulder_pitch_joint', 'right_shoulder_roll_joint',
                 'right_shoulder_yaw_joint', 'right_elbow_joint',
                 'right_wrist_roll_joint', 'right_wrist_pitch_joint', 'right_wrist_yaw_joint']
    matching = all(joint(body, n) == joint(ftp, n) for n in arm_names)
    hand_names = ['right_base_joint', 'right_index_1_joint', 'right_index_2_joint']
    hand_joints = [joint(ftp, n) for n in hand_names]
    meshes = {name: mesh_bounds(REF / (name + '.STL'))
              for name in ['right_base_link', 'right_index_1', 'right_index_2']}
    hand = {'name': 'Inspire RH56DFTP (Unitree official FTP integration)',
            'physical_model_confirmed_by_user': True,
            'official_repository_commit': manifest['commit'],
            'official_urdf_sha256': sha(ftp_path),
            'joints': hand_joints,
            'tip': meshes['right_index_2']['tip_proxy_m'],
            'tip_definition': 'Unique-vertex centroid within 0.2 mm of maximum local Y in official right_index_2.STL; geometric proxy, not calibrated contact TCP',
            'tip_calibrated': False, 'meshes': meshes}
    (ROOT / 'web' / 'hand-geometry.mjs').write_text(
        'export const hand = ' + json.dumps(hand, ensure_ascii=False, indent=2) + ';\n', encoding='utf-8')
    local = ROOT.parent / '_repo_inspection/g1-robot-dd6ea93-hand/model/g1_29dof_rev_1_0.urdf'
    audit = {'date': '2026-10-13', 'manifest': manifest,
             'local_body_byte_identical_to_official': sha(local) == sha(REF / 'g1_29dof_rev_1_0.urdf'),
             'body_and_ftp_right_arm_joint_definitions_identical': matching,
             'hand': hand,
             'joint_origin_distances_cm': {
                 n: 100 * math.sqrt(sum(v*v for v in joint(ftp, n)['xyz']))
                 for n in hand_names[1:] + ['right_wrist_roll_joint']},
             'field_measurements': {'shoulder_top_height_cm': 110,
                                    'arm_and_hand_segments_cm': [20, 13, 6, 16],
                                    'finger_segments_cm': [5, 5],
                                    'palm_16cm_excludes_finger_segments': True,
                                    'segment_values_applied_to_geometry': False,
                                    'reason': 'Field endpoints are not yet matched to official joint frames; use official model geometry without rescaling.'}}
    (ROOT.parent / 'artifacts/g1-official-geometry-audit-20261013.json').write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'body_exact_match': audit['local_body_byte_identical_to_official'],
                      'arm_joint_match': matching, 'tip_m': hand['tip'],
                      'index_joint_spacing_cm': audit['joint_origin_distances_cm']['right_index_2_joint']}))


if __name__ == '__main__':
    main()
