"""Receive Quest and map arm/hand targets on the computer."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

from .config import ROOT, load_config


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
                 "os.execv(interpreter,[interpreter,'-u','-m','quest_teleop.hand.follow']+sys.argv[4:])")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='JSON configuration file')
    parser.add_argument('--only', choices=['arm', 'hand', 'both'])
    parser.add_argument('--side', choices=['left', 'right', 'both'])
    parser.add_argument('--transport', choices=['usb', 'udp'])
    parser.add_argument('--port', type=int)
    parser.add_argument('--scale', type=float, help='Arm translation scale; rotation is 1:1')
    parser.add_argument('--thor-host', help='Thor target bridge IP')
    parser.add_argument('--thor-port', type=int)
    parser.add_argument('--frame-port', type=int, default=8001)
    parser.add_argument('--frame-bind', default='127.0.0.1', help='Local frame listener IP; default localhost')
    parser.add_argument('--preview', action='store_true', help='Calculate poses locally without connecting to devices')
    parser.add_argument('--capture-only', action='store_true', help='Receive Quest data without starting robot followers')
    parser.add_argument('--replay', type=Path, help='Replay a recorded Quest JSONL instead of receiving live input')
    parser.add_argument('--speed', type=float, default=1., help='Replay speed (default 1)')
    parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load_config(args.config)
    for name in ('only', 'side', 'transport', 'port', 'scale', 'thor_host', 'thor_port'):
        if getattr(args, name) is None:
            setattr(args, name, config[name])
    if not math.isfinite(args.scale) or args.scale <= 0:
        parser.error('scale must be positive and finite')
    if not math.isfinite(args.speed) or not .1 <= args.speed <= 4:
        parser.error('speed must be between 0.1 and 4')
    if args.replay:
        from .recording import read_header
        header = read_header(args.replay)
        for name in ('only', 'side', 'scale'):
            setattr(args, name, header['config'][name])
    if args.port is None:
        args.port = 8000 if args.transport == 'usb' else 9000
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    if not 1 <= args.frame_port <= 65535 or args.frame_port == args.port:
        parser.error('frame port must be 1..65535 and differ from the Quest input port')
    if not 1 <= args.thor_port <= 65535:
        parser.error('Thor port must be 1..65535')
    sides = ('left', 'right') if args.side == 'both' else (args.side,)
    arms = args.only in ('arm', 'both')
    if args.only != 'arm' and not args.preview and not args.capture_only:
        for side in sides:
            if not config[side + '_hand_serial']:
                parser.error(side + '_hand_serial must be configured')
    frame_bind = args.frame_bind
    frame_host = '127.0.0.1' if frame_bind == '0.0.0.0' else frame_bind
    output = args.output or ROOT / 'logs' / ('session_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    output.mkdir(parents=True, exist_ok=args.output is not None)
    config.update(only=args.only, side=args.side, transport=args.transport, port=args.port, scale=args.scale,
                  thor_host=args.thor_host, thor_port=args.thor_port)
    effective_config = output / 'config.json'
    effective_config.write_text(json.dumps(config, indent=2), encoding='utf-8')
    stop = output / 'STOP'
    children, logs = {}, {}
    failed = None

    def launch(name, command):
        logs[name] = (output / (name + '.log')).open('w', encoding='utf-8')
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                   QUEST_TELEOP_CONFIG=str(effective_config))
        children[name] = subprocess.Popen(command, cwd=ROOT, env=env, stdout=logs[name], stderr=subprocess.STDOUT,
                                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    print('Session: ' + str(output), flush=True)
    try:
        ports = [(frame_bind, args.frame_port, True)]
        if not args.replay:
            ports.append(('127.0.0.1' if args.transport == 'usb' else '0.0.0.0', args.port, args.transport == 'usb'))
        for host, port, tcp in ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind((host, port))
                except OSError as error:
                    raise RuntimeError(f'Cannot listen on {host}:{port}: {error}. Stop the previous console with Ctrl+C.') from error
        if args.transport == 'usb' and not args.replay:
            adb_reverse(args.port, stop)
        command = [sys.executable, '-u', '-m', 'quest_teleop.quest.receiver',
                   '--transport', 'tcp' if args.transport == 'usb' else 'udp',
                   '--host', '127.0.0.1' if args.transport == 'usb' else '0.0.0.0',
                   '--port', str(args.port), '--frame-host', frame_bind, '--frame-port', str(args.frame_port),
                   '--only', args.only, '--side', args.side,
                   '--output', str(output), '--stop-file', str(stop)]
        if args.replay:
            command = [sys.executable, '-u', '-m', 'quest_teleop.recording', str(args.replay.resolve()),
                       '--speed', str(args.speed), '--frame-host', frame_bind, '--frame-port', str(args.frame_port),
                       '--output', str(output), '--only', args.only, '--side', args.side]
            if args.capture_only:
                command += ['--capture-only']
        launch('receive', command)
        if arms and not args.capture_only:
            command = [sys.executable, '-u', '-m', 'quest_teleop.arm.follow',
                       '--frame-host', frame_host, '--frame-port', str(args.frame_port),
                       '--side', args.side, '--scale', str(args.scale),
                       '--thor-host', args.thor_host, '--thor-port', str(args.thor_port),
                       '--output', str(output), '--stop-file', str(stop)]
            if args.preview:
                command += ['--preview']
            else:
                print(f'Thor target bridge: {args.thor_host}:{args.thor_port}; '
                      f'on Thor run: python3 -u -m quest_teleop.arm.thor --port {args.thor_port}', flush=True)
            launch('arms', command)
        if args.only in ('both', 'hand') and not args.capture_only:
            for side in sides:
                command = hand_command(config, ROOT, frame_host, args.frame_port, output, stop, effective_config, side)
                if not args.preview:
                    command += ['--execute']
                launch('hand_' + side, command)
        print('Preparing replay.' if args.replay else 'Waiting for Quest. Start Streaming; Video Feed OFF. Ctrl+C stops forwarding and local hands.', flush=True)
        positions = {name: 0 for name in children}
        while not stop.exists():
            for name, child in children.items():
                if child.poll() is not None:
                    if args.replay and name == 'receive' and child.returncode == 0:
                        stop.touch()
                        break
                    raise RuntimeError(name + ' exited; see ' + str(output / (name + '.log')))
                with (output / (name + '.log')).open(encoding='utf-8', errors='replace') as log:
                    log.seek(positions[name])
                    lines = log.readlines()
                    positions[name] = log.tell()
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if any(key in row for key in ('warning', 'calibrated', 'preview', 'phase', 'event')):
                        print('[' + name + '] ' + json.dumps(row), flush=True)
            time.sleep(.1)
    except KeyboardInterrupt:
        print('Stopping: Ctrl+C', flush=True)
    except Exception as error:
        failed = str(error)
        print('STOP: ' + failed, flush=True)
    finally:
        stop.touch()
        deadline = time.monotonic() + 15
        while any(p.poll() is None for p in children.values()) and time.monotonic() < deadline:
            time.sleep(.1)
        for name, child in children.items():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
                failed = failed or name + ' did not exit within 15 seconds; inspect log'
            elif child.returncode:
                failed = failed or name + ' exited with code ' + str(child.returncode)
        for log in logs.values():
            log.close()
        if failed:
            for name in children:
                print(name + ':', flush=True)
                print('\n'.join((output / (name + '.log')).read_text(encoding='utf-8', errors='replace').splitlines()[-8:]), flush=True)
        (output / 'summary.json').write_text(json.dumps({'failure': failed}, indent=2), encoding='utf-8')
        print('Stopped. Logs: ' + str(output), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
