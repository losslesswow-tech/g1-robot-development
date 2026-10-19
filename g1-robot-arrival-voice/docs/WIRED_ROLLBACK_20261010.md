# 2026-10-10 恢复有线运行记录

用户要求回到“我原本通过网线连接，现在想拔了网线通过wifi连接”之前的状态。当前选择为原有有线方案；不要依据早先无线需求重新启动无线检查或候选桥接。

## 已完成

- 现场 `enp132s0` 已有原地址 `192.0.2.6/24`（另有既存 `192.0.2.20/24`）；Wi-Fi `wlp131s0f0` 只有 `192.0.2.5/24`，没有测试别名 `.123.10/32`。
- 临时无线测试此前已有两端 `rolled_back / remaining: []` 回执；当前未发现无线测试或候选桥接进程。容器中的 Livox 试运行记录为 `restored: true`。
- 安装的雷达程序及当前雷达进程均为原版，SHA256 `af79e6768301142b8fe935693762dd8a5edc5749d157da122dccf4c3e0a95311`。没有重装或重启旧运动桥接。
- 雷达配置仍为 `.123.120 → .123.10`，SHA256 `68bf8e12390bed9d9eedfcad5d8fa60da20ffa7d67ccb7d2da3664dac0751914`。
- `task.py`、`g1_task_voice.py`、任务前端 JS/HTML 与提出无线切换前安装的任务语音版本清单逐项一致；地图 1009 文件保留。
- 容器内以下入口已备份并改为提示停用后退出（退出码 78）：`/tmp/g1-wifi-check-start-20261010.sh`、`/tmp/g1-wifi-lidar-check-start-20261010.sh`、`/tmp/g1-network-modes-prepare-20261010.sh`。
- 现场备份及回执：`/opt/g1-arrival-voice/state/disabled-wireless-20261010T065516Z/`。
- 本轮没有修改网络规则，没有重启进程，没有发送运动或导航指令。离线候选源码与历史包仅留档，不代表正在使用。

## 验证及边界

恢复有线后只读观察 10 秒：机器人 IMU 10402 条、Livox IMU 1999 条、Livox 点云 100 条；未观察到非零速度指令。

最后只读观察 5 秒，`/x_nav/global_map`、`/x_nav/current_pointcloud`、`/base_link/odom` 均未收到消息。导航状态为“导航模式 请重新设置位姿（重定位）”，任务为“无任务”。因此有线传感器链路已验证，地图显示、定位及导航播报尚不能宣称恢复通过。这是运行配置的核对及停用无线入口，不是完整操作系统快照还原。

本地证据位于 `.local-voice/hardware/wired-rollback-before-20261010.json`、`wired-rollback-sensors-20261010.json`、`wired-rollback-receipt-20261010.json`、`wired-rollback-map-20261010.json`。

## 后续地图显示恢复

用户随后报告点击切换地图仍无点云。只读排查发现：`get_map` 刷新成功且 ROS 已发布 194189 字节地图数据，但 Foxglove 的 nodelet 列表为空，8765 端口未监听。仅 roslaunch 与 nodelet manager 进程存活，原进程状态页面因此仍标为正常。

通过原有进程管理接口仅停止/启动 `foxglove_node`，未重启导航 Web、SLAM、规划器、雷达或运动桥接。随后确认 `/foxglove_bridge` 已加载、`0.0.0.0:8765` 正常监听；前端重新连接后刷新地图数据，已目视确认彩色地图点云显示。再次核对端口仍正常监听。

此操作恢复的是显示链路；仍需现场按机器人实际位置完成重定位，未执行导航或运动验收。桥接历史日志曾有退出/崩溃记录，本次未修改厂商桥接源码或宣称根除所有历史崩溃原因。证据：`wired-map-refresh-20261010.json`、`wired-map-websocket-20261010.json`、`wired-map-bridge-restarted-20261010.json`。
