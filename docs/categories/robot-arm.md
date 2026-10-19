# 机械臂

| 模块 | 范围 |
| --- | --- |
| [arm-button](../../arm-button/README.md) | 官方几何、右臂运动学、指尖高度预览、动作组界面 |
| [arm-debug](../../arm-debug/README.md) | 右臂七轴调试、只读叠加、单次目标、原生 worker |
| [arm-teach-play](../../arm-teach-play/README.md) | 示教与回放会话、轨迹存储、重力模型和服务接管适配 |
| [dreamcontrol-button](../../dreamcontrol-button/README.md) | 固定版本 DreamControl、离线仿真与按钮动作控制台 |

`native/`、`control/`、`readonly/` 和 `session/` 中保留相应 C++/Python 实现。
三段脱敏后的录制保留在根目录 `evidence/recordings/`，日志样本见 `evidence/README.md`；
运行配置改为示例，录制不自动载入活动控制目录。
DreamControl 使用研究许可，具体约束以其随附 LICENSE 为准。
