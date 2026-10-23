"""Portable configuration, resolved once and passed explicitly to children."""
from copy import deepcopy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'transport': 'usb', 'port': None, 'scale': 1.0,
    'only': 'both', 'side': 'right', 'wsl_distro': 'Ubuntu-22.04',
    'hand_python': '~/.venv-quest-teleop/bin/python',
    'right_hand_serial': 'WH2KA01260905017',
    'left_hand_serial': 'WH2JA01260902022',
    'ros_domain_id': 24,
    'thor_host': '6.6.8.100', 'thor_port': 8002,
}


def load_config(path=None):
    chosen = path or os.environ.get('QUEST_TELEOP_CONFIG')
    chosen = Path(chosen).expanduser().resolve() if chosen else ROOT / 'config.json'
    config = deepcopy(DEFAULTS)
    if chosen.exists():
        overrides = json.loads(chosen.read_text(encoding='utf-8-sig'))
        if not isinstance(overrides, dict):
            raise ValueError('config must be a JSON object')
        unknown = set(overrides) - set(DEFAULTS)
        if unknown:
            raise ValueError('unknown config keys: ' + ', '.join(sorted(unknown)))
        config.update(overrides)
    elif path or os.environ.get('QUEST_TELEOP_CONFIG'):
        raise FileNotFoundError(chosen)
    if config['side'] not in ('left', 'right', 'both'):
        raise ValueError('side must be left, right or both')
    serials = [config[side + '_hand_serial'] for side in ('left', 'right')]
    if any(not isinstance(sn, str) for sn in serials):
        raise ValueError('hand serials must be strings')
    if serials[0] and serials[0] == serials[1]:
        raise ValueError('left and right hands must have different serial numbers')
    import math
    if not math.isfinite(config['scale']) or config['scale'] <= 0:
        raise ValueError('scale must be finite and positive')
    if config['only'] not in ('both', 'arm', 'hand') or config['transport'] not in ('usb', 'udp'):
        raise ValueError('invalid only or transport')
    if config['port'] is not None and (type(config['port']) is not int or not 1 <= config['port'] <= 65535):
        raise ValueError('port must be an integer in 1..65535')
    return config
