"""Wuji Hand 2 SDK control, local to each hand follower."""
import math
import threading
import time
import json
import os
from pathlib import Path


os.environ.setdefault("WUJI_TELEMETRY_DISABLE", "1")

LABELS = [f"{finger}_S{j}" for finger in
          ("thumb", "index", "middle", "ring", "pinky") for j in range(1, 5)]
STREAM_POLICIES = {"position_limit_policy": "clamp_and_warn",
                   "diagnostic_warning_policy": "warn_and_continue",
                   "tracking_error_policy": "warn_and_continue",
                   "feedback_limit_policy": "warn_and_continue",
                   "learned_limit_policy": "while_active"}


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def positions(values, limits=None):
    if not isinstance(values, list) or len(values) != 20:
        raise ValueError("20 joint positions required")
    result = [number(v, "position") for v in values]
    # Targets obey model bounds; measured feedback remains unmodified.
    return result if limits is None else [min(hi, max(lo, v)) for v, (lo, hi) in zip(result, limits)]


def unpack_states(frame, sdk):
    values = [None] * 20
    if len(frame.joints) != 20:
        raise RuntimeError("incomplete joint state frame")
    for joint in frame.joints:
        i = sdk.WujiHand2.nid_to_joint_index(joint.nid)
        if not 0 <= i < 20 or values[i] is not None:
            raise RuntimeError("joint state NID mismatch")
        values[i] = number(joint.position, "feedback position")
    return values


def check_diagnostics(frame, sdk, expected_state, warnings=None):
    seen = set()
    limited = []
    if len(frame.joints) != 20:
        raise RuntimeError("incomplete diagnostics frame")
    for joint in frame.joints:
        i = sdk.WujiHand2.nid_to_joint_index(joint.nid)
        if not 0 <= i < 20 or i in seen:
            raise RuntimeError("diagnostics NID mismatch")
        seen.add(i)
        code = joint.error_code_current
        info = sdk.WujiHand2.describe_error(code) if code else None
        warning = isinstance(info, dict) and info.get('severity') == 'Warning'
        if (code and not warning) or (expected_state is not None and joint.status_word.ext_state != expected_state):
            raise RuntimeError(f"{LABELS[i]} error={joint.error_code_current} "
                               f"state={joint.status_word.ext_state} "
                               f"position_limit={joint.status_word.position_limit_active}")
        if code and warning and warnings is not None:
            warnings.append({'joint': i, 'label': LABELS[i], 'code': code,
                             'name': info.get('name'), 'severity': 'Warning'})
        if joint.status_word.position_limit_active:
            limited.append(i)
    return sorted(limited)


class PositionLimits:
    """Remember the measured boundary reached by an outward command.

    The status bit has no direction. Infer it only from a previously sent
    command, never from a new operator target. With no such evidence, hold
    the flagged joint until the bit clears. Cleared bits release learned bounds.
    """

    def __init__(self):
        self.lower = [None] * 20
        self.upper = [None] * 20
        self.active = set()
        self.unknown = set()

    def observe(self, active, actual, previous_command):
        active = set(active)
        for i in self.active - active:
            self.lower[i] = self.upper[i] = None
        self.unknown.intersection_update(active)
        for i in active - self.active:
            delta = 0 if previous_command is None else previous_command[i] - actual[i]
            if delta > .001:
                self.upper[i] = actual[i] if self.upper[i] is None else min(self.upper[i], actual[i])
            elif delta < -.001:
                self.lower[i] = actual[i] if self.lower[i] is None else max(self.lower[i], actual[i])
            else:
                self.unknown.add(i)
        self.active = active

    def clamp(self, target, actual):
        result = target[:]
        for i in range(20):
            if i in self.unknown:
                result[i] = actual[i]
            else:
                if self.lower[i] is not None:
                    result[i] = max(self.lower[i], result[i])
                if self.upper[i] is not None:
                    result[i] = min(self.upper[i], result[i])
        return result


class WujiHand:
    """Direct SDK connection; a local worker sends the latest target at 100 Hz."""

    def __init__(self, side, serial, stop_file=None, sdk=None):
        if side not in ('left', 'right') or not serial:
            raise ValueError('hand side and serial required')
        if sdk is None:
            import wuji_sdk as sdk
        self.sdk = sdk
        self.manager = sdk.SdkManager.instance()
        self.side, self.serial = side, serial
        self.stop_file = stop_file
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.config = json.loads((Path(__file__).resolve().parents[1] / 'assets/hand.json').read_text())
        self.safety = self.config['safety']
        self.session = {"side": side, "phase": "idle", "last_seq": -1,
                        "last_target_at": None, "target": None, "actual_rad": None,
                        "position_limit_joints": [], "limit_clamped_joints": [],
                        "limit_direction_unknown": [], "diagnostic_warnings": [],
                        "tracking_warning": None, "feedback_limit_joints": [],
                        "disabled_after": None, "diagnosis": "", "cleanup_errors": []}

    def status(self):
        with self.lock:
            return {**STREAM_POLICIES, **{k: v for k, v in self.session.items()
                    if k not in ("target", "last_target_at")}}

    def start(self):
        if self.worker is not None:
            raise RuntimeError('hand already started')
        self._patch(phase='initializing')
        self.worker = threading.Thread(target=self._run, daemon=True, name='wuji-' + self.side)
        self.worker.start()

    def close(self):
        self.cancel.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise TimeoutError('Wuji SDK worker did not finish cleanup')
        status = self.status()
        if status['cleanup_errors']:
            raise RuntimeError('; '.join(status['cleanup_errors']))
        return status

    def _check_cancel(self):
        if self.cancel.is_set() or (self.stop_file and self.stop_file.exists()):
            raise InterruptedError('motion cancelled')

    def _connect(self):
        available = {device.sn: device for device in self.manager.scan()}
        self._check_cancel()
        if self.serial not in available:
            raise RuntimeError(f'{self.side} hand {self.serial} not discovered')
        if available[self.serial].device_type != self.sdk.DeviceType.WujiHand2:
            raise RuntimeError(f'{self.serial} is not a Wuji Hand 2')
        return self.manager.connect(sn=self.serial, device_name='wuji_' + self.side)

    def _positions(self, hand):
        subscription = hand.joint_states().subscribe()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self._check_cancel()
                frame = subscription.recv()
                if frame is not None:
                    try:
                        return unpack_states(frame, self.sdk)
                    except (RuntimeError, ValueError):
                        pass
                time.sleep(.005)
        finally:
            subscription.close()
        raise TimeoutError('no complete joint_states frame in 3 seconds')

    def update(self, values, seq):
        target = positions(values, self.config['gestures']['joint_limits_rad'])
        self._check_cancel()
        with self.lock:
            self._check_cancel()
            s = self.session
            if s['phase'] not in ('awaiting_input', 'enabling', 'running', 'paused', 'rebasing'):
                raise RuntimeError(s['diagnosis'] or 'hand is not accepting targets: ' + s['phase'])
            if seq <= s['last_seq']:
                raise ValueError('target sequence did not advance')
            if s['phase'] == 'paused' or (s['phase'] == 'running' and
                                          time.monotonic() - s['last_target_at'] > .25):
                s.update(phase='rebasing', last_seq=seq, target=s['actual_rad'][:])
                return {'completion': 'rebase_required', 'phase': 'rebasing'}
            s.update(target=target, last_seq=seq, last_target_at=time.monotonic())
            if s['phase'] == 'rebasing':
                s['phase'] = 'running'
        return self.status()

    def _patch(self, **values):
        with self.lock:
            self.session.update(values)

    def _check_diagnostics(self, frame, expected_state):
        warnings = []
        limited = check_diagnostics(frame, self.sdk, expected_state, warnings)
        self._patch(diagnostic_warnings=warnings)
        return limited

    def _run(self):
        hand = publisher = states = diagnostics = None
        enabled = False
        diagnosis = ""
        cleanup_errors = []
        limits = PositionLimits()
        try:
            self._check_cancel()
            hand = self._connect()
            if hand.handedness().get() != self.side or [j.label for j in hand.joints()] != LABELS:
                raise RuntimeError(self.side + "-hand identity/joint order mismatch")
            if hand.online_joints_count().get() != 20:
                raise RuntimeError("20/20 joints required")
            model_limits = self.config["gestures"]["joint_limits_rad"]
            actual = positions(self._positions(hand))
            diagnostics = hand.joint_diagnostics().subscribe()
            deadline = time.monotonic() + 2
            while True:
                self._check_cancel()
                frame = diagnostics.recv()
                if frame is not None:
                    active_limits = self._check_diagnostics(frame, 1)
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("no ready-state diagnostics")
                time.sleep(.005)
            self._patch(actual_rad=actual[:], phase="awaiting_input")
            while True:
                self._check_cancel()
                with self.lock:
                    received_at = self.session["last_target_at"]
                if received_at is not None:
                    break
                time.sleep(.005)
            hand.effort_limit().set(self.safety["effort_limit_a"])
            hand.mit_params().set((self.safety["mit_kp"], self.safety["mit_kd"]))
            states = hand.joint_states().subscribe()
            # Pre-enable Ready frames must not be mistaken for running faults.
            diagnostics.close()
            diagnostics = None
            self._patch(phase="enabling")
            self._check_cancel()
            while True:
                self._check_cancel()
                with self.lock:
                    fresh = time.monotonic() - self.session['last_target_at'] <= .25
                if fresh:
                    break
                time.sleep(.01)
            enabled = True
            hand.enable()
            # Device enable diagnostics still have their own deadline.
            enabling = hand.joint_diagnostics().subscribe()
            try:
                deadline = time.monotonic() + 2
                while True:
                    self._check_cancel()
                    frame = enabling.recv()
                    if frame is not None:
                        if all(j.status_word.ext_state == 2 for j in frame.joints):
                            active_limits = self._check_diagnostics(frame, 2)
                            break
                        # Transition states may differ, but only SDK Warning
                        # severity may continue through the enable handshake.
                        self._check_diagnostics(frame, None)
                    if time.monotonic() > deadline:
                        raise TimeoutError('enable transition timed out')
                    time.sleep(.005)
            finally:
                enabling.close()
            diagnostics = hand.joint_diagnostics().subscribe()
            publisher = hand.joint_command().publish()
            self._patch(phase="running")
            command = actual[:]
            # No command from this session can establish a direction yet.
            limits.observe(active_limits, actual, None)
            tick = state_seen = diag_seen = time.monotonic()
            tracking_bad_since = None
            while True:
                self._check_cancel()
                for _ in range(100):
                    frame = states.recv()
                    if frame is None:
                        break
                    actual = unpack_states(frame, self.sdk)
                    state_seen = time.monotonic()
                for _ in range(100):
                    frame = diagnostics.recv()
                    if frame is None:
                        break
                    self._check_cancel()
                    active_limits = self._check_diagnostics(frame, 2)
                    diag_seen = time.monotonic()
                limits.observe(active_limits, actual, command)
                now = time.monotonic()
                self._check_cancel()
                with self.lock:
                    s = self.session
                    if now - s['last_target_at'] > .25 and s['phase'] == 'running':
                        s.update(phase='paused', target=actual[:])
                    paused = s['phase'] in ('paused', 'rebasing')
                    target = s['target'][:]
                if now - state_seen > .5 or now - diag_seen > 1.5:
                    raise TimeoutError("hand feedback stale")
                requested = target[:]
                target = limits.clamp(target, actual)
                # A boundary may have been reported since the previous send.
                command = limits.clamp(command, actual)
                if paused:
                    # Hold the snapshot taken on entering pause, while still
                    # checking device feedback, diagnostics and cancellation.
                    command = target[:]
                    tracking_bad_since = None
                # Tracking lag is telemetry, not a firmware position limit or
                # stop-level diagnostic. Report it without ending the stream.
                errors = [abs(c - a) for c, a in zip(command, actual)]
                tracking_warning = None
                if not paused and max(errors) > .25:
                    tracking_bad_since = now if tracking_bad_since is None else tracking_bad_since
                    if now - tracking_bad_since > .5:
                        tracking_warning = {'joints': [i for i, e in enumerate(errors) if e > .25],
                                            'max_error_rad': max(errors),
                                            'duration_s': now - tracking_bad_since}
                else:
                    tracking_bad_since = None
                command = positions(target, model_limits)
                publisher.send([self.sdk.JointCommand(v, 0., 0.) for v in command])
                self._patch(actual_rad=actual[:], command_rad=command[:],
                            feedback_limit_joints=[i for i, (v, (lo, hi)) in enumerate(zip(actual, model_limits))
                                                   if v < lo - .01 or v > hi + .01],
                            tracking_warning=tracking_warning,
                            position_limit_joints=sorted(limits.active),
                            limit_direction_unknown=sorted(limits.unknown),
                            limit_clamped_joints=[i for i in range(20) if abs(requested[i] - target[i]) > 1e-9],
                            learned_lower_rad=limits.lower[:], learned_upper_rad=limits.upper[:])
                tick = max(tick + .01, time.monotonic())
                time.sleep(max(0, tick - time.monotonic()))
        except InterruptedError:
            pass
        except BaseException as error:
            diagnosis = str(error) or type(error).__name__
        finally:
            try:
                if enabled:
                    hand.disable()
                    # SDK disable() acknowledgement alone does not prove Ready.
                    verification = hand.joint_diagnostics().subscribe()
                    try:
                        deadline = time.monotonic() + 2
                        while True:
                            frame = verification.recv()
                            if frame is not None:
                                # Motors briefly report state 0 while the disable
                                # transition completes; wait for all 20 Ready.
                                if all(j.status_word.ext_state == 1 for j in frame.joints):
                                    self._check_diagnostics(frame, 1)
                                    break
                                self._check_diagnostics(frame, None)
                            if time.monotonic() > deadline:
                                raise TimeoutError('disabled state not verified')
                            time.sleep(.005)
                    finally:
                        verification.close()
            except Exception as error:
                cleanup_errors.append("disable: " + str(error))
            for resource in (publisher, states, diagnostics):
                try:
                    if resource is not None:
                        resource.close()
                except Exception as error:
                    cleanup_errors.append(str(error))
            try:
                if hand is not None:
                    self.manager.disconnect('wuji_' + self.side)
            except Exception as error:
                cleanup_errors.append(str(error))
            self._patch(phase="stopped", diagnosis=diagnosis,
                        disabled_after=(not cleanup_errors) if enabled else None,
                        cleanup_errors=cleanup_errors)
