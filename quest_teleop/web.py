"""Local recording / teleoperation panel: python -m quest_teleop.web."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .config import ROOT, load_config
from .ipc import FrameSubscriber
from .recording import Recorder, read_header
from .quest.gestures import VictoryGesture


class Controller:
    def __init__(self, frame_port=8001, config_path=None):
        self.config = load_config(config_path)
        self.config_path = config_path
        self.frame_port = frame_port
        self.lock = threading.RLock()
        self.recorder = Recorder(ROOT / 'recordings')
        self.gesture_enabled = False
        self.waiting_for_gesture = False
        self.gestures = {}
        self.process = self.output = self.console = None
        self.mode = 'idle'
        self.options = dict(self.config)
        self.stopping = False
        self.latest = None
        self.error = None
        self.log_position = 0
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._monitor, daemon=True)
        self.worker.start()

    def _alive(self):
        return self.process is not None and self.process.poll() is None

    def recordings(self):
        result = []
        if not self.recorder.directory.exists():
            return result
        for path in sorted(self.recorder.directory.glob('*.jsonl'), reverse=True):
            try:
                header = read_header(path)
                # Our footer fits in one block; never rescan the full capture.
                with path.open('rb') as source:
                    source.seek(max(0, path.stat().st_size - 8192))
                    footer = json.loads(source.read().splitlines()[-1])['summary']
                result.append({**header, **footer, 'id': path.name, 'bytes': path.stat().st_size})
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue
        return result

    def _recording_path(self, name):
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name or ':' in name:
            raise ValueError('Invalid recording ID')
        path = self.recorder.directory / name
        if path.suffix != '.jsonl' or not path.is_file():
            raise ValueError('Recording not found')
        return path

    def start(self, body, replay=False):
        with self.lock:
            if self._alive():
                raise ValueError('请先停止当前会话')
            self.recorder.stop()
            options = dict(self.config)
            for key, choices in (('only', ('arm', 'hand', 'both')), ('side', ('left', 'right', 'both')),
                                 ('transport', ('usb', 'udp'))):
                options[key] = body.get(key, options[key])
                if options[key] not in choices:
                    raise ValueError('Invalid ' + key)
            # Reject obsolete clients instead of turning their non-motion request
            # into robot execution after the mode selector has been removed.
            if body.get('execution', 'execute') != 'execute':
                raise ValueError('仅支持执行到机器人，请刷新页面后重试')
            options['scale'] = float(body.get('scale', options['scale']))
            if not math.isfinite(options['scale']) or options['scale'] <= 0:
                raise ValueError('比例必须是大于零的有限数值')
            port = body.get('port') or (8000 if options['transport'] == 'usb' else 9000)
            if type(port) is not int or not 1 <= port <= 65535 or port == self.frame_port:
                raise ValueError('串流端口无效或与帧端口冲突')
            options['port'] = port
            path = None
            speed = float(body.get('speed', 1))
            if not math.isfinite(speed) or not .1 <= speed <= 4:
                raise ValueError('回放速度应为 0.1–4 倍')
            if replay:
                path = self._recording_path(body.get('id'))
                options.update(read_header(path)['config'])
            output = ROOT / 'logs' / ('session_' + time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6])
            output.mkdir(parents=True)
            command = [sys.executable, '-u', '-m', 'quest_teleop', '--output', str(output),
                       '--frame-port', str(self.frame_port), '--only', options['only'], '--side', options['side'],
                       '--transport', options['transport'], '--port', str(port), '--scale', str(options['scale'])]
            if self.config_path:
                command += ['--config', str(self.config_path.resolve())]
            if replay:
                command += ['--replay', str(path), '--speed', str(speed)]
            elif self.gesture_enabled:
                command += ['--wait-for-start']
            if self.console:
                self.console.close()
            self.console = (output / 'console.log').open('w', encoding='utf-8')
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.console, stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.output, self.options = output, options
            self.mode = 'replay' if replay else 'live'
            self.waiting_for_gesture = self.gesture_enabled and not replay
            self.stopping, self.error, self.latest = False, None, None
            self.gestures = {}
            self.log_position = 0

    def stop(self):
        with self.lock:
            self.recorder.stop()
            if self._alive():
                self.stopping = True
                (self.output / 'STOP').touch()

    def set_gesture(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        with self.lock:
            if self._alive():
                raise ValueError('请先停止当前会话，再修改手势启动设置')
            self.gesture_enabled = enabled
            self.gestures = {}

    def stop_recording(self):
        with self.lock:
            self.recorder.stop()

    def _gesture_start(self, frame):
        if (not self.waiting_for_gesture or not self._alive()
                or self.mode != 'live' or self.stopping):
            return
        sides = ('Left', 'Right') if self.options['side'] == 'both' else (self.options['side'].title(),)
        keys = {side + '/landmarks' for side in sides}
        triggered = False
        for packet in frame['packets']:
            if packet.get('key') in keys:
                if packet['key'] not in self.gestures:
                    self.gestures[packet['key']] = VictoryGesture()
                gesture = self.gestures[packet['key']]
                triggered = gesture.update(packet) or triggered
        if triggered:
            try:
                self._require_live_input()
            except ValueError:
                self.gestures.clear()
                return  # Restart the hold when the selected streams become ready.
            if not (self.output / 'STOP').exists():
                (self.output / 'START_TELEOP').touch()
                self.waiting_for_gesture = False

    def _require_live_input(self):
        if self.latest is None:
            raise ValueError('尚未收到 Quest 数据，请先开启串流')
        required = []
        sides = ('Left', 'Right') if self.options['side'] == 'both' else (self.options['side'].title(),)
        for side in sides:
            required.append(side + '/landmarks')
            if self.options['only'] != 'hand':
                required.append(side + '/wrist')
        if self.options['only'] != 'hand':
            required.append('Head/pose')
        packets = {p['key']: p for p in self.latest['packets']}
        if any(k not in packets or not -.1 <= (time.time_ns() - packets[k]['recv_unix_ns']) / 1e9 <= .2 for k in required):
            raise ValueError('所选侧数据未就绪：请保持手在视野内，开启 Head Pose / Debug Info')

    def record(self):
        with self.lock:
            if not self._alive() or self.mode != 'live' or self.stopping:
                raise ValueError('请先启动实时会话')
            if self.waiting_for_gesture:
                raise ValueError('请先用 ✌ 手势开始遥操，再开始采集')
            self._require_live_input()
            self.recorder.start(self.options)

    def _read_state(self, name):
        if not self.output:
            return None
        try:
            state = json.loads((self.output / (name + '.status.json')).read_text(encoding='utf-8'))
            state['fresh'] = self._alive() and time.time_ns() - state['updated_ns'] < 2_000_000_000
            return state
        except (OSError, ValueError):
            return None

    def status(self):
        with self.lock:
            active = self._alive()
            packets = self.latest['packets'] if self.latest else []
            ages = {p['key']: max(0, (time.time_ns() - p['recv_unix_ns']) / 1e9) for p in packets}
            return {'active': active, 'stopping': self.stopping and active, 'mode': self.mode,
                    'options': self.options, 'error': self.error,
                    'quest': {'live': active and self.mode == 'live' and any(a < .2 for a in ages.values()), 'ages': ages},
                    'arms': self._read_state('arms'),
                    'hands': {s: self._read_state('hand_' + s) for s in ('left', 'right')},
                    'replay': self._read_state('replay'), 'recording': self.recorder.status(),
                    'gesture_enabled': self.gesture_enabled,
                    'waiting_for_gesture': active and self.waiting_for_gesture}

    def _logs(self):
        with (self.output / 'console.log').open(encoding='utf-8', errors='replace') as source:
            source.seek(self.log_position)
            # Keep partial writes for the next poll; detailed logs stay on disk.
            while True:
                position = source.tell()
                line = source.readline()
                if not line or not line.endswith('\n'):
                    self.log_position = position
                    break
                if line.startswith('STOP:'):
                    self.error = line.strip()

    def _monitor(self):
        subscriber = FrameSubscriber('127.0.0.1', self.frame_port)
        observed = None
        last_logs = 0.
        try:
            while not self.closed.is_set():
                with self.lock:
                    active, output = self._alive(), self.output
                    if output != observed:
                        subscriber.close()
                        observed = output
                    try:
                        if output and time.monotonic() - last_logs >= .3:
                            self._logs()
                            last_logs = time.monotonic()
                        if self.process and not active:
                            self.recorder.stop()
                            if self.process.returncode and not self.error:
                                self.error = f'会话退出（{self.process.returncode}），请查看 {output / "console.log"}'
                    except (OSError, ValueError) as error:
                        self.error = str(error)
                        self.recorder.stop()
                if not active:
                    subscriber.close()
                    self.closed.wait(.05)
                    continue
                try:
                    frame = subscriber.next_latest(timeout=.04)
                    if frame is not None:
                        with self.lock:
                            if output == self.output:
                                self.latest = frame
                                self._gesture_start(frame)
                                self.recorder.add(frame)
                except (OSError, ValueError, KeyError) as error:
                    subscriber.close()
                    with self.lock:
                        if self.recorder.file:
                            self.error = str(error)
                            self.recorder.stop()
                    self.closed.wait(.03)
                self.closed.wait(.002)
        finally:
            subscriber.close()

    def close(self):
        self.stop()
        with self.lock:
            process = self.process
        if process:
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        self.closed.set()
        self.worker.join(timeout=2)
        if self.console:
            self.console.close()


def make_handler(controller):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, content_type='application/json; charset=utf-8'):
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/':
                return self.reply(200, Path(__file__).with_name('web.html').read_bytes(), 'text/html; charset=utf-8')
            if route == '/api/status':
                return self.reply(200, controller.status())
            if route == '/api/recordings':
                return self.reply(200, controller.recordings())
            self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            # This panel owns local devices. Accept JSON actions only from its
            # own page, so unrelated browser pages cannot issue robot commands.
            origin = self.headers.get('Origin')
            expected = 'http://' + self.headers.get('Host', '')
            if origin and origin != expected:
                return self.reply(403, {'error': 'Origin mismatch'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply(415, {'error': 'JSON required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                route = urlsplit(self.path).path
                if route == '/api/start':
                    controller.start(body)
                elif route == '/api/replay':
                    controller.start(body, replay=True)
                elif route == '/api/stop':
                    controller.stop()
                elif route == '/api/record/start':
                    controller.record()
                elif route == '/api/record/stop':
                    controller.stop_recording()
                elif route == '/api/teleop/gesture':
                    controller.set_gesture(body.get('enabled'))
                else:
                    return self.reply(404, {'error': 'Not found'})
                self.reply(200, controller.status())
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
                self.reply(400, {'error': str(error)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--frame-port', type=int, default=8001)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.frame_port <= 65535 or args.port == args.frame_port:
        parser.error('Ports must be distinct and in 1..65535')
    controller = Controller(args.frame_port, args.config)
    try:
        with ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(controller)) as server:
            print(f'Quest Teleop: http://127.0.0.1:{args.port}  (Ctrl+C stops all local workers)', flush=True)
            server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()


if __name__ == '__main__':
    main()
