# 受控单机 runner

[run_guarded.py](run_guarded.py) 只负责本次离线容器，主控必须已取得Gateway维护状态及共享GPU锁。它不申请/释放锁、不切模型、不重启Gateway，不执行远端命令。远端head以operator运行，通过`sudo -n docker`操作确切容器。

固定镜像为 `sha256:a8b6aee9435f0a96b3b5aea7b09788e81ff3d675511eadefe3b8759ad6d3c1a1`；显式使用`/opt/longcat-venv/bin/python -m torch.distributed.run`，避免系统torchrun shebang绕过应用venv。source必须已由prepare_runtime生成好新入口及helpers；weights参数指向同时包含LongCat-Video与LongCat-Video-Avatar-1.5的父目录。

```bash
python3 /home/operator/dgx-models/longcat-avatar15/control/runtime/run_guarded.py \
  --source-dir /home/operator/dgx-models/longcat-avatar15/src \
  --weights-dir /home/operator/dgx-models/longcat-avatar15/weights \
  --input-dir /home/operator/dgx-models/longcat-avatar15/input \
  --run-dir /home/operator/dgx-models/longcat-avatar15/runs/NEW_UNIQUE_RUN \
  --num-segments 1
```

具体路径以主控实际部署目录为准；run-dir本身必须不存在，父目录需先准备。默认1段30分钟，5/19段2小时，显式`--timeout-seconds`允许30秒到24小时。只允许1、5、19段；480p、ai2v、INT8、DMD、meta loader、单进程固定。

2026-09-09实际首段耗时9分4秒，5段耗时38分40秒，19段耗时2小时14分10秒，续段速度存在波动。19段分钟级验收显式使用`--timeout-seconds 14400`并已正常结束；重跑分钟级任务时应明确设置足够的有界预算，默认2小时低于这次真实完成时间。完整实测与当前状态见安装记录（历史内部记录，公开版不包含）。

| 防护 / 证据 | 行为 |
|---|---|
| 镜像与隔离 | 固定image ID，单GPU0，network none，容器根只读，source/weights/input三个挂载只读，显式static单进程loopback rendezvous（127.0.0.1:29500） |
| 新写入 | 独立新run-dir内output/logs/cache；不覆盖已有目录；临时/tmp和shm有界 |
| 内存 | Docker memory=100GiB，memory-swap合计=100GiB，故无额外swap；实查HostConfig并每次核对cgroup memory.max=100GiB、memory.swap.max=0 |
| 运行监测 | 每5秒MemAvailable、cgroup memory.current/peak/events写`logs/memory.jsonl`；低于20GiB连续三次或总超时触发精确docker stop，20秒grace由docker随后杀死本容器 |
| 容器归属 | 随机UUID名、运行归属label，每次停止/删除前重新核对，禁止停止其他容器 |
| finally | 停止本容器；记录exit/OOM；导出`logs/model.log`；日志导出失败也独立尝试删除已停止的本容器；再次计算输入SHA并保存`logs/run-report.json` |
| 峰值解释 | `cgroup_peak_bytes_observed`是5秒采样读到的memory.peak最高值，退出前最后短窗口可能漏采；不是GB10全部GPU驱动分配的保证总峰值。全机MemAvailable提供另一个视角 |
| 结果解释 | exit0且清理/输入完整等通过时只写`PROCESS_COMPLETED_MEDIA_NOT_VERIFIED`；须再做ffprobe、完整解码、画质/口型实看。无假媒体验收 |

运行时模型日志保存在Docker日志中，可由主控使用run名精确只读查看；runner在finally导出到本次model.log，不向其他服务写日志。

本地7项mock/fixture测试已PASS：三次低内存及恢复计数/截止时间、固定镜像与venv解释器/只读挂载/内存参数、精确label停容器、cgroup字段/事件解析、已有输出拒绝/输入哈希及symlink门、日志超时仍清理、实效配置不匹配拒绝。Python语法检查PASS。本地mock不作为内核限额、取消或生成实测。独立代码审查已反馈并修复日志异常跳过清理问题；后续主控已完成真实1段和5段模型生成、完整解码、只读输入核验及精确清理，证据见安装记录。主观口型同步仍未完成连续试听。

2026-09-09主控补充：真实CPU容器cgroup限额/计数读取及精确stop/rm已PASS，见 `plans/longcat-avatar15/evidence/install/cgroup-lifecycle-probe.json`。该测试有意停止sleep进程，exit137、OOMKilled=false；未加载模型，不能代替推理或GPU峰值验证。

## I03 首轮 rendezvous 失败后的修正

真实首轮在模型加载之前，由`--standalone`路径派生的TCPStore地址使用容器hostname，network none下地址不可解析，反复出现c10d gai -3。主控精确停止首轮并保留证据；这一失败不算模型加载或画质测试。

runner已移除`--standalone`，固定传入：

```text
--rdzv_backend=static --master_addr=127.0.0.1 --master_port=29500 --node_rank=0 --nnodes=1 --nproc_per_node=1
```

PyTorch 2.10上游`get_rdzv_endpoint`在static且没有显式rdzv_endpoint时使用master_addr:master_port；static handler把该地址/端口直接传给TCPStore并提供给worker。使用数值loopback地址避免容器hostname解析；保留network none，未改宿主DNS/网络。各容器独立网络命名空间使29500在本次容器内隔离。`--standalone`会重设backend为c10d，不能与这个static配置一起保留。

核查来源：[PyTorch v2.10 run.py](https://github.com/pytorch/pytorch/blob/v2.10.0/torch/distributed/run.py)、[static TCP rendezvous](https://github.com/pytorch/pytorch/blob/v2.10.0/torch/distributed/elastic/rendezvous/static_tcp_rendezvous.py)。这是版本相近的上游语义核查。随后主控已在最终镜像、真实离线非root容器中，以相同static参数和`--no_python /bin/echo STATIC_RDZV_CHILD_PASS`完成CPU启动探针，exit=0并打印PASS；实际回执（历史内部记录，公开版不包含）已读取核对。该探针验证了launcher无需hostname DNS即可派生子进程，不覆盖NCCL init、GPU和模型加载。

本地mock已明确断言不存在standalone、存在全部6项static参数且仍固定venv解释器；7项测试PASS。后续完整模型的真实1段和5段运行均使用这一static配置并正常结束。

## 单节点 NCCL 外部插件修正

主控在同一最终镜像完成新的真实GPU单rank探针：保留NCCL 2.28.8、static loopback、`NCCL_IB_DISABLE=1`，增加`NCCL_NET_PLUGIN=none`后，cuda_set_device、init_process_group、all_reduce和destroy均成功，日志显示内置Socket与nranks=1。证据：static-rank-probe3.log（历史内部记录，公开版不包含）。此前原配置出现native double-free；现结果证明外部网络插件禁用的配置可运行该短探针，不等于完整模型通过或已完成native根因定位。

runner据此新增`NCCL_NET_PLUGIN=none`，仍为PyTorch NCCL backend，没有切换为Gloo；另外新增Docker`--ulimit core=0:0`避免native崩溃产生巨大core文件，其余资源限制不变。mock已断言插件变量及core上限。

**此处network none、loopback、IB_DISABLE和禁外部网络插件是专用单节点配置，不得复制用于双机RoCE。** 双机必须恢复按playbook验证的fabric及其独立NCCL配置，重新进行该运行时通信验收。最终runner真实I03重跑与后续5段均已成功；19段状态以安装记录和执行日志为准。
