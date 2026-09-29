import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from DGXGateway.dgx_gateway.adapters.music3 import Music3Adapter

class ComfyProbeTests(unittest.TestCase):
 def test_collected_transient_unit_is_stopped(self):
  def run(node,argv,timeout):
   if argv[0]=='systemctl': return SimpleNamespace(returncode=4,stdout='not-found\ninactive\n')
   if argv[0]=='pgrep':
    self.assertIn('[m]ain.py',argv[-1]);return SimpleNamespace(returncode=1,stdout='')
   return SimpleNamespace(returncode=0,stdout='')
  adapter=Music3Adapter(SimpleNamespace(nodes=[SimpleNamespace(role='head')]),SimpleNamespace(run=run))
  self.assertEqual(adapter.observe().state,'STOPPED')
  self.assertTrue(adapter.stopped_proof(SimpleNamespace(guard=Mock(),lease_generation='test'))['stopped'])

 def test_live_listener_blocks_stopped_proof(self):
  def run(node,argv,timeout):
   if argv[0]=='systemctl':return SimpleNamespace(returncode=4,stdout='not-found\ninactive\n')
   if argv[0]=='pgrep':return SimpleNamespace(returncode=1,stdout='')
   return SimpleNamespace(returncode=0,stdout='LISTEN 8188')
  adapter=Music3Adapter(SimpleNamespace(nodes=[SimpleNamespace(role='head')]),SimpleNamespace(run=run))
  with self.assertRaises(RuntimeError):adapter.stopped_proof(SimpleNamespace(guard=Mock(),lease_generation='test'))

if __name__=='__main__':unittest.main()
