# 导航

| 模块 | 范围 |
| --- | --- |
| [navigation](../../navigation/README.md) | ROS1 导航容器覆盖配置、网络检查及启动门禁 |
| [bridge](../../bridge/README.md) | ARM64 DDS/ROS 桥接、SDK 获取及架构检查 |
| [g1-robot-arrival-voice](../../g1-robot-arrival-voice/README.md) | 到点播报、任务步骤接入、雷达修正及网络方案 |

第三个模块中的 `arrival_voice/` 和 `navigation_task/` 是语音及任务扩展；
`wifi_access/`、`network_modes/` 是网络检查与回退方案；`nav/livox_patch/`
是雷达驱动适配。该模块内的 `bridge/` 属历史基线，根目录 `bridge/`
有独立的构建及默认行为说明，两者不能直接互换。

导航基础镜像、规划器二进制及完整厂商 SDK 未发布。历史网络拓扑中的地址已改为示例，
未获得的规划器源码不在本仓库中。
