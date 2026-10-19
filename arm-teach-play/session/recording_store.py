"""Named recording library; client supplied paths are never accepted."""
import json
import math
from pathlib import Path
import re
import time
import uuid
import threading

ROOT = Path(__file__).resolve().parents[1] / 'recordings'
TERMINAL = ('ended', 'ended_with_error')


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def recording_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise ValueError('项目名称需为 1～80 个字符')
    if any(ord(c) < 32 for c in value):
        raise ValueError('项目名称不能包含控制字符')
    return value.strip()


class RecordingStore:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sample_cache = {}
        self.cache_lock = threading.RLock()

    def has_continuous(self, path):
        source = path / 'samples.jsonl'
        if not source.is_file():return False
        stat = source.stat();key=(str(source),stat.st_size,stat.st_mtime_ns)
        with self.cache_lock:
            if key in self.sample_cache:return self.sample_cache[key]
            count=0;first=None;found=False
            try:
                with source.open(encoding='utf-8') as stream:
                    for index,line in enumerate(stream):
                        if index>=4096:break
                        row=json.loads(line)
                        if row.get('phase')!='teaching':continue
                        q,t=row.get('q_upper'),row.get('t_s')
                        if not isinstance(q,list) or len(q)!=17 or isinstance(t,bool) or not isinstance(t,(int,float)):break
                        if not math.isfinite(t) or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in q):break
                        if first is None:first=t
                        count+=1
                        if count>=2 and t>first:found=True;break
            except (OSError,ValueError,TypeError,AttributeError):found=False
            # Keep one cache entry per source; active drafts are never scanned.
            self.sample_cache={k:v for k,v in self.sample_cache.items() if k[0]!=str(source)}
            self.sample_cache[key]=found
            return found

    def path(self, recording_id):
        if not isinstance(recording_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', recording_id):
            raise ValueError('invalid_recording_id')
        path = self.root / recording_id
        if path.resolve().parent != self.root or path.is_symlink() or not path.is_dir():
            raise ValueError('recording_not_found')
        return path

    def create(self):
        recording_id = time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        path = self.root / recording_id
        path.mkdir(exist_ok=False)
        return path

    def describe(self, recording_id):
        path = self.path(recording_id)
        meta = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
        if not isinstance(meta,dict):raise ValueError('invalid_recording_metadata')
        points = json.loads((path / 'waypoints.json').read_text(encoding='utf-8'))
        if not isinstance(points, list):
            raise ValueError('invalid_waypoints')
        valid = bool(points) and all(isinstance(p, dict) and isinstance(p.get('q_arm'), list)
            and len(p['q_arm']) == 14 and all(isinstance(q, (int, float)) and not isinstance(q, bool)
            and math.isfinite(q) for q in p['q_arm']) for p in points)
        complete = meta.get('phase') in TERMINAL and meta.get('saved', True)
        return {'id': recording_id, 'name': meta.get('name') or recording_id,
                'points': len(points), 'duration_s': meta.get('recording_duration_s'),
                'created_at': meta.get('created_at', recording_id[:15]),
                'phase': meta.get('phase'), 'saved': bool(complete),
                'playable': bool(valid and complete),
                'continuous_playable': bool(complete and self.has_continuous(path)), 'directory': str(path)}

    def catalog(self):
        result = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                result.append(self.describe(path.name))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda item: item['id'], reverse=True)

    def playback(self, recording_id, mode='staged'):
        if mode not in ('staged','continuous'):raise ValueError('invalid_playback_mode')
        if not self.describe(recording_id)['continuous_playable' if mode=='continuous' else 'playable']:
            raise ValueError('recording_not_saved_or_has_no_' + ('continuous_samples' if mode=='continuous' else 'keypoints'))
        return self.path(recording_id)
