"""Verify a source/binary bundle without installing, connecting or executing it."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify(root):
    root=root.resolve()
    manifest=json.loads((root/'MANIFEST.json').read_text(encoding='utf-8'))
    checked=0
    for name,metadata in manifest['files'].items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('Unsafe manifest path')
        path=root.joinpath(*relative.parts)
        if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
            raise ValueError('Missing/unsafe bundle file: '+name)
        data=path.read_bytes()
        if len(data)!=metadata['bytes'] or hashlib.sha256(data).hexdigest()!=metadata['sha256']:
            raise ValueError('Bundle checksum mismatch: '+name)
        checked+=1
    return {'verified_files':checked,'network_applied':False,'processes_started':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.root)))
