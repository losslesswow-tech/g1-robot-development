# G1 导航容器安全覆盖层

本目录把现有 `robot-navigation:2.7` 导航镜像固定到已检查的关键文件指纹，并覆盖其默认
入口和配置，使其成为 G1 Jetson ROS1 桥接的**只读消费者**。Unitree SDK2
仍只属于 Jetson 上的 `g1-bridge:arm64`，不会复制到导航镜像。

> 此覆盖层仍是 `linux/amd64`，只适用于当前外置 x86_64 导航方案。未来把
> 导航和 Bridge 一起放到 G1 Jetson 时，必须先把导航本体及 `/nav` 下的专有
> 二进制真正重编译为 `linux/arm64`；禁止在 Jetson 上用 QEMU 模拟运行本镜像。
> 入口脚本会检测这种情况并拒绝启动。

## 固定契约

| 项目 | 值 |
|---|---|
| 导航容器 | Ubuntu 20.04 / amd64 / ROS Noetic |
| ROS Master | `http://192.0.2.6:11311` |
| 导航节点公布地址 | `192.0.2.6` |
| Jetson bridge | `192.0.2.8`，`DDS_INTERFACE=eth0` |
| G1 MCU/DDS | `192.0.2.10`，`ROS_DOMAIN_ID=0` |
| 运动开关 | `ENABLE_MOTION=false` |
| 自动启动 | `AUTO_START=false`、`NAVIGATION_AUTO_START=false` |
| 速度输出 | `ALLOW_CMD_VEL=false`；配置重定向到 `/g1_navigation/blocked_cmd_vel` |

入口脚本会拒绝改变上述安全值，也会拒绝 Foxy/ament/colcon 环境混入。
`/run.sh` 已改为安全入口，不再沿用基础镜像中自动启动 Web 控制器的行为。

## 构建与离线检查

私有仓库无法在匿名状态下按 digest 解析，因此 Dockerfile 使用本地 `2.7`
标签，并在覆盖文件前校验 `/nav` 三个二进制和原 `/run.sh` 的 SHA-256；标签
漂移会直接令构建失败。本机已有镜像时不需要拉取网络内容：

```bash
cd navigation
docker compose build
docker compose run --rm navigation image-check
```

`image-check` 只检查镜像、ROS Noetic、catkin 包和安全开关，不连接机器人。
完整的离线安全回归（包括四个 `true` 覆盖拒绝测试）可执行：

```bash
bash tests/smoke.sh
```

## 网络预检

完整 ROS1 不只使用 Master 的 11311 端口。节点还会公布动态 XMLRPC/TCPROS
端口，因此运行导航进程的同一网络命名空间必须实际拥有
`192.0.2.6`：

```bash
docker compose run --rm navigation preflight
```

若容器内没有 `.10`，命令会失败，而不是用 Docker Desktop 的
`192.0.2.x` 地址继续运行。截至 2026-09-22 的本机核对结果是：`.10`
属于 Windows 的物理以太网；默认 WSL 发行版只看到 `eth5=192.0.2.11`
和 `eth6=192.0.2.12`。因此当前 `portproxy` 足以暴露临时 ROS Master，
但**不等于**完整导航已具备双向 ROS1 网络条件。

同日的选路复核还显示，Windows/WSL 到 `.161`、`.164` 的实际选择被
`FlClash` 默认路由接管，WSL 显示经 `eth6 -> 192.0.2.13`。因此经该路径
得到的 TCP 成功不能作为现场网直连证据；需要在不受隧道路由影响的网络
命名空间重新完成 Jetson/MCU 连通性验收。

## 安全保持与只读观察

以下命令只让容器保持运行，不启动导航、SLAM、规划器、雷达驱动或机器人
控制：

```bash
docker compose up -d navigation
```

只有在 `preflight` 通过后，才可执行一次只读 ROS 图检查：

```bash
docker compose run --rm navigation observe
```

`observe` 只调用 `rosnode list`、`rostopic list` 和对
`/sdk/bridge_status` 的单次读取；不会发布消息。任何后续启用规划器、雷达或
运动链路的工作都属于新的现场阶段，不在本覆盖层的自动启动范围内。

## 已知基础镜像风险

基础镜像的 Web 层包含硬编码的凭据样式常量。本覆盖层会在运行时用空值／
环境变量版本替换对应模块，但无法从父镜像历史层中删除原内容。若这些凭据
曾经有效，应在提供方侧轮换；不要把父镜像或其层导出到不受信环境。
