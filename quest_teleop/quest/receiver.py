"""Receive HTS UDP/TCP telemetry. Does not connect to robot drivers.

Started by the follow command as a receiver worker.
Coordinates are kept in the sender's Unity convention; no robot mapping is applied.
"""
from __future__ import annotations
from ..config import ROOT

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
import json
import math
from pathlib import Path
import socket
import time

from .frames import FrameAssembler
from ..ipc import FramePublisher


def inspect_packet(packet):
    """Validate numeric geometry and return a diagnostic payload."""
    from hand_tracking_sdk.models import LandmarksPacket, WristPacket

    data = packet.data.to_dict()
    if isinstance(packet, LandmarksPacket):
        values = [v for point in packet.data.points for v in point]
    else:
        values = list(data.values())
    if not all(math.isfinite(v) for v in values):
        raise ValueError("non-finite geometry")
    if isinstance(packet, WristPacket):
        q = packet.data
        norm = math.sqrt(q.qx**2 + q.qy**2 + q.qz**2 + q.qw**2)
        if not 0.9 <= norm <= 1.1:
            raise ValueError(f"invalid quaternion norm {norm}")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--transport", choices=["udp", "tcp"], default="udp")
    parser.add_argument("--port", type=int)
    parser.add_argument("--peer", help="Only accept datagrams from this IP")
    parser.add_argument("--output", type=Path, default=ROOT / "logs")
    parser.add_argument('--frame-host', default='127.0.0.1', help=argparse.SUPPRESS)
    parser.add_argument('--frame-port', type=int, default=8001, help=argparse.SUPPRESS)
    parser.add_argument('--only', choices=['arm', 'hand', 'both'], default='both')
    parser.add_argument('--side', choices=['left', 'right', 'both'], default='right')
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    # Keep the command dispatcher and --help usable from the base Windows
    # environment; the receiver SDK is a project dependency.
    from hand_tracking_sdk.exceptions import (ParseError,
                                              TransportDisconnectedError,
                                              TransportTimeoutError)
    from hand_tracking_sdk.parser import parse_line
    from hand_tracking_sdk.transport import TCPServerConfig, TCPServerLineReceiver
    if args.port is None:
        args.port = 8000 if args.transport == "tcp" else 9000
    if args.peer and args.transport == "tcp":
        parser.error("--peer is available only for UDP; bind TCP to --host 127.0.0.1 for USB")
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    summary_path = args.output / f"summary_{stamp}.json"
    counts, peers = Counter(), set()
    assembler = FrameAssembler(only=args.only, side=args.side)
    errors = reported_errors = 0
    started = last_report = time.monotonic()
    with ExitStack() as stack:
        publisher = FramePublisher(args.frame_host, args.frame_port)
        stack.callback(publisher.close)
        if args.transport == "udp":
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            sock.bind((args.host, args.port))
            sock.settimeout(0.25)
        else:
            receiver = stack.enter_context(TCPServerLineReceiver(TCPServerConfig(
                host=args.host, port=args.port, accept_timeout_s=0.25, read_timeout_s=0.25)))
        print(f"Listening {args.transport.upper()} {args.host}:{args.port}; frames: {args.frame_host}:{args.frame_port}", flush=True)
        try:
            while True:
                if args.stop_file and args.stop_file.exists():
                    break
                address = None
                try:
                    if args.transport == "udp":
                        payload, address = sock.recvfrom(65535)
                    else:
                        payload = receiver.recv_line().encode("utf-8")
                except TransportDisconnectedError:
                    print(json.dumps({'event': 'Quest TCP disconnected; waiting for reconnect'}), flush=True)
                    publisher.latest = None
                    assembler = FrameAssembler(only=args.only, side=args.side)
                    payload = None
                except (socket.timeout, TransportTimeoutError):
                    payload = None
                except UnicodeDecodeError:
                    errors += 1
                    payload = None
                if payload is not None and (not args.peer or address[0] == args.peer):
                    peer = address[0] if address else 'tcp_client'
                    if peer not in peers:
                        peers.add(peer)
                        print(json.dumps({'event': 'Quest connected', 'peer': peer}), flush=True)
                    record = {"recv_unix_ns": time.time_ns(),
                              "peer": list(address) if address else None}
                    packets = []
                    for line in payload.decode("utf-8", "replace").splitlines():
                        if not line.strip():
                            continue
                        try:
                            packet = parse_line(line)
                            data = inspect_packet(packet)
                        except (ParseError, ValueError) as exc:
                            errors += 1
                            packets.append({"error": str(exc)})
                            continue
                        key = f"{packet.side.value}/{packet.kind.value}"
                        counts[key] += 1
                        item = {"key": key, "data": data,
                                "debug": asdict(packet.debug) if packet.debug else None}
                        packets.append(item)
                    record["packets"] = packets
                    frame = assembler.add(record)
                    if frame is not None:
                        publisher.publish(frame)
                publisher.poll()
                now = time.monotonic()
                if now - last_report >= 2:
                    if errors != reported_errors:
                        print(json.dumps({'warning': 'invalid Quest packets skipped', 'parse_errors': errors}), flush=True)
                        reported_errors = errors
                    last_report = now
        except KeyboardInterrupt:
            pass
    summary = {"elapsed_s": round(time.monotonic() - started, 2), "peers": sorted(peers),
               "valid_packets": dict(counts), "parse_errors": errors}
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Saved {summary_path}", flush=True)


if __name__ == "__main__":
    main()
