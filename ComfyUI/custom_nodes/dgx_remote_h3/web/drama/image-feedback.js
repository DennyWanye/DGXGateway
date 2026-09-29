export function imageJobFeedback(job) {
  const error = typeof job.error === "string" ? job.error : job.error?.message;
  const count = job.candidates?.length || 0;
  if (job.state === "SUCCEEDED") return { tone: "success", text: `图片制作完成 · ${count} 张候选。点击“查看任务与图片”，检查后选为参考草稿。` };
  if (["FAILED", "INTERRUPTED", "CANCELED"].includes(job.state)) return { tone: "error", text: `图片任务${{ FAILED: "失败", INTERRUPTED: "中断", CANCELED: "已取消" }[job.state]}。${error || "请查看任务详情。"} 表单内容仍保留。` };
  if (job.state === "SUBMISSION_UNKNOWN") return { tone: "error", text: "任务提交结果不明。请核查原任务，不要重复生成。" };
  const phase = { PREPARED: "等待派发", DISPATCHING: "正在派发", QUEUED: "等待执行", SUBMITTED: "已提交", RUNNING: "正在制作", COLLECTING: "正在归档", VALIDATING: "正在校验图片" }[job.state] || "正在查询状态";
  return { tone: "pending", text: `任务已接收 · ${phase}。页面每 5 秒更新；无需再次点击提交。` };
}

// Submission acknowledgement is separate from completion of image generation.
// An uncertain response must never trigger a second automatic POST.
export function createImageSubmissionFeedback({ send, show, accepted, delay = setTimeout, cancelDelay = clearTimeout }) {
  let blocked = false;
  return async function submit(payload) {
    if (blocked) return;
    blocked = true;
    show({ tone: "pending", text: "正在提交图片任务…正在等待服务确认，请勿重复点击。", submitting: true });
    const timer = delay(() => show({ tone: "pending", text: "仍在等待提交回执，任务可能已经创建。请保持页面，勿重复提交。", submitting: true }), 15000);
    let result;
    try {
      result = await send(payload);
      if (!result.job?.id) throw new Error("提交回执缺少任务编号");
    } catch (error) {
      const uncertain = !error.status;
      blocked = uncertain;
      show({ tone: "error", text: uncertain
        ? `提交结果尚未确认：${error.message}。请先核查项目原任务，勿重新提交；填写内容保留。`
        : `未能提交：${error.message}。请修正后再提交；填写内容保留。`, submitting: false, uncertain });
      return;
    } finally { cancelDelay(timer); }
    blocked = false;
    show({ ...imageJobFeedback(result.job), submitting: false, job: result.job });
    // Display failures after an acknowledged POST must not imply submission failed.
    accepted(result.job);
  };
}
