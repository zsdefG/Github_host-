import struct, socket, random, sys

def make_query(name):
    tid = struct.pack('>H', random.randint(0, 65535))
    qname = b''.join(bytes([len(l)]) + l.encode() for l in name.split('.')) + b'\x00'
    return tid + struct.pack('>H', 0x0100) + struct.pack('>HHHH', 1, 0, 0, 0) + qname + struct.pack('>HH', 1, 1)

def send(host, port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(6)
    s.sendto(make_query(name), (host, port))
    data, _ = s.recvfrom(4096)
    an = struct.unpack('>H', data[6:8])[0]
    rcode = data[3] & 0x0f
    out = []
    if an:
        off = 12
        while data[off] != 0:
            ln = data[off]
            if ln & 0xC0 == 0xC0:
                off += 2
                break
            off += 1 + ln
        off += 5
        for _ in range(an):
            if data[off] & 0xC0 == 0xC0:
                off += 2
            else:
                while data[off] != 0:
                    off += 1 + data[off]
                off += 1
            typ, cls, ttl, rdlen = struct.unpack('>HHIH', data[off:off + 10])
            off += 10
            rdata = data[off:off + rdlen]
            off += rdlen
            if typ == 1 and rdlen == 4:
                out.append(socket.inet_ntoa(rdata))
    print(f'{name}: rcode={rcode} ANCOUNT={an} A={out}')
    s.close()

port = int(sys.argv[1]) if len(sys.argv) > 1 else 15353
send('127.0.0.1', port, 'github.com')
send('127.0.0.1', port, 'raw.githubusercontent.com')
send('127.0.0.1', port, 'no-such-xxx-aaaa.com')
