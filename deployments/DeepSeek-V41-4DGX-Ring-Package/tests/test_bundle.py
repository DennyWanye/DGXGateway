import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'scripts'))
import common
import model_assets as assets
import ringctl
import ring_net
import api_probe


def configured():
    c = json.loads((BASE / 'config/cluster.example.json').read_text())
    for i, n in enumerate(c['nodes']):
        n.update(management_ip=f'192.168.50.{20+i}', management_iface='mgmt0',
                 prev_iface='rdma0', next_iface='rdma1', unused_rdma_ifaces=['rdma2','rdma3'])
    return c

class ConfigTests(unittest.TestCase):
    def validate(self, c):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'c.json'; f.write_text(json.dumps(c)); return common.load_config(f)
    def test_valid(self): self.assertEqual(len(self.validate(configured())['nodes']),4)
    def test_placeholder(self):
        with self.assertRaises((ValueError,KeyError)): self.validate(json.loads((BASE/'config/cluster.example.json').read_text()))
    def test_duplicate_rank(self):
        c=configured(); c['nodes'][2]['rank']=1
        with self.assertRaises(ValueError): self.validate(c)
    def test_three_nodes(self):
        c=configured(); c['nodes'].pop()
        with self.assertRaises(ValueError): self.validate(c)
    def test_duplicate_management(self):
        c=configured();c['nodes'][1]['management_ip']=c['nodes'][0]['management_ip']
        with self.assertRaises(ValueError):self.validate(c)
    def test_data_management_same_iface(self):
        c=configured();c['nodes'][0]['prev_iface']='mgmt0'
        with self.assertRaises(ValueError):self.validate(c)
    def test_broken_cable_subnet(self):
        c=configured();c['nodes'][1]['prev_cidr']='192.0.2.23/30'
        with self.assertRaises(ValueError):self.validate(c)
    def test_network_address_not_host(self):
        c=configured();c['nodes'][0]['next_cidr']='192.0.2.13/30'
        with self.assertRaises(ValueError):self.validate(c)
    def test_api_public_bind_rejected(self):
        c=configured();c['api_bind']='0.0.0.0'
        with self.assertRaises(ValueError):self.validate(c)
    def test_duplicate_ports(self):
        c=configured();c['master_port']=c['api_port']
        with self.assertRaises(ValueError):self.validate(c)
    def test_interface_shell_injection(self):
        with self.assertRaises(ValueError):common.iface_name('eth0;reboot')
    def test_path_volume_injection(self):
        with self.assertRaises(ValueError):common.abs_path('/data:/host')

class LaunchTests(unittest.TestCase):
    def test_smoke_has_eager_no_spec(self):
        a=common.model_args('smoke32k',0,'192.0.2.43',8891,29541)
        self.assertIn('--enforce-eager',a);self.assertNotIn('--speculative-config',a)
    def test_head_is_not_headless(self):
        self.assertNotIn('--headless',common.model_args('text256k',0,'x',8891,29541))
    def test_worker_headless(self):
        self.assertIn('--headless',common.model_args('text256k',3,'x',8891,29541))
    def test_graphs_exact_two_sequences(self):self.assertEqual(common.graph_sizes(5,2),[5,6,10,12])
    def test_native_ring_research_keeps_runtime_sequences(self):
        self.assertNotIn('--max-num-seqs',common.model_args('research1m',0,'x',8891,29541))
    def test_nvme_only_opt_in(self):
        self.assertNotIn('--kv-transfer-config',common.model_args('research1m',0,'x',8891,29541))
        self.assertIn('--kv-transfer-config',common.model_args('research1m-nvme',0,'x',8891,29541))
    def test_all_profiles_bounded_gmu(self):self.assertTrue(all(float(s['gmu'])<=.83 for s in common.STAGES.values()))
    def test_mesh_and_socket_separate(self):
        a=ringctl.docker_command(configured(),0,'start','smoke32k')
        self.assertIn('NCCL_NET=Mesh',a);self.assertIn('NCCL_SOCKET_IFNAME==mgmt0',a)
        self.assertNotIn('NCCL_NET=IB',a)
    def test_no_privileged_or_docker_socket(self):
        a=ringctl.docker_command(configured(),0,'start','text256k')
        self.assertNotIn('--privileged',a);self.assertNotIn('/var/run/docker.sock',' '.join(a))
    def test_secrets_not_rendered(self):
        a=ringctl.docker_command(configured(),0,'start','smoke32k')
        self.assertIn('--env-file',a);self.assertFalse(any(x.startswith('VLLM_API_KEY=') for x in a))
    def test_preserve_flashinfer_baked_cache(self):
        self.assertNotIn('/root/.cache/flashinfer',' '.join(ringctl.docker_command(configured(),0,'start','smoke32k')))
    def test_stop_rejects_unowned(self):
        with patch.object(ringctl,'inspect_container',return_value={'Config':{'Labels':{}}}):
            with self.assertRaises(RuntimeError):ringctl.stop_owned('dsv41-ring-tp4')
    def test_key_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key';p.write_text('VLLM_API_KEY='+'a'*64+'\n');p.chmod(0o644)
            with self.assertRaises(RuntimeError):ringctl.check_key(str(p))
            p.chmod(0o600);ringctl.check_key(str(p))

class NetworkPlanTests(unittest.TestCase):
    def test_scoped_only(self):
        c=configured();n=c['nodes'][0]
        states={d:{'mtu':1500,'addr_info':[{'family':'inet','local':'192.0.2.33','prefixlen':30}]} for d in ['rdma0','rdma1']}
        a=ring_net.build_plan(c,n,states);text=json.dumps(a)
        self.assertNotIn('mgmt0',text);self.assertNotIn('netplan',text);self.assertNotIn('route',text)
        self.assertNotIn('flush',text);self.assertIn('9000',text)
    def test_unused_ipv4_removed_not_assigned(self):
        n=configured()['nodes'][0];states={'rdma2':{'addr_info':[{'family':'inet','local':'192.0.2.35','prefixlen':30}]}}
        a=ring_net.build_plan(configured(),n,states)
        self.assertEqual(a,[['ip','address','del','192.0.2.35/30','dev','rdma2']])

class AssetTests(unittest.TestCase):
    def test_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertEqual(assets.file_digest(p,'sha256'),hashlib.sha256(b'abc').hexdigest())
    def test_git_blob_sha1(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertEqual(assets.file_digest(p,'sha1',True),hashlib.sha1(b'blob 3\0abc').hexdigest())
    def test_wrong_existing_file_not_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertFalse(assets.match_file(p,{'size':3,'sha256':'0'*64}))
    def test_reuse_hardlink_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source';q=Path(d)/'new';p.write_bytes(b'old')
            assets.reuse_one(p,q,'hardlink')
            self.assertEqual(p.stat().st_ino,q.stat().st_ino);self.assertEqual(p.read_bytes(),b'old')
    def test_reuse_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source';q=Path(d)/'new';p.write_bytes(b'old');q.write_bytes(b'keep')
            with self.assertRaises(RuntimeError):assets.reuse_one(p,q,'hardlink')
            self.assertEqual(q.read_bytes(),b'keep')
    def test_manifest_path_traversal(self):self.assertFalse(assets.valid_relative('../config.json'))

class MockAPIHandler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_POST(self):
        obj=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.headers.get('Authorization')!='Bearer test-key':
            self.send_response(401);self.end_headers();return
        if self.path=='/tokenize':result={'count':len(obj['prompt'].split())}
        else:result={'choices':[{'message':{'content':'323'}}],'usage':{'prompt_tokens':10,'completion_tokens':1}}
        body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

class APIClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),MockAPIHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def test_authenticated_chat(self):
        r,t=api_probe.chat(self.url,'test-key','17*19');self.assertEqual(api_probe.answer(r),'323');self.assertGreaterEqual(t,0)
    def test_http_failure_surfaces(self):
        with self.assertRaises(RuntimeError):api_probe.chat(self.url,'wrong','hi')
    def test_needle_budget_fits_mock_tokenizer(self):
        text,count=api_probe.make_needle_text(self.url,'test-key',1000,.5,'abc123')
        self.assertLessEqual(count,1000);self.assertGreater(count,950);self.assertEqual(text.count('abc123'),1)
    def test_missing_api_key(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key';p.write_text('NOT_KEY=x')
            with self.assertRaises(ValueError):api_probe.key_from_file(str(p))

if __name__=='__main__':unittest.main()
