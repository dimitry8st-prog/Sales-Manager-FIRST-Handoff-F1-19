#!/usr/bin/env python3
import json,hashlib,fnmatch
from pathlib import Path
root=Path(__file__).resolve().parent
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
manifest=json.loads((root/'PACKAGE_MANIFEST.json').read_text())
for name,sha in manifest['immutable_package_files'].items():
 p=root/name
 if p.is_symlink() or not p.is_file() or digest(p)!=sha:raise SystemExit('IMMUTABLE_INPUT_CHANGED: '+name)
task=json.loads((root/'TASK_IDENTITY.json').read_text())
known=set(manifest['immutable_package_files'])
for p in (root/'source').rglob('*'):
 if p.is_symlink():raise SystemExit('SYMLINK_NOT_ALLOWED: '+str(p))
 if not p.is_file():continue
 name=p.relative_to(root).as_posix()
 if name in known:continue
 if not any(fnmatch.fnmatchcase(name,a) for a in task['allowed_paths']):raise SystemExit('UNAUTHORIZED_SOURCE_PATH: '+name)
print('PACKAGE_INPUTS_PASS; this is package integrity, not implementation acceptance')
