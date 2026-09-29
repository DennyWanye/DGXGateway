import base64,json,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from DGXGateway.dgx_gateway.image_workflows import qwen_image_edit
from DGXGateway.dgx_gateway.creative_jobs import CreativeJobs
from DGXGateway.dgx_gateway.mcp import _validate,TOOLS

class EditWorkflowTests(unittest.TestCase):
 def test_references_reach_positive_and_negative_encoders(self):
  g=qwen_image_edit('Preserve face, change coat',['a.png','b.png','c.png'])
  for key in ('image1','image2','image3'):
   self.assertEqual(g['7']['inputs'][key],g['8']['inputs'][key])
  self.assertEqual(g['11']['inputs']['pixels'],g['7']['inputs']['image1'])
  self.assertEqual(g['12']['inputs']['steps'],40)
 def test_reject_non_string_asset_or_too_many_references(self):
  spec=next(t['inputSchema'] for t in TOOLS if t['name']=='edit_image')
  for ids in [[],['a']*9,[1]]:
   with self.assertRaises(ValueError):_validate(spec,{'prompt':'edit','asset_ids':ids,'idempotency_key':'test-key'})
  from DGXGateway.dgx_gateway.image_workflows import validate_edit_parameters
  with self.assertRaises(ValueError):
   validate_edit_parameters({'prompt':'edit','asset_ids':['a']*4})
 def test_upload_failure_never_submits_generation_and_releases(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=SimpleNamespace(config=SimpleNamespace(state_dir=Path(tmp),nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188),_submit_lock=threading.Lock())
   jobs=CreativeJobs(app);d={'job_id':'a'*32,'input_assets':['b'*64],'workflow':{},'memory_released':False}
   jobs.active=d['job_id'];jobs.switch=Mock();jobs.request=Mock(return_value={});jobs.upload_assets=Mock(side_effect=TimeoutError('upload response lost'))
   jobs.run(d)
   self.assertEqual(d['state'],'FAILED');self.assertTrue(d['memory_released']);self.assertIsNone(jobs.active)
   self.assertEqual([call.args[0] for call in jobs.request.call_args_list],['/queue','/queue'])
   self.assertEqual(jobs.switch.call_count,2)
 def test_upload_requires_matching_content_addressed_name(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=SimpleNamespace(config=SimpleNamespace(state_dir=Path(tmp),nodes=[SimpleNamespace(address='127.0.0.1')],music3_port=8188))
   jobs=CreativeJobs(app);a=jobs.assets.register_base64('x.png',base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode())
   response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read=Mock(return_value=json.dumps({'name':'renamed.png','subfolder':'','type':'input'}).encode())
   jobs.http=Mock();jobs.http.open.return_value=response
   with self.assertRaises(RuntimeError):jobs.upload_assets({'input_assets':[a['asset_id']]})
   request=jobs.http.open.call_args.args[0]
   self.assertIn(a['comfy_filename'].encode(),request.data)
   self.assertIn(b'\x89PNG\r\n\x1a\nfixture',request.data)

if __name__=='__main__':unittest.main()
