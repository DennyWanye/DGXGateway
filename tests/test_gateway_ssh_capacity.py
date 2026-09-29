import threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from DGXGateway.dgx_gateway.ssh import FixedSSH

class SSHCapacityTest(unittest.TestCase):
 def test_adapters_share_handshake_limit(self):
  state={'active':0,'peak':0};lock=threading.Lock()
  def run(*args,**kw):
   with lock:
    state['active']+=1;state['peak']=max(state['peak'],state['active'])
   time.sleep(.03)
   with lock:state['active']-=1
   return SimpleNamespace(returncode=0,stdout='',stderr='')
  node=SimpleNamespace(user='operator',address='127.0.0.1')
  with patch('DGXGateway.dgx_gateway.ssh.subprocess.run',side_effect=run):
   with ThreadPoolExecutor(max_workers=16) as pool:
    list(pool.map(lambda _:FixedSSH().run(node,['true']),range(32)))
  self.assertLessEqual(state['peak'],4);self.assertGreater(state['peak'],1)
if __name__=='__main__':unittest.main()
