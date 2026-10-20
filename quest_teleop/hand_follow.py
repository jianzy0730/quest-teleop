"""Quest hand preview and live follow. Ctrl+C stops."""
import argparse
import json
import math
from pathlib import Path
import time

from .config import ROOT, load_config
from .quest.frames import LiveGate, packets_by_key
from .ipc import FrameSubscriber
from .retarget.hand import WujiHandRetargeter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame-host', default='127.0.0.1')
    parser.add_argument('--frame-port', type=int, default=8001)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--side', choices=['left', 'right'], default='right')
    parser.add_argument('--output', type=Path, default=ROOT / 'logs')
    args = parser.parse_args()
    key = args.side.title() + '/landmarks'
    mapper = WujiHandRetargeter(args.side)
    print(json.dumps({'joint_mapping': mapper.mapping, 'rotation': mapper.retargeter.rotation_xyz}), flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    log_path = args.output / ('hand_' + args.side + '_' + time.strftime('%Y%m%d_%H%M%S') + '.json')
    hand = None
    subscriber = FrameSubscriber(args.frame_host, args.frame_port)
    status = None
    gate = LiveGate()
    begun = time.monotonic()
    preview_samples = 0
    last_report = 0
    last_phase = None
    last_hardware_warning = -math.inf
    previous_hardware_limits = None
    previous_diagnostic_warnings = None
    last_diagnostic_warning = -math.inf
    last_tracking_warning = -math.inf
    last_pause_report = -math.inf
    failure = None
    timing = {}
    try:
        while True:
            if args.stop_file and args.stop_file.exists():
                break
            if hand is not None:
                status = hand.status()
                if status['phase'] == 'stopped':
                    if status['diagnosis']:
                        raise RuntimeError(status['diagnosis'])
                    break
            try:
                frame = subscriber.next_latest()
                if frame is None:
                    raise TimeoutError('no Quest frame received')
                rec, packet = packets_by_key(frame)[key]
            except (OSError, KeyError, ValueError) as exc:
                if hand is not None:
                    if time.monotonic() - last_pause_report > 2:
                        print(json.dumps({'warning': 'Quest input unavailable; hand paused', 'detail': str(exc)}), flush=True)
                        last_pause_report = time.monotonic()
                    time.sleep(.05)
                    continue
                time.sleep(.05)
                continue
            try:
                accepted = gate.accept(rec, packet)
            except (TimeoutError, ValueError, KeyError, TypeError) as exc:
                if time.monotonic() - last_pause_report > 2:
                    print(json.dumps({'warning': 'Quest input delayed; hand paused', 'detail': str(exc)}), flush=True)
                    last_pause_report = time.monotonic()
                time.sleep(.05)
                continue
            if not accepted:
                time.sleep(.01)
                continue
            t = time.monotonic()
            timing = {'input_age_ms': (time.time_ns() - rec['recv_unix_ns']) / 1e6}
            try:
                q = mapper.solve(packet['data']['points'])
                gate.fresh(rec, time.time_ns())
            except (ValueError, KeyError, TypeError, IndexError, TimeoutError) as error:
                if time.monotonic() - last_pause_report > 2:
                    print(json.dumps({'warning': 'hand sample skipped; waiting for fresh valid input',
                                      'detail': str(error)}), flush=True)
                    last_pause_report = time.monotonic()
                time.sleep(.005)
                continue
            if gate.restarted:
                gate.restarted = False
                print(json.dumps({'warning': 'Quest source restarted; hand resumes absolute poses'}), flush=True)
            timing['retarget_ms'] = (time.monotonic() - t) * 1000
            preview_samples += 1
            if args.execute:
                # Initialize only after fresh input has warmed up the mapper.
                if hand is None and preview_samples >= 20 and time.monotonic() - begun >= 1:
                    from .devices.wuji import WujiHand
                    hand = WujiHand(args.side, load_config()[args.side + '_hand_serial'], args.stop_file)
                    hand.start()
                    continue
                if hand is not None:
                    status = hand.status()
                    if status['phase'] == 'stopped':
                        if status['diagnosis']:
                            raise RuntimeError(status['diagnosis'])
                        break
                    if status['phase'] == 'initializing':
                        time.sleep(.01)
                        continue
                    if (time.time_ns() - rec['recv_unix_ns']) / 1e9 > .2:
                        continue
                    t_target = time.monotonic()
                    try:
                        status = hand.update(q.tolist(), gate.seq)
                    except InterruptedError:
                        if args.stop_file and args.stop_file.exists():
                            break
                        raise
                    timing['target_ms'] = (time.monotonic() - t_target) * 1000
                    hardware_limits = (status.get('position_limit_joints', []),
                                       status.get('limit_clamped_joints', []),
                                       status.get('limit_direction_unknown', []),
                                       status.get('feedback_limit_joints', []))
                    now = time.monotonic()
                    if any(hardware_limits) and (hardware_limits != previous_hardware_limits or now - last_hardware_warning >= 1):
                        print(json.dumps({'warning': 'hand position limit; following continues',
                                          'active_joints': hardware_limits[0],
                                          'clamped_joints': hardware_limits[1],
                                          'held_until_limit_clears': hardware_limits[2],
                                          'feedback_outside_model': hardware_limits[3]}), flush=True)
                        last_hardware_warning = now
                    previous_hardware_limits = hardware_limits
                    diagnostic_warnings = status.get('diagnostic_warnings', [])
                    if diagnostic_warnings and (diagnostic_warnings != previous_diagnostic_warnings or now - last_diagnostic_warning >= 2):
                        print(json.dumps({'warning': 'hand firmware warning; following continues',
                                          'diagnostics': diagnostic_warnings}), flush=True)
                        last_diagnostic_warning = now
                    previous_diagnostic_warnings = diagnostic_warnings
                    tracking_warning = status.get('tracking_warning')
                    if tracking_warning and now - last_tracking_warning >= 2:
                        print(json.dumps({'warning': 'hand tracking lag; following continues',
                                          'detail': tracking_warning}), flush=True)
                        last_tracking_warning = now
                    if status.get('completion') == 'rebase_required':
                        # The SDK worker holds measured pose for this handshake;
                        # next fresh frame resumes absolute targets.
                        print(json.dumps({'warning': 'Quest input resumed; hand will follow next absolute pose'}), flush=True)
            phase = status.get('phase') if status else 'preview'
            if phase != last_phase:
                print(json.dumps({'phase': phase}), flush=True)
                last_phase = phase
            if not args.execute and time.monotonic() - last_report > 1:
                print(json.dumps({'preview': {'q_sdk': q.round(3).tolist()}}), flush=True)
                last_report = time.monotonic()
            time.sleep(.005)
    except KeyboardInterrupt:
        print('STOP: Ctrl+C', flush=True)
    except Exception as exc:
        failure = str(exc) or type(exc).__name__
        print('STOP: ' + failure + ' ' + json.dumps(timing), flush=True)
    finally:
        if subscriber is not None:
            subscriber.close()
        if hand is not None:
            try:
                status = hand.close()
                if status['diagnosis']:
                    failure = failure or status['diagnosis']
            except Exception as exc:
                status = hand.status()
                failure = (failure or '') + '; cleanup: ' + str(exc)
        summary = {'side': args.side, 'execute': args.execute, 'samples': preview_samples, 'failure': failure, 'last_timing_ms': timing, 'final_stream': status, 'log': str(log_path)}
        log_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary), flush=True)
    if failure:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
