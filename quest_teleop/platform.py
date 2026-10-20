"""Platform launch helpers. Arguments remain argv arrays, never shell strings."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def wsl_path(path):
    path = Path(path).resolve()
    if not path.drive or len(path.drive) != 2:
        raise ValueError('WSL shared files must be on a local Windows drive: ' + str(path))
    return '/mnt/' + path.drive[0].lower() + '/' + path.as_posix()[3:]


def hand_command(config, root, frame_host, frame_port, output, stop, effective_config, side):
    convert = wsl_path if os.name == 'nt' else lambda p: str(Path(p).resolve())
    # Expand ~ inside Linux using Python, without a shell or hardcoded username.
    bootstrap = ("import os,sys; root,config,interpreter=sys.argv[1:4]; "
                 "os.environ['QUEST_TELEOP_CONFIG']=config; "
                 "os.environ['PYTHONPATH']=root; "
                 "os.environ['PYTHONUTF8']='1'; "
                 "interpreter=os.path.expanduser(interpreter); "
                 "os.execv(interpreter,[interpreter,'-u','-m','quest_teleop.hand_follow']+sys.argv[4:])")
    prefix = ['wsl.exe', '-d', config['wsl_distro'], '--exec', 'python3'] if os.name == 'nt' else [sys.executable]
    return prefix + ['-c', bootstrap, convert(root), convert(effective_config), config['hand_python'],
                     '--frame-host', frame_host, '--frame-port', str(frame_port), '--output', convert(output), '--stop-file', convert(stop),
                     '--side', side]


def find_adb():
    adb = shutil.which('adb')
    if not adb and os.name == 'nt':
        candidates = list((Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WinGet/Packages').glob('Google.PlatformTools*/platform-tools/adb.exe'))
        adb = str(candidates[0]) if candidates else None
    if not adb:
        raise RuntimeError('adb missing. Install Android platform-tools and add adb to PATH.')
    return adb


def adb_reverse(port, stop):
    adb = find_adb()
    last = 0.
    while not stop.exists():
        result = subprocess.run([adb, 'devices'], capture_output=True, text=True, timeout=10)
        states = [line.split() for line in result.stdout.splitlines()[1:] if line.strip()]
        ready = [v[0] for v in states if len(v) > 1 and v[1] == 'device']
        if len(ready) > 1:
            raise RuntimeError('Multiple ADB devices: leave only the Quest connected.')
        if ready:
            result = subprocess.run([adb, '-s', ready[0], 'reverse', f'tcp:{port}', f'tcp:{port}'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return
        if time.monotonic() - last > 5:
            print('Waiting for Quest USB debugging authorization. Connect USB and allow debugging in headset; Ctrl+C stops.', flush=True)
            last = time.monotonic()
        time.sleep(.5)
    raise KeyboardInterrupt
