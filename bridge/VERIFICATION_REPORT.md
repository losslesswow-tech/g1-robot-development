# ARM64 G1 Bridge 验证记录

验证日期：2026-09-23

## 构建产物

- 镜像：`g1-bridge:arm64`
- 镜像架构：`linux/arm64`
- 镜像 ID：`sha256:9c5cf42838b15bd83f2b4f7c9c156a1fef9ea0c9cb4f76e3ace31cefe8d714e4`
- Unitree SDK2 commit：`c753829882fba461ed07ba25aaabee0a25d83663`
- 导出包：`g1-bridge-arm64-colocated-safe.tar`
- 导出包 SHA-256：`6640640be49e545b16b284287054492e22ec46a94096b8be4bb4abfc510a0000`

## 已通过

- SDK 静态库、CycloneDDS 动态库和 Bridge 可执行文件均由 `readelf`
  确认为 AArch64。
- 最终可执行文件 `ldd` 无 `not found` 依赖。
- ROS 发行版为 Noetic。
- 外置导航配置通过 `image-check`：Master `.10:11311`、Bridge `.164`。
- 未来同机配置通过 `image-check`：Master/Bridge 均使用 `.164`。
- `ENABLE_MOTION=true`、`AUTO_START=true` 在 `MOTION_LOCKED=true` 时均被拒绝。
- 同机模式下 Master 地址与 `ROS_IP` 不一致会被拒绝。
- 基础 Compose 和同机覆盖 Compose 均可成功解析。
- 离线 ARM64 容器联调通过：临时 ROS Master、只读 Bridge、导航观察容器
  使用 host 网络；导航侧实际读取到 `/sdk/bridge_status`，识别
  `/sdk/imu` 为 `sensor_msgs/Imu`，并确认 Bridge 节点没有 `/cmd_vel`
  订阅。
- smoke 结束后临时容器自动删除。

复现命令：

```bash
cd bridge
bash tests/smoke.sh
```

## 尚未声称通过

- 未在本轮让机器人执行站立、行走或任何执行器动作。
- 未在本轮现场确认 Jetson `eth0=192.0.2.8/24`、MCU `.161` 路由
  或 DDS lowstate 数据。
- 未收到真机 `/sdk/imu` 样本；真机验收必须使用
  `REQUIRE_IMU=true scripts/verify_bridge_navigation_link.sh`。
- 当前 `navigation/` 镜像仍为 amd64，不能部署到 Jetson；未来同机方案还
  需要导航本体及 `/nav` 专有程序的原生 ARM64 构建。

因此当前可确认的是：ARM64 Bridge 镜像和同机 ROS1 通信契约已完成并通过
离线容器联调。只有 ARM64 导航镜像和机器人同时在线、且现场验收脚本收到
IMU 后，才能确认完整的“导航容器 ↔ Bridge ↔ G1 DDS”链路。
