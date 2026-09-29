(() => {
  const $ = id => document.getElementById(id);
  const terminal = new Set(['SUCCEEDED', 'FAILED', 'CANCELED', 'INTERRUPTED']);
  const labels = {QUEUED:'等待取得资源',LOADING:'准备数字人服务',SUBMITTING:'提交生成任务',RUNNING:'正在生成视频',COLLECTING:'校验并保存视频',RELEASING:'释放模型资源',SUCCEEDED:'视频已完成',FAILED:'生成未完成',CANCELED:'任务已取消',CANCELING:'正在取消并清理',INTERRUPTED:'需要检查资源清理'};
  let current = null, key = null, polling = false;
  async function api(path, body) {
    const response = await fetch(path, {method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.error==='string'?value.error:'请求失败，请返回 Gateway 检查登录与资源状态');
    return value;
  }
  const b64 = file => new Promise((resolve,reject) => {const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
  function render(job) {
    current=job.job_id; $('status').textContent=labels[job.state]||job.state;
    $('details').textContent=`任务 ${job.job_id} · ${job.memory_released?'资源已释放':'资源尚未释放'}${job.elapsed_seconds?' · 已用 '+Math.round(job.elapsed_seconds/60)+' 分钟':''}${job.error?' · '+job.error:''}`;
    $('cancel').disabled=terminal.has(job.state);$('submit').disabled=!terminal.has(job.state)||job.cleanup_required;
    $('reconcile').hidden=!job.cleanup_required;
    if(job.artifact){if($('result').getAttribute('src')!==job.artifact.url)$('result').src=job.artifact.url;$('result').hidden=false;$('download').href=job.artifact.url;$('download').download=`avatar-${job.job_id}.mp4`;$('download').hidden=false;}
    else {$('result').hidden=true;$('download').hidden=true;}
  }
  async function history() {
    const {jobs}=await api('/api/v1/avatar-jobs');$('history').replaceChildren();
    jobs.slice(0,10).forEach(job=>{const b=document.createElement('button');b.type='button';b.textContent=`${labels[job.state]||job.state} · ${new Date(job.created_at*1000).toLocaleString()} · ${job.duration_seconds} 秒`;b.onclick=()=>{render(job);if(!terminal.has(job.state))poll();};$('history').append(b);});
    return jobs;
  }
  async function poll() {
    if(polling||!current)return;polling=true;
    try{while(current){const {job}=await api('/api/v1/avatar-jobs/'+current);render(job);if(terminal.has(job.state)){key=null;await history();break;}await new Promise(resolve=>setTimeout(resolve,5000));}}
    catch(error){$('status').textContent='状态读取中断';$('details').textContent=error.message+'；刷新可继续查看，不会重复生成。';}
    finally{polling=false;}
  }
  $('avatar-form').onsubmit=async event=>{event.preventDefault();$('submit').disabled=true;
    try{const image=$('image').files[0],audio=$('audio').files[0];if(image.size>8*1024*1024||audio.size>4*1024*1024)throw new Error('图片或语音文件超过大小限制');key=key||Array.from(crypto.getRandomValues(new Uint8Array(16)), b=>b.toString(16).padStart(2,"0")).join("");
      const {job}=await api('/api/v1/avatar-jobs',{image_base64:await b64(image),audio_base64:await b64(audio),prompt:$('prompt').value,num_segments:Number($('segments').value),idempotency_key:key});render(job);poll();
    }catch(error){$('status').textContent=error.message;$('submit').disabled=false;}};
  $('cancel').onclick=async()=>{try{render((await api('/api/v1/avatar-jobs/'+current+'/cancel',{})).job);poll();}catch(error){$('status').textContent=error.message;}};
  $('reconcile').onclick=async()=>{try{render((await api('/api/v1/avatar-jobs/'+current+'/reconcile',{})).job);poll();}catch(error){$('status').textContent=error.message;}};
  history().then(jobs=>{const active=jobs.find(job=>!terminal.has(job.state)||job.cleanup_required);if(active){render(active);poll();}}).catch(error=>{$('status').textContent=error.message;});
})();
