"""A small, real MQTT 3.1.1 server, for tests that must not mock the broker.

WHY IT IS REAL. The publisher's contract is not "call paho" -- it is "a record
leaves the local queue ONLY when a broker has acknowledged it". That is a
statement about PUBACK, and a mock returning True proves nothing about it: the
exact failure it guards against is a gateway deleting a shift's production
because send() returned without error.

So this parses CONNECT, answers CONNACK, parses PUBLISH, and -- the part that
matters -- can be told to WITHHOLD the PUBACK, which is what a saturated or
half-dead broker does in practice. It can also refuse the credentials, and it
records what it saw on the wire so a test can assert the client sent what it
claimed to.

Deliberately NOT a general broker: it handles what this client sends and
pretends about nothing else.

Shared rather than duplicated: test_publisher_against_broker.py proves the
publisher against it, and test_edge_runner.py proves the assembled Gateway
against it. Two copies would drift, and a broker stub that drifts makes one of
those suites quietly stop testing what it says it does.
"""
import socket
import struct
import threading


def _varint(data, i):
    """MQTT's remaining-length encoding. Returns (value, next_index)."""
    multiplier, value = 1, 0
    while True:
        byte = data[i]
        i += 1
        value += (byte & 127) * multiplier
        if not byte & 128:
            return value, i
        multiplier *= 128
        if multiplier > 128 ** 3:
            raise ValueError("malformed remaining length")


class TinyBroker(threading.Thread):
    """Enough MQTT 3.1.1 to hold a real client honestly."""

    CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
    SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 12, 13, 14

    def __init__(self, port, ack=True, refuse=False):
        super().__init__(daemon=True)
        self.port = port
        self.ack = ack                 # withhold PUBACK when False
        self.refuse = refuse           # answer CONNACK with "bad credentials"
        self.published = []            # (topic, payload)
        self.credentials = []          # (username, password) as SEEN on the wire
        self.running = True
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(4)
        self._srv.settimeout(0.5)

    def run(self):
        while self.running:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(5)
        buf = b""
        try:
            while self.running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while True:
                    consumed = self._one_packet(conn, buf)
                    if consumed == 0:
                        break
                    buf = buf[consumed:]
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _one_packet(self, conn, buf):
        if len(buf) < 2:
            return 0
        kind = buf[0] >> 4
        flags = buf[0] & 0x0F
        try:
            length, header_end = _varint(buf, 1)
        except (IndexError, ValueError):
            return 0
        total = header_end + length
        if len(buf) < total:
            return 0
        body = buf[header_end:total]

        if kind == self.CONNECT:
            self._read_connect(body)
            # 0 = accepted, 5 = not authorised
            conn.sendall(bytes([self.CONNACK << 4, 2, 0, 5 if self.refuse else 0]))
        elif kind == self.PUBLISH:
            topic_len = struct.unpack("!H", body[:2])[0]
            topic = body[2:2 + topic_len].decode("utf-8", "replace")
            rest = body[2 + topic_len:]
            qos = (flags >> 1) & 3
            packet_id = None
            if qos > 0:
                packet_id = struct.unpack("!H", rest[:2])[0]
                rest = rest[2:]
            self.published.append((topic, rest.decode("utf-8", "replace")))
            if qos > 0 and self.ack:
                conn.sendall(bytes([self.PUBACK << 4, 2]) + struct.pack("!H", packet_id))
        elif kind == self.SUBSCRIBE:
            packet_id = struct.unpack("!H", body[:2])[0]
            conn.sendall(bytes([self.SUBACK << 4, 3]) + struct.pack("!H", packet_id) + b"\x00")
        elif kind == self.PINGREQ:
            conn.sendall(bytes([self.PINGRESP << 4, 0]))
        elif kind == self.DISCONNECT:
            return total
        return total

    def _read_connect(self, body):
        i = 0
        name_len = struct.unpack("!H", body[i:i + 2])[0]
        i += 2 + name_len
        i += 1                                   # protocol level
        connect_flags = body[i]
        i += 1
        i += 2                                   # keepalive
        client_len = struct.unpack("!H", body[i:i + 2])[0]
        i += 2 + client_len
        if connect_flags & 0x04:                 # will
            for _ in range(2):
                field_len = struct.unpack("!H", body[i:i + 2])[0]
                i += 2 + field_len
        username = password = None
        if connect_flags & 0x80:
            field_len = struct.unpack("!H", body[i:i + 2])[0]
            username = body[i + 2:i + 2 + field_len].decode("utf-8", "replace")
            i += 2 + field_len
        if connect_flags & 0x40:
            field_len = struct.unpack("!H", body[i:i + 2])[0]
            password = body[i + 2:i + 2 + field_len].decode("utf-8", "replace")
        self.credentials.append((username, password))

    def stop(self):
        self.running = False
        try:
            self._srv.close()
        except OSError:
            pass
