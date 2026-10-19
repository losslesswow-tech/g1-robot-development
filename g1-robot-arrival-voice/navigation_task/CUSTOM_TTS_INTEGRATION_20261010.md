# CosyVoice 接入记录（2026-10-10）

状态：用户已确认使用阿里云 CosyVoice，接受 `cosyvoice-v3-flash` 和 `longanyang` 音色。用户已在宿主机执行安装，云端生成检查和音频服务启用成功，随后只读核对确认原任务目录可用、6 条配置保留、导航后端哈希不变。安装与本次核对未提交机器人播音；实机听音及真实重启自启验收仍待完成。

## 模型与调用接口

| 项目 | 配置 |
|---|---|
| 模型 / 音色 | `cosyvoice-v3-flash` / `longanyang` |
| 用户基础地址 | `https://llm-4nfolspkfnl4xxg4.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| 实际语音接口 | 同一域名上的 `POST /api/v1/services/audio/tts/SpeechSynthesizer` |
| 请求 | `model`；`input.text/voice/format=wav/sample_rate=16000`；关闭 SSML；按原 `speaker_id` 设置中英文提示 |
| 推理位置 | 阿里云北京地域云服务；宿主机负责请求、下载、格式校验及现有音频传输 |
| 密钥 | 运行时读取 `DASHSCOPE_API_KEY`，不写入源码、安装包、任务数据或机器人 |

按[官方 HTTP API](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)及[官方音色列表](https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list)核对。兼容 API 基础路径不直接作为语音路径使用。

## 接入行为

原任务编辑器文字 → 宿主机音频服务 → CosyVoice → 合规 WAV → 原 SSH 传输 → 机器人 `PlayStream`。

- 原任务动作 `J`、`tts/wav/saved` 字段和三个前端选项保持。启用后任务文字和已保存的文字播报均使用 CosyVoice；预录 WAV 直接走原路径，不调用云服务。
- `arrival_voice/cosyvoice.py` 只做云端合成，没有机器人 SDK 或运动控制入口。仅向已校验的官方北京接口发送密钥；下载音频不附带密钥，不跟随 HTTP 重定向。音频下载限官方北京 OSS 域名，使用 HTTPS。
- 云端实测返回流式 WAV 长度占位值（RIFF `0x7fffffbf`、data `0x7fffff9b`）。适配器仅对这一已观察的标准 44 字节 PCM 头重写实际长度，采样字节保持不变。其他格式错误、不完整 HTTP 传输、奇数字节 PCM、超限文件继续拒绝，不放宽用户导入 WAV 的原校验。
- 结果仍须符合 16 kHz、单声道、16 位 PCM、10 MB、300 秒以内的现有限制。合成与下载共享最多 45 秒预算；播放前按实际时长检查原任务 350 秒请求预算并预留余量，超出预算拒绝提交。
- 原互斥锁和请求去重覆盖合成及播放，失败或结果未知不自动重试，不静默切回旧 TTS。生成音频通过临时目录传输，结束后清理。
- 对 `task-` 请求，在合成前后只读核对原 `/api/task/voice_status` 的运行 ID、暂停和任务状态；取消、切换或状态不可读时，不提交生成后的音频。自动到点模式再次检查开关和监听连接。已交给机器人播放的音频不能据此保证撤回，保留现有协议边界。
- 启动仅初始化配置及查询机器人音量，不生成后自动播音，不恢复旧请求。

只需更新宿主机音频服务，不需要重载导航任务后端或前端。机器人受限 worker SHA256 仍为 `8c4dd29690cab06c1c45576c7b7c5eada3a52bbeb9578d99a2b84bbb8c51cea7`；专用公钥的 `restrict` 和固定命令保持。地图、阈值、有线导航及运动桥接保持。

## 已完成验证

- 本地完整 99 项测试通过；在导航容器 Python 3.8 独立临时目录内同样 99 项通过，未连接真实运动或机器人播放器。
- 新增 21 项覆盖调用契约、密钥隔离、URL/重定向限制、格式和传输完整性、占位头修复、去重、任务取消或切换、自动播报暂停、预录音频旁路及时间预算。
- 使用用户密钥完成两次短句云端生成检查，发现并确认占位头问题。使用最终规范化代码得到 3.000 秒、96,044 字节的合规 WAV，确认 PCM 采样不变。两轮均未提交机器人播放。
- 本地样本：`.local-voice/hardware/cosyvoice-cloud-check-20261010.wav`。密钥通过隐藏终端输入传入检查进程，本次未保存到本地文件。
- 原任务目录只读检查仍为 `live:true`、6 条。当前 Windows 对宿主机 SSH 登录被认证拒绝，已使用现有任务 API 仅暂存安装文件，没有绕过宿主机认证。

## 现场安装及只读核对结果

用户现场安装输出 `audio_ready:true`，模型为 `cosyvoice-v3-flash`、音色为 `longanyang`，6 条播报配置保留。宿主机云端检查样本为 96,044 字节、3.000 秒，`robot_audio_submitted:false`。回退目录为 `/home/operator/.local/share/g1-task-voice/backups/cosyvoice-20261010T080601Z/`。

随后经原任务 API 进行只读核对：宿主机 `/api/state` 的 `live:true`、`tts.provider:cosyvoice`；任务 `/api/task/voice_catalog` 的 `live:true`、6 条。任务当前 `managed:false`，历史 `job:submitted` 不作为本次听音证据。

导航容器 Python 3.8.10；安装文件哈希与原部署一致：`task.py` 为 `8faaa64bd6e336bfe79d7e50e84afd9dc450c480eed58ccedde27271601d5cc7`，`g1_task_voice.py` 为 `cb6a9bcd1530d0dc1548fda9458315434401c360f11b65d8b53abba28be02034`。核对没有请求云端合成、播音或导航，也没有读取凭据文件。

安装时 `/lib/systemd/system/snapd.service` 的 `RestartMode` 提示来自原有系统 snapd 单元，未阻止本次音频服务启用。证据：`.local-voice/hardware/cosyvoice-installed-20261010.json`，内含用户安装回执摘要与只读结果，不含密钥。

## 现场启用

保持任务空闲，由现场人员在 **operator@192.0.2.5 宿主机终端**执行：

```bash
docker cp nav_robot:/tmp/g1-cosyvoice-install-20261010.sh /tmp/g1-cosyvoice-install-20261010.sh
bash /tmp/g1-cosyvoice-install-20261010.sh
```

安装器会隐藏输入 API 密钥并只生成文件检查云服务，之后需要宿主机 sudo 密码，密码不保存。为支持服务自启，API 密钥单独存于宿主机 `~/.local/share/g1-task-voice/cosyvoice/credentials-<UTC时间>.env`，权限 600，由 systemd `EnvironmentFile` 注入；模型配置另存于同目录。

安装器保留原发布目录、配置和音频，复制既有受限 SSH wrapper 到新发布目录，只添加 `30-cosyvoice.conf` 音频服务覆盖项。在准备开始和实际切换前检查任务空闲及无音频 SSH 子进程，并核对原配置哈希。只重启 `g1-task-voice.service`，不重载导航 Web/API、容器、ROS 或运动桥接。

成功应显示 `audio_ready:true`、`tts.provider:cosyvoice` 和现有播报数量；该状态不代表现场听音完成。之后继续用原任务页输入文字，由现场协调单条语音步骤验收，不自动启动导航。

## 回退

安装器输出确切命令：

```text
bash /home/operator/.local/share/g1-task-voice/backups/cosyvoice-20261010T080601Z/rollback.sh
```

回退脚本校验安装哈希，仅移除本次覆盖项并重启音频服务，恢复原运行代码与 SDK TTS。覆盖项后来被修改时会停止并提示检查。新配置、凭据及检查样本保留在宿主机，回退不输出其内容，不覆盖任务、用户音频或去重数据库。新服务激活失败时，安装器尝试同样的回退。

真实重启自启验收仍待完成；已启用/运行不能代替该验收。
