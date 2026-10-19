# G1 导航点语音播报

默认使用 G1 自带音频设备的文字转语音，另支持预录 WAV。2026-10-10 新增可选阿里云 CosyVoice：在导航宿主机合成合规 WAV，再复用现有机器人音频传输。需要显式启用，不静默回退至 SDK TTS；现场启用状态及安装步骤见[CosyVoice 接入记录](../navigation_task/CUSTOM_TTS_INTEGRATION_20261010.md)。模块不创建运动控制客户端，不发布速度，不构建镜像或创建容器。

## 当前实现与接入边界

已实现文字与音频配置网页、点位配置、持久化任务生命周期校验、重复播报防护、G1 音频适配、本地 JSONL 模拟和只读 ROS 观察器。另按用户明确接受的旧协议限制增加了[保守自动到达接入](LEGACY_AUTO.md)，复用原音频链路，网页默认暂停，需从下一次新导航开始监听。**严格 JSONL 入口与旧协议监听器是两个独立入口，后者不能提供跨任务迟到结果的绝对关联保证。**

现场源码由用户于 2026-10-09 从 `nav_robot` 只读提供，已确认：

| 来源 | 确认行为 | 限制 |
|---|---|---|
| `extra.py:navigate` | `POST /api/extra/navigate`，JSON `id`，调用 `/x_nav/planner/service` 的 `multi_nav` | HTTP 成功表示路径规划成功，不能当作到达 |
| `robot_web_controller.py:nav_state_callback` | 订阅 `/x_nav/state`（`std_msgs/String`） | `13` 无任务 ID、地图和点位 |
| 同上 | `10#目标#进度#总数`、`11#目标#进度#总数`、`12` 对准、`13` 到达、`14` 规划成功、`15` 规划失败 | 不能凭上一条 `10` 保证后来的裸 `13` 属于同一次任务 |
| `extra.py:cancel_nav` | 发布 `/planner/cmd` 的 `cancel_nav` | 独立于到达消息，没有关联 ID |
| `extra.py:point_nav_work_cancel` | `disable_localplanner` / `enable_localplanner` | 属停止/继续作业，不能自动当作到达 |
| `GET /api/extra/get_status` | 当前地图、位置、展示状态等 | 不含唯一任务/目标关联，空闲、速度为零或距离接近均不构成到达证明 |

已核对 `task.py`：它收到裸 `13` 后直接置 `reach_state=1`，同样没有任务关联；动作 `J` 的语音分支为 `pass`。严格任务关联仍需要在导航结果产生处增加带任务标识的事件。现有保守模式须明确接受限制后启用，不将当前位置、停止或对准状态作为完成事件。

后续只读核查确认：`/nav/x_planner` 为二进制；`/catkin_ws/src/x_nav` 当前可见文件仅配置、CMake/package、`Pose6D.msg` 和 `nav_function.srv`，未取得规划器实现源码。该服务响应定义为 `uint32 success`、`string return_msgs`，没有定义带任务 ID 的完成事件；现有 Web 包装还丢弃了 `return_msgs`。不能假设这个未约定的字符串藏有可靠的任务结果。剩余依赖是导航组件提供方的带任务 ID 完成接口，或规划器结果产生处源码。

当前部署以用户本次现场信息为准：Ubuntu x86_64 主机 `192.0.2.5`，`nav_robot` / `g1-bridge` 同机 host 网络，机器人网卡 `enp132s0`。仓库旧文档中的 `robot-navigation`、`enp4s0` 仅是历史基线；工作区根目录旧 `navigation/` 和 `bridge/` 不属于本模块。

## 文字输入与音频导入界面

在仓库根目录运行（Python 3.8 及以上，仅标准库）：

```bash
python -m arrival_voice.web
```

打开 <http://127.0.0.1:8768/>，选择已有点位或新建点位，填写地图名称和导航点名称，然后：

1. 选择“文字转语音”，输入播报文字，选择中文或英文。
2. 或选择“预录音频”，点击导入或拖入 WAV 文件。格式为 16 kHz、单声道、16 位 PCM，最长 5 分钟、最大 10 MB；格式不符会提示原因。
3. 点击保存配置。导入的 WAV 可在页面中使用当前电脑试听。
4. 点击“模拟播报”验证当前内容；默认模式不连接机器人。点击导出可下载包含配置和已引用音频的 ZIP。

配置默认保存至 `.local-voice/web/config.json`，音频保存在同目录的 `audio/` 中。可通过 `--config` 指定其他配置路径，通过 `--port` 修改端口。服务仅监听本机回环地址。文件导入、保存及打开页面不会触发机器人播音。

界面沿用本模块现有 `audio.py` 适配，不改变音频接入方式。在已准备好 SDK 的设备上，显式使用 `--backend unitree --interface 网卡名 --allow-robot-audio` 启动时，测试按钮才会通过原适配调用机器人音频。该按钮是手动测试，不伪造导航到达事件，也不证明扬声器已经完成发声。

运行中的严格 JSONL 消费者不会自动重载配置。旧协议监听器每秒读取网页已保存配置，变更使当前候选作废，下一次新导航使用新内容。网页底部提供监听状态和启用/暂停开关；未连接监听器时不能启用。自动到达与手动测试共用原 TTS/WAV 适配和播放互斥锁。

## 经导航宿主机 SSH 使用机器人音频

2026-10-09 用户现场核实：导航宿主机 `192.0.2.5` 能通过 `operator@192.0.2.18` 登录机器人；机器人为 aarch64、Python 3.8.10，`eth0` 地址为 `192.0.2.8/24`，已有 `/home/operator/g1_voice/.venv/bin/python`。

新增可选 `unitree-ssh` 转发入口。它在机器人现有 Python 环境运行本模块原 `UnitreePlayer`，不更改 SDK 安装或原音频 API，不要求宿主机安装 CycloneDDS/Python SDK。每次播放前读取音量；WAV 作为数据传输到机器人临时目录，播放后删除。程序不接收或保存 SSH 密码，也不修改主机密钥记录。认证失败或超时不会自动重试播音。

在导航**宿主机**解压包的根目录执行：

```bash
bash arrival_voice/start_on_nav_host.sh operator@192.0.2.18
```

按终端提示输入机器人 SSH 密码。脚本创建仅本次运行使用的私有 SSH 控制套接字，先只读探测音量和 SDK 环境，再启动 `http://127.0.0.1:18768/` 的实机配置界面。启动不会播放声音；点击“提交机器人播报”才会发声。Ctrl+C 停止该界面并关闭本次专用 SSH 连接，配置文件保留，不停止其他 SSH 连接或机器人服务。

从 Windows 查看导航宿主机界面时，在 Windows PowerShell 建立端口转发（按提示输入导航机 SSH 密码）：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 18768:127.0.0.1:18768 operator@192.0.2.5
```

然后在 Windows 打开 <http://127.0.0.1:18768/>。原 `8768` 预览仍是本地模拟页面。

独立命令行探测或单项测试可使用 `python3 -m arrival_voice.ssh_audio --help`。发声必须显式带 `--allow-robot-audio`，TTS 和 WAV 每次只选一种。接口成功需要结合现场听到的内容验收，不能当作扬声器播放完成的证据。

## 点位配置

复制 `config.example.json`，按**地图名称和导航点名称的字符串**配置；不是列表行号，也不是未经核实的规划器索引。示例 `1009/2` 和 `1009/原点` 来自只读 HTTP 点位查询。替换示例讲解文字再用于现场。

```json
{
  "version": 1,
  "max_task_seconds": 300,
  "points": [
    {"map_name": "1009", "point_name": "2", "mode": "tts",
     "speaker_id": 0, "text": "这里是二号展区，接下来介绍……"},
    {"map_name": "1009", "point_name": "原点", "mode": "wav",
     "file": "audio/origin.wav"}
  ]
}
```

`speaker_id=0` 为中文，`1` 为英文；最终发音和支持情况以实际机器人音频服务为准。WAV 必须为 16kHz、单声道、16 位 PCM，路径位于配置文件目录内，时长不超过 300 秒。未配置的点位静默。模块不使用云 TTS，不改变音量，不自动下载媒体。

## 本地验证（无需机器人、ROS 或 SDK）

在仓库根目录执行，Python 3.8 及以上，仅标准库：

```bash
python3 -m unittest discover -s tests -v
python3 -m arrival_voice \
  --config arrival_voice/config.example.json \
  --database .local-voice/simulation.sqlite3 \
  --events arrival_voice/simulation.jsonl
```

Windows 把 `python3` 改为 `python`，在一行运行第二条命令。默认 `dry-run` 只输出将播报的内容，不导入 Unitree SDK。第一次模拟应输出一次 `dry_run=true`，取消任务不播；重复运行同一数据库应零次播报。

模拟和现场必须使用不同数据库。不要清空现场数据库来“修复”去重；保留数据库和同目录锁文件，数据库所在目录需要可写。每台机器人一个消费者，数据库不能共用。进程退出后锁自动释放，锁文件可保留。

## 可信事件约定

每行一个 JSON 对象，恰好包含以下字段：

```json
{"task_id":"唯一导航尝试UUID","map_name":"1009","point_name":"2","kind":"accepted","sequence":0}
```

`kind` 为 `accepted`、`navigating`、`arrived`、`cancelled`、`failed`。生产端必须：

1. 每次导航尝试生成全局唯一 `task_id`，重复访问同一点也使用新 ID；重发同一尝试不生成新 ID。
2. 把目标地图/点位在接受任务时固定，并在后续事件中保持一致。
3. 只有实际任务接受后发送 `accepted`，有本任务导航执行证据后发送 `navigating`。
4. 只有规划器确认**本任务到达本目标**才发送 `arrived`。HTTP 规划成功、`12` 对准、空闲、靠近、经过不能发送它。
5. 每个任务的 `sequence` 严格递增，取消/失败/替换/导航退出及时关闭旧任务。生产端须对结果与取消排序，以权威终态为准。
6. 导航执行端断线、重连或重启时，关闭旧尝试；不能给无身份的旧消息补上当前任务 ID。

消费者先校验身份、顺序、导航阶段、时限与当前活动任务，再在 SQLite 中持久化播放占位，最后才调用音频。重复接受或重复到达不能重开终态任务。新任务使旧任务失效。消费者重启后未完成任务作废，避免迟到结果播放。

这里保证的是**每次任务最多一次音频提交尝试**：进程在占位后崩溃、音频超时或失败时，不自动重试，可能漏播。SDK 返回 0 只说明请求提交成功，不能证明扬声器已完成发声。需重播时应由操作者确认后发起新的受信任任务，不能复用到达消息绕过去重。

## 只读现场观察

取得现场授权后，由用户手动执行。先把本仓库的 `arrival_voice/` 放到宿主机 `/home/operator/g1-robot/arrival_voice/`，然后复制到现有导航容器的独立目录（不覆盖现有源码）。不要放入 `/nav_map`：现有 `task.py:get_map_list()` 会把其中的目录枚举为地图。

```bash
docker exec nav_robot mkdir -p /opt/g1-arrival-voice
docker cp /home/operator/g1-robot/arrival_voice nav_robot:/opt/g1-arrival-voice/
docker exec -it nav_robot bash -lc \
  'source /opt/ros/noetic/setup.bash; export ROS_MASTER_URI=http://127.0.0.1:11311; cd /opt/g1-arrival-voice; python3 -m arrival_voice.observe'
```

观察器只订阅 `/x_nav/state`、`/planner/cmd`、`/node_cmd` 并输出原始消息、发布节点及观察时间，不导航、不播音。用 Ctrl+C 仅停止观察器。观察器中所有旧协议解析都标记 `goal_correlated=false`。

## 机器人音频准备与使用边界

核心代码无额外依赖。真实音频适配需要官方 [Unitree SDK2 Python](https://github.com/unitreerobotics/unitree_sdk2_python)，不能把现有 C++ `unitree_sdk2` 库或已存在的 G1 audio C++ 头文件视为已经安装了 Python 包。SDK 音频源码已按提交 `814556d15970dd2ecf1c9984e845ca02ab07e206` 核对，尚未在现场安装或验证。

用户决定进入音频环境准备后，可在 **192.0.2.5 宿主机** 手动执行以下独立环境命令（需要 Git、Python >=3.8 及 venv；目录应尚未存在）：

```bash
python3 --version
python3 -m venv /home/operator/.venvs/g1-arrival-voice
source /home/operator/.venvs/g1-arrival-voice/bin/activate
git clone --no-checkout https://github.com/unitreerobotics/unitree_sdk2_python.git /home/operator/g1-arrival-audio-sdk
git -C /home/operator/g1-arrival-audio-sdk checkout --detach 814556d15970dd2ecf1c9984e845ca02ab07e206
python -m pip install -e /home/operator/g1-arrival-audio-sdk
python -m pip freeze > /home/operator/g1-arrival-audio-sdk/installed-requirements.txt
```

任一步失败即停止。官方依赖含 `cyclonedds==0.10.2`；如果安装报告找不到 CycloneDDS，需要按 [官方安装说明](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/README.md) 核实独立库路径，再继续。不更改桥接容器依赖，不把现有容器的动态库路径直接假定为宿主机路径。

已确认导航容器 Python 为 3.8.10，`pip show unitree_sdk2py` 无输出；宿主机 Python 为 3.10.12，未安装这两个 Python 包，创建 venv 时报告缺少 ensurepip。本次新增网页无需这些依赖；音频链路已通过后述 SSH 实机测试。

在单独音频环境安装 SDK 后，先运行只读探测：

```bash
python3 -m arrival_voice.audio_check --interface enp132s0
```

它只调用 `AudioClient.GetVolume()`，确认 DDS 音频服务可达。失败立即停止，不以更换机器人运控模式或重启桥接作为解决手段。用户现场确认可播音后，使用独立短句测试：

```bash
python3 -m arrival_voice.audio_check --interface enp132s0 \
  --text '语音测试，已连接机器人音频设备。' --allow-robot-audio
```

这会让机器人发声，只调用音频 API，不导航、不调音量、不改灯带。不要直接运行官方综合示例：它含调音量、灯带，部分示例还含动作调用。

只有受信任的带任务 ID 生产端完成接入并验证后，才可以使用以下播放入口；**不能将 `observe` 的日志或裸 `13` 输入这里**：

```bash
python3 -m arrival_voice \
  --config /home/operator/g1-arrival-voice/config.json \
  --database /home/operator/g1-arrival-voice/live.sqlite3 \
  --backend unitree --interface enp132s0 --allow-robot-audio
```

该命令从标准输入读取可信事件，不发送导航请求。当前不提供自启服务，避免接入尚未确认时误启。后续现场上线不需要构建镜像，不需要启动/重启 `g1-bridge`，也不应为此重启含 ROS master 的 `nav_robot`。

官方接口依据（2026-10-09 核对）：

- [G1 AudioClient 源码：TtsMaker / GetVolume / PlayStream / PlayStop](https://github.com/unitreerobotics/unitree_sdk2_python/blob/master/unitree_sdk2py/g1/audio/g1_audio_client.py)
- [官方 G1 WAV 播放示例](https://github.com/unitreerobotics/unitree_sdk2_python/blob/master/example/g1/audio/g1_audio_client_play_wav.py)
- [官方 G1 C++ 音频示例：中文/英文 speaker_id](https://github.com/unitreerobotics/unitree_sdk2/blob/main/example/g1/audio/g1_audio_client_example.cpp)

本地验证通过本身不代表现场链路可用。本次另完成了下述独立音频实机验收；ROS 自动到达接入仍未验证。

## 本次交付验证

2026-10-09，在 Windows 本地 Python 3.9.13 执行 59 项 unittest 全部通过；所有新增 Python 文件通过 Python 3.8 语法解析，网页脚本通过 JavaScript 语法检查。独立 Edge 浏览器验证了文字输入与保存、模拟播报、无效 WAV 拒绝、有效 WAV 导入与保存、刷新后数据保留及桌面/手机布局，未发现浏览器异常。浏览器验证使用独立测试配置，不修改用户预览配置，不播放机器人音频。

JSONL 命令行首轮模拟一次播报、同库重放零次播报；缺少显式音频开关的 TTS 测试在导入 SDK 前被拒绝。ROS 观察器仅验证帮助入口和协议解析，未在本地运行 ROS 订阅。

已通过导航宿主机 SSH 调用机器人现有 SDK，完成一句短中文 TTS 和 3.655 秒预录 WAV 的导入、保存与播放；两项各执行一次，用户现场确认都听到了。只读音量为 100，本次未调整。详见 [实机测试记录](HARDWARE_TEST_20261009.md)。

本次改动位于本地 `codex/g1-arrival-voice` 分支，尚未提交或推送到远端 `main`。普通交付包只包含本模块与测试；独立实机测试包另含生成的短语音样本和测试配置，不含 SDK、数据库或凭据。现场仅启动独立音频测试界面，未重启导航、桥接或机器人服务。
