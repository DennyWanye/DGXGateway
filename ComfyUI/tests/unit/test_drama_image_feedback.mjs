import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createImageSubmissionFeedback, imageJobFeedback } from '../../custom_nodes/dgx_remote_h3/web/drama/image-feedback.js';

function setup(send) {
  const messages = [], jobs = []; let slow;
  const submit = createImageSubmissionFeedback({ send, show: value => messages.push(value), accepted: job => jobs.push(job), delay: fn => { slow = fn; return 1; }, cancelDelay: () => { slow = null; } });
  return { submit, messages, jobs, slow: () => slow?.() };
}
test('pending feedback is immediate; slow receipt and double click cause only one POST', async () => {
  let resolve, calls = 0;
  const h = setup(() => { calls++; return new Promise(r => { resolve = r; }); });
  const waiting = h.submit({ prompt: 'keep me' });
  assert.equal(h.messages[0].submitting, true); h.slow();
  assert.match(h.messages.at(-1).text, /仍在等待/);
  await h.submit({ prompt: 'duplicate' }); assert.equal(calls, 1);
  resolve({job:{id:'one',state:'RUNNING'}}); await waiting;
  assert.equal(h.jobs.length,1); assert.equal(h.messages.at(-1).submitting,false);
  assert.match(h.messages.at(-1).text,/任务已接收.*正在制作/);
});
test('HTTP validation error restores action and preserves caller payload', async () => {
  let calls = 0; const payload = { prompt: 'keep me' };
  const h = setup(async () => { calls++; throw Object.assign(new Error('输入不合格'),{status:400}); });
  await h.submit(payload); assert.equal(h.messages.at(-1).uncertain,false);
  assert.match(h.messages.at(-1).text,/未能提交.*输入不合格/);
  assert.deepEqual(payload,{prompt:'keep me'});
  await h.submit(payload); assert.equal(calls,2);
});
test('lost acknowledgement blocks a new POST and explains uncertainty', async () => {
  let calls = 0; const h = setup(async () => { calls++; throw new TypeError('connection lost'); });
  await h.submit({}); await h.submit({}); assert.equal(calls,1);
  assert.equal(h.messages.at(-1).uncertain,true); assert.match(h.messages.at(-1).text,/核查项目原任务/);
});
test('malformed success receipt is unknown, not an invitation to resubmit', async () => {
  const h = setup(async () => ({})); await h.submit({});
  assert.equal(h.messages.at(-1).uncertain,true); assert.equal(h.jobs.length,0);
});
test('job lifecycle separates completion, queueing, failures and uncertainty', () => {
  assert.match(imageJobFeedback({state:'SUCCEEDED',candidates:[{}]}).text,/完成 · 1 张候选/);
  assert.match(imageJobFeedback({state:'QUEUED'}).text,/等待执行/);
  assert.match(imageJobFeedback({state:'FAILED',error:{message:'显存不足'}}).text,/显存不足/);
  assert.equal(imageJobFeedback({state:'SUBMISSION_UNKNOWN'}).tone,'error');
});
