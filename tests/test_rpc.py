import sys
import unittest

from codex_airlift.rpc import RpcClient


class RpcTest(unittest.TestCase):
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
