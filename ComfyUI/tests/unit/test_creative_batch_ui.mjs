import test from 'node:test';
import assert from 'node:assert/strict';
import { appendReference, batchSummary, candidateCaption, parseCreativeParameter, creativeImageURL } from '../../custom_nodes/dgx_remote_h3/web/creative-batch.js';
test('more than three references is explicit and never truncates previous identity',()=>{
 const before=['a','b','c']; assert.throws(()=>appendReference(before,'d'),/原参考图已保留/);assert.deepEqual(before,['a','b','c']);assert.deepEqual(appendReference(before,'b'),before);
});
test('batch information distinguishes limit from observed parallelism',()=>{
 const value=batchSummary({image_batch:{candidateCount:4,parallelLimit:2,candidates:[{state:'SUCCEEDED'},{state:'FAILED'}]}});
 assert.match(value,/共 4 张/);assert.match(value,/实际同时生成 尚未记录/);assert.match(value,/已成功 1 · 失败 1/);
 assert.match(batchSummary({}),/未记录候选并行信息/);
});
test('candidate label uses explicit identity and never invents historical seed',()=>{
 assert.equal(candidateCaption({candidate_index:2,seed:44}),'候选 3 · seed 44');assert.match(candidateCaption({node:'42'}),/未记录逐张seed/);
});

test('large candidate seeds render exact server decimal identity',()=>{
 assert.equal(candidateCaption({candidate_index:1,seed:9007199254740992,seedText:'9007199254740993'}),'候选 2 · seed 9007199254740993');
});

test('generic Comfy tool form refuses silent integer rounding including raw graph JSON',()=>{
 assert.throws(()=>parseCreativeParameter('integer','9007199254740993'),/精确/);
 assert.throws(()=>parseCreativeParameter('object','{"seed":9007199254740993}'),/精确/);
 assert.throws(()=>parseCreativeParameter('integer','2.5'),/整数/);
 assert.deepEqual(parseCreativeParameter('object','{"cfg":4.5,"seed":42}'),{cfg:4.5,seed:42});
});

test('image preview uses a same-origin owned-artifact path without weakening CSP',()=>{
 const jid='a'.repeat(32), artifact={kind:'images',url:`http://192.0.2.37:18082/api/v1/artifacts/${jid}/001-test.png`};
 assert.equal(creativeImageURL(artifact,jid),`/dgx-creative/artifacts/${jid}/001-test.png`);
 assert.equal(creativeImageURL(artifact,'b'.repeat(32)),null);
 for (const filename of ['..','%2fsecret','%5csecret','%00x','a.png?url=http://other']) assert.equal(creativeImageURL({...artifact,url:`http://192.0.2.37:18082/api/v1/artifacts/${jid}/${filename}`},jid),null);
 assert.equal(creativeImageURL({...artifact,kind:'videos'},jid),null);
});
