# MiniMax H3 四Spark Ring4 四Profile工具包 V2

先阅读 `PLAN_V2_CN.md`，再看 `CHANGELOG.md`。原两份文档位于 `reference/`，作为安装与排障参考。

**不要覆盖旧kit、已工作的模型和网络。不要直接运行example中的占位值。**

这是本地执行用的脚本和计划，不是已经在你的四台GPU上完成的部署。四个Profile应按官方FL2VA→官方Ref2VA→Heretic→Hybrid实验顺序独立验收。

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

`BUILD_REPORT.md`说明本次28项本地测试与未测试硬件边界。新下载脚本需要huggingface_hub；生成环境与系统依赖见原安装说明。队列的媒体检查需要ffmpeg/ffprobe。

包内 `PLAN_V2_CN.md` 是主文档正文。另附可下载的完整单文件Markdown包含全部文件区块，可以在没有ZIP时还原本工具包。不要把主文档里的示例主机名、路径和占位模型参数当作已核验的现场值。
