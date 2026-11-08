"""Start the supervisor so it outlives the caller (spec 5.3).

Order: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB. If breakaway is
refused and the caller's Job object kills its processes on close, use a one-shot Task Scheduler
task (no execution time limit). If the Job does not kill on close, start detached inside it.
"""
import json
import os
import pathlib
import subprocess
import sys

from easyci import store

DETACHED_PROCESS, NEW_GROUP, BREAKAWAY = 0x8, 0x200, 0x01000000
ENTRY = pathlib.Path(__file__).resolve().parent / '_launch.py'


def interpreter(windowless=False):
    exe = pathlib.Path(sys.executable)
    if windowless and os.name == 'nt':
        candidate = exe.with_name('pythonw.exe')
        if candidate.exists():
            return str(candidate)
    return str(exe)


def command(rd, windowless=False):
    # -I: ignore PYTHON* variables, user site and cwd; _launch.py adds the package root itself.
    return [interpreter(windowless), '-I', str(ENTRY), str(rd)]


def job_kills_on_close():
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [('a', ctypes.c_int64), ('b', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                    ('c', ctypes.c_size_t), ('d', ctypes.c_size_t), ('e', wintypes.DWORD), ('f', ctypes.c_size_t),
                    ('g', wintypes.DWORD), ('h', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6), ('limits', ctypes.c_size_t * 4)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p]
    info = Extended()
    if not k.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return False
    return bool(info.basic.LimitFlags & 0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def popen(rd, flags):
    rd = pathlib.Path(rd)
    with open(rd / 'supervisor.out', 'ab') as out, open(rd / 'supervisor.err', 'ab') as err:
        kwargs = {'creationflags': flags} if os.name == 'nt' else {'start_new_session': True}
        proc = subprocess.Popen(command(rd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True,
                                cwd=str(rd), **kwargs)
    return proc.pid


def task_name(run_id):
    return f'EasyCI-{run_id}'


def write_launch_env(rd):
    # A Task Scheduler process starts from a fresh logon environment, so EASYCI_* settings
    # (home, tool overrides, intervals; never secrets) are handed over in a file. required_env
    # values are deliberately not written: such jobs fail with a clear 'missing variable' reason.
    env = {k: v for k, v in os.environ.items() if k.startswith('EASYCI_') and k != 'EASYCI_LAUNCH'}
    (pathlib.Path(rd) / 'launch-env.json').write_text(json.dumps(env, indent=2, sort_keys=True), encoding='utf-8')


def schtasks(rd, run_id):
    from xml.sax.saxutils import escape
    write_launch_env(rd)
    cmd = command(rd, windowless=True)
    args = ' '.join(f'"{a}"' for a in cmd[1:])
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>EasyCI supervisor for run {escape(run_id)}</Description></RegistrationInfo>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(cmd[0])}</Command><Arguments>{escape(args)}</Arguments>
    <WorkingDirectory>{escape(str(rd))}</WorkingDirectory></Exec></Actions>
</Task>
'''
    path = pathlib.Path(rd) / 'task.xml'
    path.write_text(xml, encoding='utf-16')
    for args_ in (['/create', '/tn', task_name(run_id), '/xml', str(path), '/f'], ['/run', '/tn', task_name(run_id)]):
        result = subprocess.run(['schtasks', *args_], capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
                                creationflags=0x08000000)
        if result.returncode != 0:
            raise OSError(f'schtasks {args_[0]} failed: {result.stderr.decode(errors="replace").strip()}')


def delete_task(run_id):
    if os.name == 'nt' and run_id:
        subprocess.run(['schtasks', '/delete', '/tn', task_name(run_id), '/f'], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60, creationflags=0x08000000)


def start(rd, run_id):
    """Spawn the supervisor; return (launch_path, pid or None)."""
    forced = os.environ.get('EASYCI_LAUNCH')
    if os.name != 'nt':
        return 'detached', popen(rd, 0)
    if forced != 'schtasks':
        try:
            return 'detached', popen(rd, DETACHED_PROCESS | NEW_GROUP | BREAKAWAY)
        except OSError as error:
            store.log(f'{run_id}: breakaway refused ({error})')
    if forced == 'schtasks' or job_kills_on_close():
        try:
            schtasks(rd, run_id)
            return 'schtasks', None
        except (OSError, subprocess.SubprocessError) as error:
            store.log(f'{run_id}: schtasks launch failed ({error}); starting inside the job')
    return 'detached-in-job', popen(rd, DETACHED_PROCESS | NEW_GROUP)
