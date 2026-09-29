#!/usr/bin/env bash
# 仅供 Gateway 已取得相应节点锁后调用；不删除 NFS、卷、镜像或权重。
set -euo pipefail
ENV_FILE="${1:?用法: safe_stop_ablit.sh /绝对路径/.env 或 .env.tp4}"
[[ "$ENV_FILE" = /* && -f "$ENV_FILE" ]] || { echo '需要可信配置文件的绝对路径' >&2; exit 1; }
set -a
source "$ENV_FILE"
set +a
: "${HEAD_CTN:?}" "${WORKER_CTN:?}" "${WORKER_IPS:?}" "${WORKER_USER:?}" "${SSH_IDENTITY:?}" "${IMAGE:?}"
[[ "$HEAD_CTN" == ablit-dsv41-head && "$WORKER_CTN" == ablit-dsv41-worker ]] || {
  echo '容器名称与本迁移方案不一致，拒绝停止' >&2; exit 1;
}
expected="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
[[ "$expected" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
stop_exact() {
  local name="$1" expected_id="$2" actual names
  # 显式检查每次 Docker 返回值；函数在 OR 条件内调用时不能依赖 set -e。
  names="$(docker container ls -a --format '{{.Names}}')" || {
    echo "Docker 不可用，不能判定 $name 已停止" >&2; return 1;
  }
  if ! grep -Fxq -- "$name" <<< "$names"; then
    echo "$name: absent"
    return 0
  fi
  actual="$(docker container inspect -f '{{.Image}}' "$name")" || return 1
  [[ "$actual" == "$expected_id" ]] || { echo "$name 的镜像不匹配；拒绝操作" >&2; return 1; }
  docker container stop --time 60 "$name" >/dev/null || return 1
  docker container rm "$name" >/dev/null || return 1
  echo "$name: stopped and removed"
}
# 先停止 head，拒绝接收新请求，再依次停止所有指定 worker。
# 任一节点失败返回非零，Gateway 必须保持 FAILED/UNKNOWN，不得标为 STOPPED。
failed=0
stop_exact "$HEAD_CTN" "$expected" || failed=1
read -r -a peers <<< "${WORKER_IPS//,/ }"
for peer in "${peers[@]}"; do
  [[ "$peer" =~ ^[a-zA-Z0-9_.:-]+$ ]] || { echo '无效 worker 地址' >&2; exit 1; }
  [[ "$WORKER_USER" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 1
  {
    printf 'set -euo pipefail\n'
    declare -f stop_exact
    printf 'stop_exact %q %q\n' "$WORKER_CTN" "$expected"
  } | ssh -i "$SSH_IDENTITY" -o BatchMode=yes -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes -o ConnectTimeout=15 \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      "${WORKER_USER}@${peer}" bash -s || failed=1
done
exit "$failed"
