"""TCP latest-state transport, with one outstanding request per consumer."""
import json
import select
import socket
import time


class FramePublisher:
    def __init__(self, host, port, on_request=None):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            option = getattr(socket, 'SO_EXCLUSIVEADDRUSE', socket.SO_REUSEADDR)
            self.server.setsockopt(socket.SOL_SOCKET, option, 1)
            self.server.bind((host, port))
            self.server.listen()
            self.server.setblocking(False)
        except OSError:
            self.server.close()
            raise
        # Per client: None = idle, b'' = requested, memoryview = unsent reply.
        self.clients = {}
        self.requests = {}
        self.on_request = on_request
        self.latest = None

    def _drop(self, client):
        self.clients.pop(client, None)
        self.requests.pop(client, None)
        client.close()

    def publish(self, frame):
        # Unrequested frames replace the cache, never enter a consumer queue.
        self.latest = frame
        self.poll()

    def poll(self):
        """Serve requests even when no new Quest frame has arrived."""
        while True:
            try:
                client, _ = self.server.accept()
            except BlockingIOError:
                break
            # Target exchange has one writer; Quest frames allow many readers.
            if self.on_request and self.clients:
                client.close()
                continue
            client.setblocking(False)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.clients[client] = None
            self.requests[client] = bytearray()
        if not self.clients:
            return
        # Each consumer requests again only after consuming its last reply.
        # Frames produced while it computes are discarded, never queued.
        readable, writable, failed = select.select(self.clients, self.clients, self.clients, 0)
        for client in failed:
            self._drop(client)
        for client in readable:
            if client not in self.clients:
                continue
            try:
                chunk = client.recv(65536 if self.on_request else 1)
                if not chunk or self.clients[client] is not None:
                    self._drop(client)
                    continue
                if self.on_request:
                    request = self.requests[client]
                    request.extend(chunk)
                    if len(request) > 65536:
                        raise ValueError('request too large')
                    if b'\n' not in request:
                        continue
                    response = self.on_request(json.loads(request))
                    request.clear()
                    self.clients[client] = memoryview(json.dumps(
                        {**response, 'sent_unix_ns': time.time_ns()},
                        allow_nan=False, separators=(',', ':')).encode() + b'\n')
                elif chunk == b'?':
                    self.clients[client] = b''
                else:
                    self._drop(client)
            except BlockingIOError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                self._drop(client)
        for client in writable:
            if client not in self.clients or self.clients[client] is None:
                continue
            reply = self.clients[client]
            if not reply:
                if self.latest is None:
                    continue
                reply = memoryview(json.dumps(
                    {**self.latest, 'sent_unix_ns': time.time_ns()},
                    allow_nan=False, separators=(',', ':')).encode() + b'\n')
            try:
                sent = client.send(reply)
                if not sent:
                    self._drop(client)
                else:
                    self.clients[client] = reply[sent:] if sent < len(reply) else None
            except BlockingIOError:
                pass
            except OSError:
                self._drop(client)

    def close(self):
        for client in list(self.clients):
            self._drop(client)
        self.server.close()


class FrameSubscriber:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.socket = None
        self.pending = False
        self.buffer = bytearray()

    def next_latest(self, timeout=.2, request=None):
        deadline = time.monotonic() + timeout
        try:
            if self.socket is None:
                self.socket = socket.create_connection((self.host, self.port), timeout=timeout)
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.settimeout(timeout)
            if not self.pending:
                self.requested_at = time.monotonic_ns()
                self.socket.sendall(b'?' if request is None else
                                    json.dumps(request, allow_nan=False, separators=(',', ':')).encode() + b'\n')
                self.pending = True
            while True:
                newline = self.buffer.find(b'\n')
                if newline >= 0:
                    frame = json.loads(self.buffer[:newline])
                    del self.buffer[:newline + 1]
                    self.pending = False
                    # Translate packet ages into this machine's clock. Charge
                    # the full round trip as transit time, so unsynchronized
                    # hosts can never make an old frame appear newer.
                    sent = frame.pop('sent_unix_ns')
                    shift = time.time_ns() - sent - (time.monotonic_ns() - self.requested_at)
                    frame['source_recv_unix_ns'] = frame['recv_unix_ns']
                    frame['recv_unix_ns'] += shift
                    for packet in frame.get('packets', []):
                        if 'recv_unix_ns' in packet:
                            packet['source_recv_unix_ns'] = packet['recv_unix_ns']
                            packet['recv_unix_ns'] += shift
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.socket.settimeout(remaining)
                chunk = self.socket.recv(65536)
                if not chunk:
                    raise ConnectionError('TCP publisher disconnected')
                self.buffer.extend(chunk)
        except TimeoutError:
            return None  # Retain the request and partial reply until more data arrives.
        except (OSError, ValueError):
            self.close()
            raise

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.buffer.clear()
        self.pending = False
