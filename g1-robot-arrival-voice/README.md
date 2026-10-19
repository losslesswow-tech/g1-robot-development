# g1-robot — 宇树 G1 人形机器人 导航 + DDS 桥接资料

robot实验室 G1 机器人（ROS1 noetic 导航容器 + unitree_sdk2 DDS 桥接容器）的**部署、调试与模型资料**汇总。

> ⚠️ **G1 是真人形机器人。** 未清场、无人监护时禁止执行 `move` / `action` / `g1_arm` / 灵巧手写寄存器；
> `docker start|restart g1-bridge` 必然触发 `StandUp → 3s → Start`，机器人会自己站起来。

---

## 目录

| 路径 | 内容 |
|---|---|
| `docs/g1-bridge_容器与镜像.md` | **桥接容器的权威信息**：镜像 ID/digest、三种获取方式、运行参数、部署到其他主机的注意事项 |
| `docs/G1-导航桥架架构文档.md` | **真源文档**（667 行）：网络拓扑、桥接容器构建、坑点清单、雷达修复、灵巧手、手臂低级控制 |
| `docs/G1_收工交接_20260831.md` | 2026-08-31 收工交接（现场状态、唤醒步骤、安全红线） |
| `docs/现场状态核对_20260916.md` | 2026-09-16 实测状态，**修正了交接文档的 4 处偏差**，动手前先读这份 |
| `docs/导航容器配置核查报告_192.0.2.5_20260916.md` | 宿主机 `192.0.2.5` 导航容器 `nav_robot` 与 G1 基线的逐项核对：**缺 IMU 翻转补丁（P0）**、无桥接容器（P1），含修复命令与验收判据 |
| `bridge/` | 桥接容器源码：`Dockerfile`、`g1_start.sh`、`pkg/src/{g1_ros1_sdk,g1_control,g1_arm}.cpp` |
| `bridge/fetch_unitree_sdk2.sh` | 一键从导航容器提取 unitree_sdk2 预编译库（44 MB 二进制未入库，本脚本是复原方式） |
| `nav/x_nav.yaml` | 导航配置（`robot_model: G1_BRIDGE`、`networkInterface: enp4s0`、G1 几何参数） |
| `nav/MID360_config.json` | 头顶 MID360 雷达配置（host `192.0.2.16` / lidar `192.0.2.14`，`extrinsic roll=180`） |
| `nav/livox_patch/pub_handler.{h,cpp}` | livox 驱动补丁：让 `extrinsic_parameter` 同时作用于 IMU（G1 雷达倒装） |
| `tools/inspire_hand_ctl.py` | Inspire RH56DFTP 灵巧手 Modbus TCP 控制（左手 `.210` / 右手 `.211`） |
| `tools/dump_markers.sh` | 导航 marker 诊断脚本 |
| `arrival_voice/` | G1 到点播报核心、TTS/WAV 音频适配与本地模拟；现场任务身份接入状态见模块 README |
| `navigation_task/` | 原导航任务“语音播报”步骤、文字输入与 WAV 导入控件、部署及回滚；2026-10-10 已安装加载，任务实机验收待完成 |
| `wifi_access/` | G1 无线接入的限时路由、只读 DDS/传感器检查与自动回退；尚未安装长期无线配置 |
| `network_modes/` | 192.0.2.5 现场有线 / Wi-Fi 双方案、默认只读的候选受保护桥接、网络租约与回退；离线准备完成，实机验收待开机 |
| `model/g1_29dof_rev_1_0.urdf` | 官方 G1 URDF（29 DOF rev 1.0） |
| `model/meshes/`、`model/g1_description/` | URDF 网格与 ROS 包 |
| `model/bake_g1.py` | 把 URDF 烘焙成单一世界坐标 STL（`g1_model.stl`） |
| `model/patch_bundle.py` | 把 Web 前端的 ArrowHelper 替换为 G1 STL 模型 |
| `model/g1_model.stl` | 烘焙产物，供导航 Web 显示真机外形 |

---

## 快速开始

### 1. 网络前提

| IP | 设备 |
|---|---|
| `192.0.2.16` | 导航宿主机 `enp4s0`（**必须在这个网段**） |
| `192.0.2.8` | G1 上位机（Jetson，SSH `operator@`） |
| `192.0.2.10` | G1 运动控制器 MCU（DDS 目标） |
| `192.0.2.14` | MID360 雷达 |
| `192.0.2.3/211` | 灵巧手 左/右（Modbus TCP :6000） |

```bash
ping -c1 192.0.2.8 && ping -c1 192.0.2.10 && ping -c1 192.0.2.14
```

### 2. 起容器（顺序不能反）

```bash
docker start robot-navigation          # roscore + Web :8000 在这里
docker start g1-bridge            # ⚠️ 会让机器人站立，务必先清场
```

### 3. 只读校验（**这一步之前不发任何运动命令**）

```bash
docker exec robot-navigation bash -c 'source /opt/ros/noetic/setup.bash; rosnode list | grep g1'
docker exec robot-navigation bash -c 'source /opt/ros/noetic/setup.bash; timeout 3 rostopic hz /sdk/imu'
docker exec g1-bridge bash -lc 'source /opt/ros/noetic/setup.bash; source /catkin_ws/devel/setup.bash; \
  /catkin_ws/devel/lib/g1_ros1_sdk/g1_control enp4s0 query'      # 期望 fsm_id=500
```

### 4. 控制入口

```bash
# 状态机 / 运动 / 预设动作
... g1_control enp4s0 {query|stand|damp|start|move <vx> <vy> <vyaw>|stop|action <id>|actionlist}
# 手臂低级控制（肩 Pitch + = 后摆，前伸用 -π/2）
... g1_arm enp4s0 {info|forward|side|down|probe}
# 灵巧手（1000=伸开、0=闭合）
python3 tools/inspire_hand_ctl.py status
```

---

## 重新编译桥接程序

```bash
# 首次：从导航容器提取 unitree_sdk2 预编译库（44 MB 厂商二进制未入库）
cd bridge && ./fetch_unitree_sdk2.sh robot-navigation

docker cp bridge/pkg/src/g1_control.cpp g1-bridge:/catkin_ws/src/g1_ros1_sdk/src/g1_control.cpp
docker exec g1-bridge bash -lc 'source /opt/ros/noetic/setup.bash && cd /catkin_ws && \
  catkin_make -DCATKIN_WHITELIST_PACKAGES=g1_ros1_sdk'
```

## 获取桥接容器镜像

**首选**——从实验室 Gitea registry 直接拉（需先登录，匿名会 401）：

```bash
docker login 192.0.2.17:3000
docker pull 192.0.2.17:3000/operator/g1-bridge:1.0
docker tag  192.0.2.17:3000/operator/g1-bridge:1.0 g1-bridge:1.0
```

**备选**——从 Release 下载离线包（1.8 GB，OCI 格式）：

**Release 页**：<https://source.example.invalid/robot/reference>

```bash
sha256sum g1-bridge-1.0.tar    # 期望 a1704c22f7440afd79f722a8c9ccfc161ccf5335da5df74ef1eb40d3cebdede8
docker load -i g1-bridge-1.0.tar
```

镜像 ID / manifest digest 均为 `sha256:ee6129fa6be8b3a412b28e51349230d49b7a0805464e092c4f4cbbc875de6e29`（各出处已逐一比对一致）。
部署到其他宿主机前**务必先改 `g1_start.sh` 里的网卡名**——详见 `docs/g1-bridge_容器与镜像.md`。

## 重新部署 Web 3D 模型

```bash
docker cp model/index-WlVTOc2a.js robot-navigation:/catkin_ws/src/x_nav_control/scripts/dist/assets/
docker cp model/g1_model.stl     robot-navigation:/catkin_ws/src/x_nav_control/scripts/dist/assets/
```

---

## 安全红线

- 真人形。未确认清场禁止 `move` / `action` / `g1_arm` / 手指写寄存器。
- 禁止把 `docker restart g1-bridge` 当「重连手段」——它会让机器人重新站立。
- FSM：`500` 只能 `damp` 回 `1`，不能直接跳 `4`/`0`。
- 收工顺序：`damp` → 停桥接容器 → 断电。
- 灵巧手开合极性（`1000=伸开 / 0=闭合`）未复检前禁止开合。

## 本仓库未包含（需另取）

| 项 | 原因 | 获取方式 |
|---|---|---|
| `unitree_sdk2/` 预编译库（44 MB） | 厂商二进制，留在导航容器里，避免二进制污染 git 历史 | **`cd bridge && ./fetch_unitree_sdk2.sh robot-navigation`**（源容器已停止也能提取） |
| `robot-navigation` / `g1-bridge` 镜像 | 体积过大（9.4 GB / 23.9 GB） | 从阿里云私有仓库 pull |
| 导航 Web 前端 bundle 全量 | 属导航镜像内容 | 见架构文档，仅补丁脚本入库存档 |
| 现场录屏 / bag 数据 | 体积大、含隐私 | 按需单独归档 |
| `浙江robot机器人导航系统接口规范V2.1.docx` | 文件标注「内部文档禁止外传」，且非 G1 专项资料 | 按内网合规流程单独分发，不进本仓库 |
| `dsh-desktop_*.deb`、`DSH-Linux-部署清单.md` | 与 G1 无关（误放在 `robot-project/`） | 不进本仓库 |
