# 本地媒体验收与审阅图

`inspect_media.py`使用机器已有ffprobe/ffmpeg，不安装软件，不操作远端或模型。它检查生成媒体的结构和音轨，仅输出`TECHNICAL_PASS`或`TECHNICAL_FAIL`；画质与嘴形状态始终是`NOT_REVIEWED`。

```bash
python3 scripts/longcat-avatar15/inspect_media.py \
  --video /absolute/path/video_continue_19.mp4 \
  --segments 19 \
  --reference-audio /absolute/path/speech.wav \
  --output-dir /absolute/path/NEW_MEDIA_AUDIT
```

输出目录必须不存在，其父目录应已存在。segments支持1..19，便于对途中累计输出验收；例如`video_continue_2.mp4`必须传2，不能按所属任务的目标5传5。

| 检查 / 产物 | 规则 |
|---|---|
| report.json | 输入文件SHA、实际25fps、完整解码帧数、分辨率、视频/音频codec、起始PTS/timebase/秒数、时长和检查结果 |
| 结构门 | 帧数必须`93+80*(segments-1)`；视频时长误差≤0.04秒，视频/音频时长和起始差≤0.1秒；逐帧实际PTS间隔0.04秒 |
| 完整解码 | ffmpeg整段解码exit=0且error日志为空；原始ffprobe与stderr回执保留 |
| 音轨比较 | 输出音轨及reference等长前缀均解码16kHz单声道PCM16，计算零lag Pearson相关；不搜索最优延迟；报告实际样本数/RMS。相关度只作信息，不作为嘴形同步门 |
| 边界帧 | 第1续段从零基帧93开始，以后每80帧；每行取前2/后2真实帧，即91、92、93、94等；保留原分辨率PNG |
| review-sheet-*.png | 每图最多6个边界行、4列，缩放到每帧宽320像素；不足6行时按实际行数生成，无占位黑格；单段额外生成4帧总览并明确标注overview |
| review-frames.json/csv | 每张原帧的零基frame_index、实际解码timestamp、相对时间、边界编号、sheet行列及SHA；审阅图不烧入编号，按清单对照 |
| 不变性 | 结束核对视频与reference音频SHA；从不修改输入视频/音频 |

1/5/19段预期分别93/413/1533帧与3.72/16.52/61.32秒。分辨率按实际生成值报告，不把`480p`bucket参数硬解释为832×480；当前真实样本是768×512。

## 实际工具验证记录

| 已有真实媒体 | 本轮工具结果 | 回执 |
|---|---|---|
| I03单段ai2v_demo_1.mp4 | TECHNICAL_PASS；93帧、3.72秒、25fps、768×512；音视频起始0；完整decode exit0；零lag音轨相关0.99992979 | seg1 report（历史内部记录，公开版不包含） |
| I04五段任务的累计第二段 | TECHNICAL_PASS；173帧、6.92秒、25fps、768×512；音视频起始0；完整decode exit0；零lag音轨相关0.99988087 | seg2 report（历史内部记录，公开版不包含） |
| 既有输出目录再调用 | 拒绝覆盖、全部已有审计制品hash不变 | 本地命令断言PASS |

审阅图1×4布局已查看，确认无占位帧；没有据此作完整视频画质或嘴形验收。这是对已有真实生成样本的本地工具验证，不能计为新增模型生成覆盖。19段多页及完整5段仍待实际媒体到位后按同工具执行；未制造假视频fixtures。
