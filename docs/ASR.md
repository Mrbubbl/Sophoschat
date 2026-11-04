# 服务器本地语音识别

SenseVoiceSmall int8 ONNX + sherpa-onnx 在 CPU 执行，自动识别普通话、粤语、英文、日语和韩语。无须 GPU、PyTorch、Claude Key 或外部语音 API；首次安装下载模型，推理不联网下载。

## 安装

使用运行应用的同一个 Python 虚拟环境：

```bash
pip install -e '.[asr]'
# Debian / Ubuntu；macOS 可用 brew install ffmpeg
sudo apt-get install ffmpeg
python -m aichat.asr_setup --model-dir /var/lib/aichat/models/sensevoice-small-int8
```

模型目录须可被应用服务账号读取。默认路径为 `$AICHAT_DATA_DIR/models/sensevoice-small-int8`；本地开发为 `data/models/sensevoice-small-int8`。切换模型文件或 Python 环境后重启应用。

使用 Debian/Ubuntu 的 `deploy/install.sh` 时，可加 `INSTALL_LOCAL_ASR=1`，同时安装 ASR 依赖和下载模型；脚本已将 FFmpeg 加入系统依赖。未加该参数时，仍可按本页单独安装。

安装器下载 [sherpa-onnx 官方 int8 发布包](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)，约 155 MiB，模型约 228 MiB。校验项目固定 SHA-256，仅复制白名单文件，保留 LICENSE、来源和校验清单；校验失败停止。具体模型许可随下载包及上游说明保留，不更改本仓库原有许可证状态。

服务器无法访问下载站时，先在其他机器下载同一个官方包再上传：

```bash
python -m aichat.asr_setup --archive /path/model.tar.bz2 --model-dir /var/lib/aichat/models/sensevoice-small-int8
```

缺少依赖或权重时普通聊天仍可用，转写明确提示安装，不会静默调用其他服务。

## 配置

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `AICHAT_ASR_ENABLED` | `1` | 本地转写开关 |
| `AICHAT_ASR_AUTO_AGENT` | `1` | 含 Agent 会话的新语音和互动中引用语音自动转写，关闭后仍可手动请求 |
| `AICHAT_ASR_MODEL_DIR` | 数据目录下 `models/sensevoice-small-int8` | 包含 `model.int8.onnx` 和 `tokens.txt` |
| `AICHAT_ASR_FFMPEG` | `ffmpeg` | 可指定绝对路径 |
| `AICHAT_ASR_THREADS` | `2` | CPU 线程数，1–8 |
| `AICHAT_ASR_TIMEOUT` | `180` | 单条等待秒数，10–600，超时终止模型子进程 |
| `AICHAT_ASR_MAX_SECONDS` | `300` | 实际时长限制，1–900 秒，不信任客户端时长 |

只启动一个应用 worker。ASR 每次处理一条，最多 32 条待处理；子进程复用模型，空闲五分钟释放。内存需求高于模型文件大小，应另留操作系统、应用、浏览器和 Agent 所需余量。

完整备份包含音频、识别文字和工作状态；权重不进入私人数据备份，恢复后重新安装或挂载模型，中断工作重排。

## 使用和接口

- 手机长按语音/桌面右键 → 转文字；完成后可复制、隐藏/显示。转写不要求 Agent 在会话中。
- 设置 → 外观与语音 → 本地语音转文字，查看就绪状态和时长限制。
- 发给 Agent 的语音先识别再回复，引用语音后 @Agent 同样支持。失败会标注，Agent 不应猜测内容。
- 原音频保留。机器识别可能出现近音词、人名、数字和混说误差，重要内容可对照原音。

| 接口 | 结果 |
| --- | --- |
| `POST /api/messages/{id}/transcribe` | 排队或复用，返回 `transcription` |
| `GET /api/messages/{id}/transcription` | `idle/queued/running/done/failed`、文字和公开错误码 |
| `GET /api/transcription/status` | 登录用户读取服务就绪情况 |
| `POST /api/tools/attachments/{id}/transcribe` | Agent 作用域内请求识别 |
| `GET /api/tools/attachments/{id}/transcription` | Agent 读取进度和文字，每次检查任务有效性 |

`message.updated` 同步给会话成员，手动请求另有轮询补偿。本地 ASR 不把音频发往外部；文字进入 Agent 上下文后按语言模型供应商设置处理。

## 参考

- [SenseVoice 官方项目](https://github.com/QwenAudio/SenseVoice)
- [sherpa-onnx 模型说明](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)
- [官方 Python 示例](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/offline-sense-voice-ctc-decode-files.py)
