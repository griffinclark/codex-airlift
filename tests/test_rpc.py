import sys
import unittest

from codex_airlift.rpc import RpcClient


class RpcTest(unittest.TestCase):
    def test_websocket_proxy_handshake_mask_and_fragmented_response(self):
        program = r'''import sys,json,base64,hashlib,struct
r=sys.stdin.buffer;w=sys.stdout.buffer
header=b''
while not header.endswith(b'\r\n\r\n'): header+=r.read(1)
fields=dict(x.split(': ',1) for x in header.decode().split('\r\n')[1:] if ': ' in x)
key=fields['Sec-WebSocket-Key']
accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
w.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode());w.flush()
first,second=r.read(2);assert first==129 and second&128
size=second&127
if size==126:size=struct.unpack('!H',r.read(2))[0]
elif size==127:size=struct.unpack('!Q',r.read(8))[0]
mask=r.read(4);body=r.read(size)
request=json.loads(bytes(b^mask[i%4] for i,b in enumerate(body)))
response=json.dumps({'id':request['id'],'result':{'ok':True}}).encode()
part=response[:5];rest=response[5:]
w.write(bytes([1,len(part)])+part+bytes([128,len(rest)])+rest);w.flush()
'''
        client = RpcClient([sys.executable, "-u", "-c", program], websocket=True)
        try:
            self.assertEqual(client.request("example", {"text": "x" * 200}), {"ok": True})
        finally:
            client.close()

    def test_stdio_response_notification_and_eof(self):
        program = """import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r: continue
 print(json.dumps({'method':'notice','params':{'test':True}}),flush=True)
 print(json.dumps({'id':r['id'],'result':{'method':r['method']}}),flush=True)
 if r['method']=='done': break
"""
        client = RpcClient([sys.executable, "-u", "-c", program])
        try:
            self.assertEqual(client.request("hello", {}), {"method": "hello"})
            self.assertEqual(client.events[0]["method"], "notice")
            client.request("done", {})
            with self.assertRaises(EOFError):
                client.receive(timeout=5)
        finally:
            client.close()
