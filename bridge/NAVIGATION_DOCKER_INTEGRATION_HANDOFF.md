# G1 桥接与导航 Docker 配置交接

本文档用于将 G1 ROS1/DDS 桥接容器与导航 Docker 对齐。当前验证范围是只读遥测，禁止通过本配置启动站立、行走或其他执行器动作。外置导航是当前可用拓扑；导航和 Bridge 同放到 Jetson 是后续目标拓扑。

## 1. 网络拓扑

| 节点 | 系统/架构 | 接口 | 地址 | 用途 |
|---|---|---|---|---|
| Jetson G1 主机 | Ubuntu 20.04 / aarch64 | `eth0` | `192.0.2.8/24` | 运行 `g1-bridge:arm64` |
| G1 MCU/DDS 端 | 现场设备 | — | `192.0.2.10` | Unitree DDS 数据源 |
| 导航/ROS 主机 | Windows + WSL Ubuntu-24.04 | Windows 物理以太网 | `192.0.2.6/24` | ROS1 Master 入口；当前并非 WSL 内地址 |
| 其他 WSL 接口 | WSL | `eth5`/`eth6` | `192.0.2.11`/`192.0.2.12` | 不用于 G1 DDS/ROS 主链路 |

目标 ROS1 Master 地址固定为：

```text
http://192.0.2.6:11311
```

未来同机部署时改为：

```text
ROS_MASTER_URI=http://192.0.2.8:11311
ROS_IP=192.0.2.8       # 导航与 Bridge 相同
network_mode=host            # 两个容器都必须使用
```

同机不会消除 ROS1 的动态 XMLRPC/TCPROS 端口，但 host 网络避免了两个容器
之间的 NAT/端口映射问题。当前导航镜像及 `/nav` 专有程序仍是 amd64；在
获得真正的 ARM64 构建前，不得把它作为 Jetson 同机方案部署，也不得用 QEMU
作为生产替代。

Jetson 已确认到 MCU 的路由为：

```text
192.0.2.10 dev eth0 src 192.0.2.8
```

## 2. 桥接镜像与运行参数

桥接镜像：

```text
g1-bridge:arm64
```

镜像已在 Jetson 导入，架构为 `linux/arm64`。桥接容器使用 host 网络，启动参数必须保持如下值：

```text
ROS_MASTER_URI=http://192.0.2.6:11311
ROS_IP=192.0.2.8
DDS_INTERFACE=eth0
ROS_DOMAIN_ID=0
ENABLE_MOTION=false
AUTO_START=false
MOTION_LOCKED=true
DEPLOYMENT_MODE=external_navigation
```

桥接镜像构建信息：

- 使用 Unitree 官方 `unitree_sdk2`，固定提交 `c753829882fba461ed07ba25aaabee0a25d83663`。
- 仅验证并安装 `aarch64` SDK/DDS 库；不接受 amd64 库混入 ARM64 镜像。
- `BUILD_CONTROL_TOOLS=OFF`，默认不编译 `g1_control`/`g1_arm` 执行器工具。
- 导航 Docker 不需要复制 `unitree_sdk2/`；SDK 只属于 Jetson 侧 DDS 桥接镜像。

安全含义：

- `ENABLE_MOTION=false`：不创建运动客户端，不订阅 `/cmd_vel`。
- `AUTO_START=false`：启动和重启不调用 `StandUp` 或 `Start`。
- `DDS_INTERFACE=eth0`：Unitree DDS 只走 Jetson 的现场网卡。
- `ROS_DOMAIN_ID=0`：必须与桥接使用的 DDS 域保持一致。

推荐的 Jetson 启动命令：

```bash
docker run -d \
  --name g1-bridge-safe \
  --network host \
  --restart no \
  --read-only \
  --tmpfs /tmp \
  --tmpfs /root/.ros \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -e ROS_MASTER_URI=http://192.0.2.6:11311 \
  -e ROS_IP=192.0.2.8 \
  -e DDS_INTERFACE=eth0 \
  -e ROS_DOMAIN_ID=0 \
  -e ENABLE_MOTION=false \
  -e AUTO_START=false \
  g1-bridge:arm64
```

如果容器已经存在，不要再次 `docker run`；先用 `docker inspect` 核对环境变量。

## 3. 对外提供的 ROS 话题

桥接节点的 ROS1 输出为：

```text
/sdk/imu             sensor_msgs/Imu
/sdk/bridge_status   std_msgs/String（latched）
```

Jetson 上查看：

```bash
docker exec -it g1-bridge-safe bash -lc \
  'source /opt/ros/noetic/setup.bash && source /opt/g1_bridge/setup.bash && rostopic list | grep "^/sdk/"'
```

```bash
docker exec -it g1-bridge-safe bash -lc \
  'source /opt/ros/noetic/setup.bash && source /opt/g1_bridge/setup.bash && rostopic echo /sdk/imu'
```

若 `/sdk/bridge_status` 存在但 `/sdk/imu` 不存在，优先检查 DDS lowstate、`eth0` 和 `ROS_DOMAIN_ID`，不要开启运动模式。

历史检查中，Jetson 上的 `g1-bridge-safe` 容器曾存在并运行，但执行
`rostopic echo /sdk/imu` 时得到过 `topic does not appear to be published yet`。
这不是当前在线状态证明，也说明不能把 IMU 数据链路视为已验收；必须以现场
DDS lowstate 和实际收到的 `/sdk/imu` 为准。不要以“容器处于 Up”代替话题验收。

## 4. 导航 Docker 必须修改的配置

导航 Docker 是 ROS1 消费者，不需要 `unitree_sdk2`，也不应直接操作 Unitree DDS。导航容器应至少设置：

```text
ROS_DISTRO=noetic
ROS_MASTER_URI=http://192.0.2.6:11311
ROS_IP=192.0.2.6
```

容器启动时必须加载：

```bash
source /opt/ros/noetic/setup.bash
```

不要混用 Foxy 环境变量或路径，例如不要在同一进程环境中同时保留 `ROS_DISTRO=foxy` 和 Noetic 的 `CMAKE_PREFIX_PATH`/`PYTHONPATH`。

### 网络模式要求

ROS1 不仅使用 Master 的 `11311` 端口，还会让每个节点通过 XMLRPC/TCPROS 动态端口互相直连。因此：

1. 导航容器必须能访问 `192.0.2.6:11311`。
2. 导航节点公布的 `ROS_IP` 必须是 Jetson 能访问的地址。
3. Jetson `192.0.2.8` 必须能反向访问导航节点公布的 XMLRPC/TCPROS 端口。
4. 仅映射 `11311` 不足以支持完整导航数据流。

如果 Docker 引擎运行在真正拥有 `192.0.2.6` 的 Linux/WSL 网络命名空间中，可使用 host 网络，并设置 `ROS_IP=192.0.2.6`。2026-09-22 的重新核对显示 `.10` 属于 Windows 物理以太网，默认 WSL 发行版只看到 `eth5=192.0.2.11` 与 `eth6=192.0.2.12`；Docker Desktop 容器也不拥有 `.10`。因此不能把 `ROS_IP=192.0.2.6` 直接写进普通 Docker Desktop bridge/host 容器并期待 ROS1 双向通信正常。

导航侧现已增加 `navigation/` 安全覆盖层：它固定 ROS Noetic 和上述地址，默认只进入安全保持状态，并在 `preflight` 中要求运行网络命名空间实际拥有 `.10`。在该检查通过前，不启动导航、SLAM、规划器或传感器驱动。

同次核对还发现 Windows `Find-NetRoute` 与 WSL `ip route get` 对 `.161`/`.164` 的实际选路被 `FlClash` 接管；WSL 路径为 `eth6 -> 192.0.2.13`，不是现场以太网。经该路径得到的 TCP 成功不能作为 Jetson 直连验收结果。在恢复直接现场选路前，不继续 DDS/ROS 动态端口或话题验收。

## 5. 当前 ROS Master 的临时验证方案

当前主机上的临时 `ros-master-noetic` 使用导航镜像启动，仅用于联调：

- 容器内部使用可解析的容器主机名启动 `roscore`。
- Docker 后端端口映射到 Windows 本机回环地址。
- Windows `portproxy` 将 `192.0.2.6:11311` 转发到本机回环端口。
- 防火墙规则仅允许 Jetson `192.0.2.8` 访问 TCP `11311`。

本轮采用的端口关系（若维护者重新创建临时 Master，应保持同样逻辑）：

```text
Docker 容器内 roscore: 11311
Windows localhost:     127.0.0.1:11312 -> container:11311
WSL/现场入口:           192.0.2.6:11311 -> 127.0.0.1:11312
```

检查端口转发：

```powershell
netsh interface portproxy show v4tov4
```

预期看到：

```text
192.0.2.6  11311  127.0.0.1  11312
```

这套方案可用于当前只读桥接验证，但不是完整导航的最终网络设计。完整导航需要一个直接拥有 `.10` 地址、并能处理 ROS1 动态 TCPROS 端口的运行环境。

验证命令：

```powershell
Test-NetConnection -ComputerName 192.0.2.6 -Port 11311
```

Jetson 侧：

```bash
timeout 3 bash -c '</dev/tcp/192.0.2.6/11311' \
  && echo "ROS Master 11311 OPEN" \
  || echo "ROS Master 11311 CLOSED"
```

## 6. 导航容器侧验收清单

导航 Docker 维护者修改后，先只做以下检查，不启动运动相关节点：

```bash
echo "$ROS_DISTRO"
echo "$ROS_MASTER_URI"
echo "$ROS_IP"
ip -br -4 addr
```

应确认：

- 使用 ROS Noetic。
- `ROS_MASTER_URI` 为 `http://192.0.2.6:11311`。
- `ROS_IP` 是 Jetson 可达的导航主机地址。
- 不存在 Foxy/Noetic 混合环境。
- 从导航容器可以访问 Master。
- 从 Jetson 可以访问导航节点的 XMLRPC/TCPROS 端口。

只读话题验证：

```bash
rostopic list | grep '^/sdk/'
rostopic echo -n 1 /sdk/bridge_status
rostopic hz /sdk/imu
```

## 7. 现场安全边界

- 默认保持 `ENABLE_MOTION=false`、`AUTO_START=false`。
- 不启动 `g1_control`、`g1_arm` 或其他执行器工具。
- 不向 `/cmd_vel` 发布消息。
- 在 ROS Master、网络路由和话题注册确认前，不启动完整导航任务。
- 若日志出现 DDS 接口、ROS Master 或节点反向连接错误，先停止桥接并修复网络，不通过打开运动开关绕过问题。

## 8. 同机最终验收

ARM64 导航镜像就绪并在 Jetson 启动 roscore 后，使用
`docker-compose.colocated.yml` 启动 Bridge，然后在宿主机执行：

```bash
BRIDGE_CONTAINER=g1-bridge-safe \
NAVIGATION_CONTAINER=<ARM64导航容器名> \
TOPOLOGY=colocated_jetson \
REQUIRE_IMU=true \
bash scripts/verify_bridge_navigation_link.sh
```

脚本通过表示导航侧实际读取了 Bridge 状态和一帧 IMU，且两边的 Master、
地址、架构及安全锁一致；在机器人离线或 ARM64 导航镜像缺失时不能提前宣称
该现场结果已通过。
