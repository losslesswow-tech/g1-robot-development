# Unitree G1 ROS1 bridge（Jetson ARM64，默认无运动）

本目录是 G1 Jetson 的 ARM64 ROS1/DDS Bridge，目标环境为 Ubuntu 20.04、
`linux/arm64`、`eth0=192.0.2.8/24`。它同时支持两种明确分离的拓扑：

- 当前外置导航：ROS Master/导航位于 `192.0.2.6`。
- 未来同机部署：ARM64 导航与 Bridge 都在 Jetson 上，使用 host 网络、
  `ROS_IP=192.0.2.8` 和 `ROS_MASTER_URI=http://192.0.2.8:11311`。

G1 MCU/DDS `192.0.2.10` 由 Unitree DDS 经指定网卡发现；SDK 接口没有
单独的“MCU IP”参数，所以该地址只用于只读网络核对，不硬编码进节点。

## 安全边界

默认配置是：

- `ENABLE_MOTION=false`
- `AUTO_START=false`
- `MOTION_LOCKED=true`
- 不创建 `LocoClient`
- 不订阅 `/cmd_vel`
- 不在启动、停止或重启时调用 `StandUp`、`Start`、`StopMove` 或 `Damp`
- 仅订阅 DDS `rt/lowstate`，发布 ROS `/sdk/imu` 与锁存的
  `/sdk/bridge_status`

`g1_control.cpp` 和 `g1_arm.cpp` 只作为参考源码保留，Docker 构建默认
`BUILD_CONTROL_TOOLS=OFF`，不会生成这两个执行文件。

## ARM64 路径为何不是 `--platform` 转换

`--platform=linux/arm64` 只选择/构建 ARM64 镜像，不能把 amd64 ELF 或
静态库转换为 ARM64。本方案同时执行以下独立约束：

1. `FROM --platform=linux/arm64 ros:noetic-ros-base-focal` 选择 ROS 官方
   ARM64 manifest。
2. 构建阶段要求 `TARGETARCH=arm64` 且 `dpkg --print-architecture=arm64`。
3. 构建上下文必须是 Unitree 官方 `unitree_sdk2` 完整仓库，而不是原脚本
   从导航容器抽出的扁平库目录。
4. SDK 自己的 CMake 在 ARM64 阶段选择 `lib/aarch64` 与
   `thirdparty/lib/aarch64`。
5. `verify_architecture.sh` 用 `readelf` 检查 SDK `.a` 的所有可见 ELF
   头、DDS `.so` 以及最终 bridge 可执行文件；任何 x86-64 文件都会使
   构建或启动失败。

注意：Unitree SDK2 的核心 `libunitree_sdk2.a` 仍是厂商发布的预编译
AArch64 库；这里没有也不会伪称从其源码重新编译了该库。

## 1. 准备 SDK（不接机器人）

先从 Unitree 官方仓库选定并记录一个与现场 G1 固件/API 匹配的完整
40 位 commit SHA。不要用移动的 `main` 作为生产构建标识。

```bash
cd bridge
bash scripts/fetch_unitree_sdk2_official.sh <40位commit SHA>
```

脚本只准备 `bridge/unitree_sdk2/`，不会连接机器人。若构建机不能访问
GitHub，可在联网机器上检出同一 commit，再离线复制完整目录到
`bridge/unitree_sdk2/`。必须保留这些路径：

```text
unitree_sdk2/include/unitree/robot/g1/loco/g1_loco_client.hpp
unitree_sdk2/lib/aarch64/libunitree_sdk2.a
unitree_sdk2/thirdparty/lib/aarch64/libddsc.so
unitree_sdk2/thirdparty/lib/aarch64/libddscxx.so
```

若这些 ARM64 工件或匹配的 SDK commit 不可获得，停止部署；不要从原
amd64 导航容器提取库来代替。

## 2. 离线构建与镜像预检

在 Jetson 原生构建：

```bash
cd bridge
docker buildx build --platform linux/arm64 --load -t g1-bridge:arm64 .
docker image inspect g1-bridge:arm64 --format '{{.Architecture}}'
```

预期为 `arm64`。在 x86_64 上用 buildx 构建需要可用的 ARM64 模拟器；
这只影响构建执行方式，不放宽任何 ELF 架构检查。

不启动 bridge 的镜像内复检：

```bash
docker run --rm --network none g1-bridge:arm64 image-check
```

完整离线回归还会检查两种拓扑、运动锁、Compose 合并结果，并在回环接口上
启动临时 ARM64 ROS Master、Bridge 和导航观察容器，实际读取锁存状态话题：

```bash
bash tests/smoke.sh
```

该测试不要求机器人在线。测试 Bridge 仍只订阅 DDS lowstate，不创建运动
客户端、不订阅 `/cmd_vel`，也不发送执行器指令。

## 3. 参数

| 环境变量 | Jetson 默认值 | 含义 |
|---|---:|---|
| `ROS_MASTER_URI` | `http://192.0.2.6:11311` | 外置导航的 roscore；同机时改为 `.164:11311` |
| `ROS_IP` | `192.0.2.8` | Jetson 对 ROS1 节点公布的可回连地址 |
| `DDS_INTERFACE` | `eth0` | Unitree DDS 使用的 Jetson 网卡 |
| `ROS_DOMAIN_ID` | `0` | 传给 Unitree `ChannelFactory::Init` 的 DDS domain |
| `ENABLE_MOTION` | `false` | 是否创建运动客户端并订阅 `/cmd_vel` |
| `AUTO_START` | `false` | 是否在启动时执行 `StandUp` 后执行 `Start`；还要求 `ENABLE_MOTION=true` |
| `MOTION_LOCKED` | `true` | 为 true 时拒绝运动与自动启动开关 |
| `DEPLOYMENT_MODE` | `external_navigation` | `external_navigation` 或 `colocated_jetson` |

启动脚本会拒绝不存在的 DDS 网卡、未配置到本机的 `ROS_IP`、非法 DDS
domain、`AUTO_START=true` 但运动未启用，以及非 ARM64 镜像/库。

Compose 使用 host 网络、`restart: "no"`、只读根文件系统、移除 Linux
capabilities。不要把 restart policy 改为自动重启，除非已经单独评审现场
故障恢复策略。

### 外置导航配置

```bash
docker compose config
docker compose up g1-bridge
```

### 未来 Jetson 同机配置

同机模式要求导航镜像本身也是原生 `linux/arm64`，并且两个容器都使用
host 网络。先启动导航侧 roscore，确认 `.164:11311` 可达，再启动 Bridge：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.colocated.yml \
  config

docker compose \
  -f docker-compose.yml \
  -f docker-compose.colocated.yml \
  up g1-bridge
```

Bridge 的 live preflight 会确认 `eth0` 存在、`.164` 确实属于主机网络命名
空间，且 ROS Master TCP 端口可达；任一条件不满足都会退出。

## 4. 分阶段无运动验收

以下命令用于现场人员执行；本文档本身不代表已经完成真机验证。

### 阶段 A：ROS 跨机通信（不启动 bridge）

1. 在 x86_64/WSL 主机确认有线地址确为 `192.0.2.6/24`，并启动
   计划使用的 roscore。
2. 确保 roscore 及导航节点公布的是 Jetson 可访问的 `.10` 地址，而不是
   WSL NAT 地址或 `127.0.0.1`。
3. 在 Jetson 只运行一个临时 ROS 客户端检查 master：

```bash
docker run --rm --network host --entrypoint bash g1-bridge:arm64 -lc '
  source /opt/ros/noetic/setup.bash
  export ROS_MASTER_URI=http://192.0.2.6:11311
  export ROS_IP=192.0.2.8
  rosnode list
'
```

此阶段不初始化 Unitree DDS，也不加载 bridge 节点。

### 阶段 B：DDS 网卡与只读状态桥

先确认接口与路由，不发控制命令：

```bash
ip -4 addr show dev eth0
ip route get 192.0.2.10
```

预期 `eth0` 包含 `192.0.2.8/24`，到 `.161` 的路由也走 `eth0`。
然后保持两个安全开关为 false：

```bash
cd bridge
ENABLE_MOTION=false AUTO_START=false docker compose up g1-bridge
```

日志必须明确出现 `SAFE MODE`、`AUTO_START is disabled`，且不能出现
`MOTION MODE` 或 `calling StandUp then Start`。

从 x86_64/WSL ROS 环境检查：

```bash
rostopic echo -n 1 /sdk/bridge_status
timeout 5 rostopic hz /sdk/imu
```

状态先应为 `read_only_waiting_for_dds_lowstate`，收到 MCU 的
`rt/lowstate` 后变为 `dds_lowstate_active`。IMU 频率必须以现场测量为准，
不能仅凭历史文档中的约 1 kHz 判定成功。

### 阶段 C：雷达链路（仍无运动）

雷达和导航留在 x86_64/WSL 侧验证，不修改导航容器。参考仓库现有
`MID360_config.json` 记录的 host 地址是 `192.0.2.16`，而本部署目标
是 `.10`；现场必须先确认实际配置与网卡绑定一致，否则雷达链路会失败。

启动既有雷达驱动后，先发现实际 topic，再测量，不臆测 topic 名：

```bash
rostopic list | grep -Ei 'livox|lidar|point|imu'
rosnode list | grep -Ei 'livox|lidar'
rostopic hz <现场确认的点云topic>
```

验收顺序应为：ROS master 可达 → DDS `lowstate`/`/sdk/imu` 正常 → 雷达
点云正常 → 再启动定位/建图；任一阶段失败都不要启用运动。

## 5. 运动功能的独立授权边界

本 bridge 的只读验收不需要运动模式。若以后确需运动，必须先由现场负责人
明确授权，并确认：机器人型号是 Unitree G1；SDK commit 与固件/API 兼容；
控制传输为 Unitree DDS；周围清场；有人持续监护；急停/遥控器可用；已明确
停止路径。遗留 `/cmd_vel` 映射中 `linear.x/y` 单位为 m/s、`angular.z` 为
rad/s，且 `linear.z` 被旧代码复用为状态命令字段，未经接口评审不要接入通用
导航输出。

运动启用需要显式设置 `ENABLE_MOTION=true`；这会创建 `LocoClient`、切换
移动模式并订阅 `/cmd_vel`。`AUTO_START` 仍默认 false。只有再次显式设置
`AUTO_START=true` 才会执行 `StandUp → 延时 → Start`，启动日志会以
`WARNING/ERROR` 级别醒目标明。容器关闭时不会自动发送 `Damp`，因为对未知
姿态的人形机器人在退出时强制卸力本身也可能造成跌倒。

当前 Compose 还设置 `MOTION_LOCKED=true`，因此即使误传上述两个开关也会
被入口脚本拒绝。本轮通信验收不解除该锁。

## 6. Bridge 与导航的只读链路验收

两个容器都启动后，在 Jetson 宿主机执行：

```bash
BRIDGE_CONTAINER=g1-bridge-safe \
NAVIGATION_CONTAINER=<ARM64导航容器名> \
TOPOLOGY=colocated_jetson \
REQUIRE_IMU=false \
bash scripts/verify_bridge_navigation_link.sh
```

该脚本会验证 host 网络、相同 Master、相同 `ROS_IP`、两镜像均为 ARM64、
Bridge 安全开关、`/sdk/bridge_status` 的实际 TCPROS 读取、`/sdk/imu` 类型，
以及 Bridge 节点没有 `/cmd_vel`。机器人供电且 DDS lowstate 已出现后，再将
`REQUIRE_IMU=true` 重跑，要求导航容器实际收到一帧 IMU。

只有最后一次检查通过，才能声称“导航容器与 Bridge 的 ROS1 通信及 G1
只读 DDS 数据链路已现场验收”。仅离线 smoke 通过不等于真机 DDS 已通过。

## 尚需现场确认

- 选定的 Unitree SDK2 commit 与 G1 固件/API 版本是否匹配。
- Jetson 的 Ubuntu/JetPack 用户态是否与 SDK 预编译库 ABI 兼容。
- ROS 官方 Noetic ARM64 基础镜像能否从现场网络获取。
- 外置拓扑下 WSL/主机防火墙是否允许 ROS1 双向 TCPROS/XML-RPC，而不只是 11311。
- DDS multicast/unicast 是否能经 `eth0` 到达 `192.0.2.10`。
- MID360 配置里的 host 地址从历史 `.103` 迁移到目标 `.10` 的现场方案。
- 导航本体及 `/nav` 专有二进制能否获得真正的 ARM64 构建；当前仓库中的
  `g1-navigation:noetic-safe` 仍为 amd64，不能部署到 Jetson。
