# G1 Robot Development

宇树 G1 机器人开发源码，按 **灵巧手、机械臂、导航** 三类整理。
这是经过脱敏的源码快照。所有网络地址、账号、主机路径和部署配置均为示例；
不包含原始工作区、Git 历史或可直接复用的现场凭据。

| 类别 | 模块 | 开发内容 |
| --- | --- | --- |
| [灵巧手](docs/categories/dexterous-hand.md) | `hand-teleop/` | Inspire RH56DFTP 六通道浏览器控制、姿态映射、单次发送、导航任务手势 |
| [机械臂](docs/categories/robot-arm.md) | `arm-button/`、`arm-debug/` | 右臂几何预览、运动学计算、逐关节调试及控制接口 |
| [机械臂](docs/categories/robot-arm.md) | `arm-teach-play/` | 示教、录制与回放源码、连续轨迹、重力模型 |
| [机械臂](docs/categories/robot-arm.md) | `dreamcontrol-button/` | DreamControl 离线仿真适配、动作组控制台、原生传输 |
| [导航](docs/categories/navigation.md) | `navigation/`、`bridge/` | ROS1 导航容器适配、G1 DDS/ROS 桥接、启动门禁 |
| [导航](docs/categories/navigation.md) | `g1-robot-arrival-voice/` | 到点语音、任务接入、网络诊断、雷达 IMU 补丁与模型资源 |

源码保留原有相邻目录关系，避免分类时破坏模块间的相对引用。
`tools/` 提供与灵巧手相关的维护和离线检查工具。

## 本地离线预览

Python 3.8+，在仓库根目录执行；灵巧手和机械臂预览使用项目随附的 Three.js：

```bash
python hand-teleop/server.py
```

对应地址为 `http://127.0.0.1:8768`。机械臂预览入口见各模块说明。
示例中的局域网地址已统一替换为文档网段，不能作为真实设备地址使用。
历史说明仅反映开发时的记录，不代表当前设备状态或部署验收结果。

## 外部依赖

- SDK2、ROS、Docker 和编译器由使用环境提供；不随本仓库打包。
- `navigation/` 需要使用者提供有权访问且与其校验约束匹配的导航基础镜像。
  发布副本只提供脱敏的镜像占位名称，不包含原有私有镜像或导航规划器实现。
- DreamControl 策略权重未打包。`dreamcontrol-button/fetch_policy.py` 提供固定版本、
  大小及 SHA-256 校验的下载入口；仿真依赖见该模块的 requirements 文件。
- 实机运行配置已转换为 `*.example.json`，必须由使用者自行配置。
- [evidence/](evidence/README.md) 保留一张遮盖机构标识的现场照片、三段完整动作录制，
  以及脱敏的运行日志。高频传输日志保留首尾样本，截取方式见该目录的清单。
- 音频、模型权重、备份、容器镜像归档和镜像内的可执行程序未发布；
  导航容器只保留解析整理后的源码、配置模板与说明。

本次整理只进行源码发布与离线验证，不代表实机动作或导航验收。
项目已有的动作范围、接管、停止和失联保护说明保留在各模块中。

## 来源与脱敏记录

第三方组件与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
脱敏范围见 [SANITIZATION.md](SANITIZATION.md)，发布文件校验见
[release-manifest.json](release-manifest.json)。原模块中的哈希属于历史来源记录；
经过脱敏修改的文件以本次发布清单为准。
