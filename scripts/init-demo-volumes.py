"""Repair only existing EasyCI demo data-volume roots; no credential access."""
import argparse
import json
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--docker', default='docker')
parser.add_argument('--host')
args = parser.parse_args()
docker = [args.docker] + (['--host', args.host] if args.host else [])

def run(arguments):
    return subprocess.run(docker + arguments, text=True, capture_output=True,
                          check=True, timeout=30).stdout.strip()

volumes = ['jenkins-local-demo-cache', 'jenkins-local-demo-releases']
for name in volumes:
    info = json.loads(run(['volume', 'inspect', name]))[0]
    assert info['Name'] == name and info['Labels']['com.docker.compose.project'] == 'jenkins-local'

# UID 0 owns the fresh volume roots. GID 1000 is the agent group. An owner can
# assign its own files to its process group and chmod them with ALL caps dropped;
# no CHOWN capability, privileged mode, network, host path or secret is needed.
code = """import json,os,pathlib
assert os.getuid()==0 and os.getgid()==1000
record={}
for name in ['/demo-cache','/demo-releases']:
    p=pathlib.Path(name)
    s=p.stat()
    if s.st_uid==1000:
        record[name]={'alreadyAgentOwned':True}
        continue
    assert s.st_uid==0 and s.st_gid in (0,1000), 'Unexpected ownership; stop for review'
    os.chown(p,-1,1000)
    os.chmod(p,0o770)
    updated=p.stat()
    record[name]={'uid':updated.st_uid,'gid':updated.st_gid,'mode':oct(updated.st_mode & 0o777)}
print(json.dumps(record))
"""
arguments = ['run', '--rm', '--network', 'none', '--user', '0:1000',
             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
             '--cpus', '0.5', '--memory', '128m', '--pids-limit', '32']
for name, target in zip(volumes, ['/demo-cache', '/demo-releases']):
    arguments += ['--mount', f'type=volume,source={name},target={target}']
arguments += ['--entrypoint', 'python3', 'local/jenkins-demo-agent:scaffold-1', '-c', code]
print(run(arguments))
