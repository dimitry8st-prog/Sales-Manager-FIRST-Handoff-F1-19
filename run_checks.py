#!/usr/bin/env python3
"""Bounded unittest runner. No root discovery, deployment, worker launch or live send."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

MANIFEST_SHA256 = '67e2502055b27f742250e94c132019a6834ee0bad52756f1cf721e81c8f86087'
BASELINE = [('test_first_binding.py',32),('test_language_registry.py',44),
            ('test_first_send_receipt.py',52),('test_first_handoff.py',27),
            ('test_first_worker_health.py',49)]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify(root):
    manifest_path = root/'SOURCE_SNAPSHOT_MANIFEST.json'
    if digest(manifest_path) != MANIFEST_SHA256:
        raise ValueError('IMMUTABLE_MANIFEST_HASH_MISMATCH')
    manifest = json.loads(manifest_path.read_text())
    expected = manifest['safe_manifest']
    if manifest['safe_count'] != 56 or len(expected) != 56:
        raise ValueError('EXPECTED_IMMUTABLE_56_FILE_BASELINE')
    for name, sha in expected.items():
        file = root/name
        if file.is_symlink() or not file.is_file() or digest(file) != sha:
            raise ValueError('BASELINE_HASH_MISMATCH:'+name)
    return expected

def postgres_gate():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ.get('WAVE3_TEST_DSN','')
    schema = os.environ.get('WAVE3_TEST_SCHEMA','')
    if os.environ.get('WAVE3_DISPOSABLE_POSTGRES') != 'YES':
        raise ValueError('DISPOSABLE_POSTGRES_NOT_ACKNOWLEDGED')
    c = conninfo_to_dict(dsn)
    if c.get('host') not in ('127.0.0.1','::1','localhost') or ',' in c.get('host',''):
        raise ValueError('POSTGRES_MUST_BE_EXPLICIT_LOCALHOST')
    if not c.get('port') or not c.get('user') or not c.get('dbname','').startswith('sm_wave3_'):
        raise ValueError('POSTGRES_NEEDS_EXPLICIT_PORT_USER_AND_SM_WAVE3_DATABASE')
    if not schema.startswith('sm_wave3_') or not schema.replace('_','').isalnum():
        raise ValueError('POSTGRES_NEEDS_DEDICATED_SM_WAVE3_SCHEMA')
    with psycopg.connect(dsn,connect_timeout=5,options='-c statement_timeout=5000 -c lock_timeout=3000') as conn:
        version = conn.execute('SHOW server_version_num').fetchone()[0]
        if not 160000 <= int(version) < 170000:
            raise ValueError('POSTGRES16_REQUIRED')
    return c

def child(root, pattern, candidate, pg):
    import socket
    import unittest
    import psycopg
    if sys.version_info[:2] != (3,12) or psycopg.__version__ != '3.2.9':
        raise ValueError('REQUIRE_REAL_PYTHON312_PSYCOPG329_NO_STUB')
    sys.dont_write_bytecode = True
    sys.path.insert(0,str(root))
    os.chdir(root)
    def audit(event,args):
        if event == 'socket.connect':
            addr = args[1]
            if not isinstance(addr,tuple) or not addr:
                raise RuntimeError('EXTERNAL_NETWORK_DENIED')
            try:
                allowed = ipaddress.ip_address(str(addr[0])).is_loopback
            except ValueError:
                allowed = str(addr[0]).lower() == 'localhost'
            if not allowed:
                raise RuntimeError('EXTERNAL_NETWORK_DENIED')
        elif event == 'socket.getaddrinfo':
            if str(args[0]).lower() not in ('localhost','127.0.0.1','::1','none'):
                raise RuntimeError('EXTERNAL_DNS_DENIED')
    sys.addaudithook(audit)
    if pg:
        from psycopg.conninfo import conninfo_to_dict
        permitted = postgres_gate()
        real_connect = psycopg.connect
        def disposable_only(conninfo='', **kwargs):
            supplied = conninfo_to_dict(conninfo)
            supplied.update({k:str(v) for k,v in kwargs.items() if k in ('host','port','dbname','user')})
            if any(supplied.get(k) != permitted.get(k) for k in ('host','port','dbname','user')):
                raise RuntimeError('POSTGRES_TARGET_NOT_EXPLICIT_APPROVED_DISPOSABLE')
            kwargs.setdefault('connect_timeout',5)
            return real_connect(conninfo,**kwargs)
        psycopg.connect = disposable_only
    else:
        def denied(*args,**kwargs):
            raise RuntimeError('REAL_DATABASE_NOT_AUTHORIZED_IN_COMPONENT_SUITE')
        psycopg.connect = denied
    suite = unittest.defaultTestLoader.discover('tests' if candidate == 'f111' or not candidate else 'tests/wave3',pattern=pattern)
    count=suite.countTestCases()
    if count == 0:
        raise ValueError('EMPTY_TEST_SUITE')
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skips':len(result.skipped),'ok':result.wasSuccessful()}
    print('CHECK_RESULT_JSON:'+json.dumps(summary),flush=True)
    expected=dict(BASELINE).get(pattern) if not candidate else None
    if expected is not None and result.testsRun != expected:
        print('EXPECTED_COUNT_MISMATCH',expected,flush=True)
        return 1
    return 0 if result.wasSuccessful() and not result.skipped else 1

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,default=Path(__file__).resolve().parent/'source')
    p.add_argument('--output',type=Path,default=Path('check_logs'))
    p.add_argument('--candidate',choices=[f'f1{i}' for i in (11,12,13,14,15,16,17,19,20)])
    p.add_argument('--requires-postgres',action='store_true')
    p.add_argument('--child',action='store_true',help=argparse.SUPPRESS)
    p.add_argument('--pattern',help=argparse.SUPPRESS)
    a=p.parse_args()
    if a.child:
        return child(a.source.resolve(),a.pattern,a.candidate,a.requires_postgres)
    if sys.version_info[:2] != (3,12):
        raise ValueError('PYTHON312_REQUIRED')
    import psycopg
    if psycopg.__version__ != '3.2.9':
        raise ValueError('PSYCOPG329_REQUIRED')
    if a.requires_postgres and not a.candidate:
        raise ValueError('POSTGRES_FLAG_ONLY_FOR_SEPARATE_CANDIDATE_SUITE')
    root=a.source.resolve();expected=verify(root)
    output=a.output.resolve()
    if output == root or root in output.parents:
        raise ValueError('LOGS_MUST_BE_OUTSIDE_SOURCE')
    output.mkdir(parents=True,exist_ok=True)
    if a.candidate:
        candidate_relative = Path('tests/test_first_thread_correlation.py') if a.candidate == 'f111' else Path('tests/wave3')/('test_'+a.candidate+'.py')
        if not (root/candidate_relative).is_file():
            report = {'candidate':a.candidate,'status':'NOT_IMPLEMENTED','expected_test_path':candidate_relative.as_posix(),'pass':False,'raw_exit_code':3,'baseline_execution':'NOT_RUN_IN_THIS_ATTEMPT'}
            (output/'CHECKS_SUMMARY.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report,indent=2))
            return 3
    safe_env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','WINDIR','TMP','TEMP','TMPDIR','LANG','LC_ALL','PYTHONHOME')}
    safe_env.update({'PYTHONDONTWRITEBYTECODE':'1','NO_OUTBOUND':'1'})
    if a.requires_postgres:
        for k in ('WAVE3_TEST_DSN','WAVE3_TEST_SCHEMA','WAVE3_DISPOSABLE_POSTGRES'):
            if k in os.environ: safe_env[k]=os.environ[k]
    entries=[]
    with tempfile.TemporaryDirectory(prefix='first_wave3_checks_') as temp:
        work=Path(temp)/'source';shutil.copytree(root,work,symlinks=False)
        verify(work)
        suites=[(x,n,False) for x,n in BASELINE]
        if a.candidate:suites.append(('test_first_thread_correlation.py' if a.candidate == 'f111' else 'test_'+a.candidate+'.py',None,True))
        for pattern,count,is_candidate in suites:
            cmd=[sys.executable,'-B',str(Path(__file__).resolve()),'--child','--source',str(work),'--pattern',pattern]
            if is_candidate:
                cmd.extend(['--candidate',a.candidate])
                if a.requires_postgres:cmd.append('--requires-postgres')
            log=output/(pattern+'.log');start=time.monotonic()
            with log.open('w',encoding='utf-8') as handle:
                try:
                    run=subprocess.run(cmd,stdout=handle,stderr=subprocess.STDOUT,env=safe_env,timeout=180)
                    exit_code=run.returncode
                except subprocess.TimeoutExpired:
                    handle.write('\nRUNNER_TIMEOUT_180_SECONDS\n');exit_code=124
            entries.append({'suite':pattern,'kind':'candidate' if is_candidate else 'baseline_component','expected_count':count,'raw_exit_code':exit_code,'elapsed_seconds':round(time.monotonic()-start,3),'log':log.name})
            verify(work)
        verify(root)
    report={'python':sys.version.split()[0],'psycopg':psycopg.__version__,'baseline_files':len(expected),'root_discovery':'NOT_RUN','live_send':'NOT_RUN','postgres_scope':'explicit_disposable_PG16' if a.requires_postgres else 'NOT_RUN','checks':entries,'pass':all(x['raw_exit_code']==0 for x in entries),'limitation':'Process guards support bounded reviewed suites, not a security sandbox for arbitrary hostile code. Component baseline is not full release acceptance.'}
    (output/'CHECKS_SUMMARY.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 0 if report['pass'] else 1

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError,OSError,ImportError) as exc:
        print('CHECKS_PREREQUISITE_OR_INTEGRITY_ERROR:'+str(exc),file=sys.stderr)
        raise SystemExit(2)
