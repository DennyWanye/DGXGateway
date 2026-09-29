"""CPU-only V2 tests. These do not validate GB10 kernels, H3, NCCL or four physical hosts."""
from __future__ import annotations
import copy,hashlib,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

KIT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(KIT/'scripts'))
import assets_v2 as av
import check_encoder as ce
import profile_node as pn
import compare_nodes as cn
import request_v2 as rv
import queue_v2 as qv


def request():
    return {'task':'t2va','prompt':'An adult walks calmly.','conditions':[],
            'target':{'duration_seconds':4,'short_edge':768,'aspect_ratio':'16:9'},
            'num_inference_steps':50,'num_outputs_per_prompt':1,'seed':17,'quality':'lossless'}


class V2Tests(unittest.TestCase):
    def test_all_python_syntax(self):
        for path in (KIT/'scripts').glob('*.py'):compile(path.read_text(),str(path),'exec')

    def test_all_shell_syntax(self):
        for path in (KIT/'scripts').glob('*.sh'):subprocess.run(['bash','-n',str(path)],check=True)

    def build(self,pid):
        c=json.loads((KIT/'config/cluster.example.json').read_text());c.update(image_id='sha256:test',master_addr='192.0.2.2')
        n={'rank':0,'control_iface':'eth0','network_mode':'socket','hcas':''}
        p=json.loads((KIT/'profiles'/f'{pid}.json').read_text())
        return pn.build(c,n,p,{},4,'test1',Path('/tmp/logs'))

    def test_four_profiles_validate(self):
        for name in pn.IDS:pn.validate_profile(json.loads((KIT/'profiles'/f'{name}.json').read_text()))

    def test_ring4_command_is_not_tp4(self):
        cmd,r=self.build('ref2va-official')
        self.assertEqual(cmd[cmd.index('--tp-size')+1],'1')
        self.assertEqual(cmd[cmd.index('--ring-degree')+1],'4')
        self.assertEqual(cmd[cmd.index('--nnodes')+1],'4')
        self.assertIn('NCCL_NET=Socket',cmd)
        self.assertNotIn('--privileged',cmd)
        self.assertEqual(cmd[cmd.index('--memory')+1],'104g')
        self.assertEqual(cmd[cmd.index('--memory-swap')+1],'104g')

    def test_model_path_is_repo_root_not_partition(self):
        # SGLang MiniMaxH3Pipeline sets model_subfolder from --model-variant
        # (fl2va→FL2VA, ref2va→Ref2VA). Passing the partition as model-path
        # resolves to /models/h3/FL2VA/FL2VA and the process exits.
        for pid in ('fl2va-official', 'ref2va-official', 'hybrid-experimental'):
            cmd=self.build(pid)[0]
            self.assertEqual(cmd[cmd.index('--model-path')+1],'/models/h3')
            self.assertEqual(cmd[cmd.index('--model-variant')+1], json.loads((KIT/'profiles'/f'{pid}.json').read_text())['model_variant'])

    def test_serve_overrides_root_modular_index(self):
        # Official MiniMax-H3 root model_index.json is MiniMaxH3ModularPipeline
        # with 3-element component specs. SGLang verify_model_config_and_directory
        # then requires transformer/ and vae/ at repo root and exits before
        # joining FL2VA/. The container must see a MiniMaxH3Pipeline probe index
        # whose 2-tuple component is the existing FL2VA directory.
        stub=json.loads((KIT/'config/serve-root-model-index.json').read_text())
        self.assertEqual(stub['_class_name'], 'MiniMaxH3Pipeline')
        self.assertEqual(stub['FL2VA'], ['diffusers', 'MiniMaxH3Pipeline'])
        for pid in ('fl2va-official', 'ref2va-official', 'hybrid-experimental'):
            cmd=self.build(pid)[0]
            self.assertEqual(cmd[cmd.index('--pipeline-class-name')+1], 'MiniMaxH3Pipeline')
            self.assertTrue(any(
                x.startswith('type=bind,src=') and x.endswith('dst=/models/h3/model_index.json,readonly') and 'serve-root-model-index.json' in x
                for x in cmd
            ))

    def test_fl2va_preserves_current_memory_profile(self):
        cmd=self.build('fl2va-official')[0]
        self.assertEqual(cmd[cmd.index('--num-gpus')+1],'4')
        self.assertEqual(cmd[cmd.index('--performance-mode')+1],'memory')

    def test_ref2va_optional_overlay_is_verified_mounted_and_fingerprinted(self):
        cmd, report = self.build('ref2va-official')
        overlay = report['identity']['ref2va_optional_inputs']
        self.assertEqual(overlay['contract'], 'ref2va-optional-inputs-v1')
        for rel in overlay['files']:
            self.assertTrue(any(f'/ref2va-optional/{rel},dst=' in arg and arg.endswith(',readonly') for arg in cmd))
        self.assertIsNone(self.build('fl2va-official')[1]['identity']['ref2va_optional_inputs'])

    def test_heretic_only_overrides_encoder(self):
        cmd,r=self.build('ref2va-heretic')
        self.assertIn('--component-paths.text_encoder',cmd)
        self.assertNotIn('--component-weights-paths.transformer',cmd)
        self.assertTrue(any('dst=/models/heretic,readonly' in x for x in cmd))
        self.assertTrue(any('dst=/data/h3,readonly' in x for x in cmd))

    def test_hybrid_has_no_online_quantization(self):
        cmd,r=self.build('hybrid-experimental')
        self.assertIn('--component-weights-paths.transformer',cmd)
        self.assertNotIn('--quantization',cmd)
        self.assertNotIn('--component-paths.text_encoder',cmd)

    def test_hybrid_requantization_is_rejected(self):
        p=json.loads((KIT/'profiles/hybrid-experimental.json').read_text());p['online_quantization']='fp8'
        with self.assertRaises(ValueError):pn.validate_profile(p)

    def test_fingerprint_changes_with_profile(self):
        self.assertNotEqual(self.build('ref2va-official')[1]['fingerprint'],self.build('ref2va-heretic')[1]['fingerprint'])

    def test_match_four_nodes(self):
        rows=[]
        for n in range(4):
            r=copy.deepcopy(self.build('ref2va-official')[1]);r.update(rank=n,full_hash_verified_now=True);rows.append(r)
        self.assertEqual(cn.compare(rows)['status'],'PASS_OFFLINE_MATCH_ONLY')
        rows[3]['fingerprint']='wrong'
        with self.assertRaises(ValueError):cn.compare(rows)

    def test_receipt_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'w';p.write_bytes(b'abcd')
            receipt={'files':[{'path':'w','bytes':4,'sha256':av.sha256(p)}]}
            av.verify_receipt(Path(d),receipt)
            p.write_bytes(b'abce')
            with self.assertRaises(RuntimeError):av.verify_receipt(Path(d),receipt)

    def test_hash_git_object(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x';p.write_bytes(b'abc')
            self.assertEqual(av.remote_hash(p,'git-sha1'),hashlib.sha1(b'blob 3\0abc').hexdigest())

    def test_paths_refuse_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            for uri in ('file:///data/h3/../secret','file:///etc/passwd','http://example.org/x','file://host/data/h3/x'):
                with self.assertRaises(ValueError):rv.asset_path(uri,Path(d))
        with self.assertRaises(ValueError):av.safe_path(Path('/tmp'),'../x')

    def test_task_profile_and_ref_empty(self):
        rv.validate(request(),'fl2va-official')
        with self.assertRaises(ValueError):rv.validate(request(),'ref2va-official')
        r=request();r['task']='ref2va'
        with self.assertRaises(ValueError):rv.validate(r,'ref2va-official')

    def test_ropes_normalize_old_and_new_configs(self):
        a={'model_type':'qwen3_vl','text_config':{'rope_theta':5000000,'rope_scaling':{'rope_type':'default','mrope_section':[24,20,20],'mrope_interleaved':True}},'vision_config':{}}
        b=copy.deepcopy(a);b['text_config']['rope_parameters']=dict(b['text_config'].pop('rope_scaling'),rope_theta=b['text_config'].pop('rope_theta'))
        self.assertEqual(ce.normalized(a),ce.normalized(b))

    def init_job(self,root,payload=None):
        db=qv.connect(root);r=payload or request();now=1.0
        qv.setmeta(db,'paused',False)
        qv.setmeta(db,'active',{'profile_id':'fl2va-official','fingerprint':'fp','deployment_id':'d1'})
        db.execute('INSERT INTO jobs(id,profile,request,request_hash,inputs,status,created,updated) VALUES(?,?,?,?,?,?,?,?)',('j1','fl2va-official',json.dumps(r),'hash','{}','queued',now,now));db.commit()
        return db

    def test_ambiguous_post_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=self.init_job(root)
            with patch.object(qv,'api_json',side_effect=TimeoutError('response lost')) as call:
                with self.assertRaises(RuntimeError):qv.work(db,root,root,'http://127.0.0.1:9',1)
                self.assertEqual(call.call_count,1)
                self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'unknown')
                with self.assertRaises(RuntimeError):qv.work(db,root,root,'http://127.0.0.1:9',1)
                self.assertEqual(call.call_count,1)
            db.close()

    def test_known_job_resume_never_posts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=self.init_job(root)
            qv.state(db,'j1','running',backend_id='known',fingerprint='fp',deployment_id='d1')
            with patch.object(qv,'api_json',return_value={'status':'failed'}) as call:
                qv.work(db,root,root,'http://127.0.0.1:9',1)
                self.assertEqual(call.call_args.args,('http://127.0.0.1:9/v1/videos/known',))
            db.close()

    def test_pause_prevents_post(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=self.init_job(root);qv.setmeta(db,'paused',True);db.commit()
            with patch.object(qv,'api_json') as call:qv.work(db,root,root,'http://127.0.0.1:9',1);call.assert_not_called()
            db.close()

    def test_worker_lock_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            with qv.worker_lock(Path(d)):
                with self.assertRaises(RuntimeError):
                    with qv.worker_lock(Path(d)):pass

    def test_input_changes_block_generation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);f=root/'a.jpg';f.write_bytes(b'first')
            r=request();r.update(task='fl2va',conditions=[{'type':'image','role':'keyframe','frame_index':0,'uri':'file:///data/h3/a.jpg'}])
            db=self.init_job(root,r);inputs=qv.input_manifest(r,root)
            db.execute('UPDATE jobs SET inputs=?',(json.dumps(inputs),));db.commit();f.write_bytes(b'changed')
            with patch.object(qv,'api_json') as call:qv.work(db,root,root,'http://127.0.0.1:9',1);call.assert_not_called()
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'failed');db.close()

    def test_media_contract_with_synthetic_video(self):
        # Actual CPU ffmpeg fixture, not model output.
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'synthetic.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=128x128:rate=24',
                            '-f','lavfi','-i','anullsrc=r=32000:cl=stereo','-t','1','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True)
            info=qv.media_check(path,{'duration_seconds':1,'short_edge':128})
            self.assertTrue(info['technical_decode_verified'])
            self.assertEqual(info['human_quality_review'],'pending')

if __name__=='__main__':unittest.main()
