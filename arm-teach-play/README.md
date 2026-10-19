# G1 拖动示教项目部署记录

> 后续录制准备：已增加独立的[本机录制入口](session/README.md)，支持双臂连续角度与关键点记录。原版源码保留；下文描述原始部署。入口已完成只读准备，尚未进入低阻尼。

部署日期：2026-10-15，Australia/Sydney。

已部署 [lez666/g1-arm-teach-play](https://github.com/lez666/g1-arm-teach-play) 原项目，锁定提交 `28ef85a7198eab82115087f8b3c82fa9f49ff961`，MIT 许可。原源码保存在 `vendor/28ef85a7198eab82115087f8b3c82fa9f49ff961`，未修改。

本轮完成下载、校验、传输、独立构建与二进制依赖检查。没有启动 teach/play/watchdog/tts_probe，没有接入机器人 DDS，没有发送运动指令，也没有注册开机服务。

## 部署位置

| 项目 | 实际部署 |
|---|---|
| 开发机 | `192.0.2.1`，Ubuntu 22.04，x86_64 |
| 构建与运行容器 | `robot-navigation-example`，Ubuntu 20.04，x86_64 |
| 编译工具 | GCC 9.4.0，CMake 3.16.3，C++14 |
| SDK | 使用容器既有 `unitree_sdk2` CMake 包与配套 DDS 库，不安装替代版本 |
| 宿主机持久目录 | `/home/operator/g1-migration-20261013/cutover/development/arm-teach-play/releases/28ef85a7198e` |
| 容器挂载目录 | `/opt/g1-development/arm-teach-play/releases/28ef85a7198e` |
| 编译临时目录 | `/var/tmp/g1-arm-teach-play-28ef85a7198e` |
| 二进制 | 持久目录下 `bin/teach`、`bin/play`、`bin/teach_with_name`、`bin/play_with_motions`、`bin/watchdog`、`bin/tts_probe` |

容器已有开发目录挂载用于持久化；没有重启容器或修改挂载。二进制针对容器环境构建，未在宿主机直接运行或验收。

## 构建适配

上游 CMake 固定引用 `thirdparty/lib/aarch64`。本机为 x86_64，因此另建 `deployment/CMakeLists.txt`，通过 `find_package(unitree_sdk2 REQUIRED CONFIG)` 链接本机 SDK 导入目标。它只改变构建集成，不改变任何电机控制参数或源码行为。

仅重新构建的命令如下，不会执行项目控制程序：

```bash
docker exec robot-navigation-example cmake \
  -S /opt/g1-development/arm-teach-play/releases/28ef85a7198e/deployment \
  -B /var/tmp/g1-arm-teach-play-28ef85a7198e \
  -DUPSTREAM_SOURCE=/opt/g1-development/arm-teach-play/releases/28ef85a7198e/vendor/28ef85a7198eab82115087f8b3c82fa9f49ff961 \
  -DCMAKE_BUILD_TYPE=Release

docker exec robot-navigation-example cmake \
  --build /var/tmp/g1-arm-teach-play-28ef85a7198e --parallel 2
```

## 验证结果

- 六个目标均编译成功，configure/build 返回码为 0。
- `readelf -h` 检查六个二进制；`ldd` 返回码为 0，无 `not found` 依赖。
- 原源码逐文件 SHA256 与下载清单校验，二进制哈希与完整构建日志保存在 [deployment.json](deployment.json)。
- 不执行上游二进制的 `--help` 测试：这些入口不是只读健康检查，启动即可能初始化 SDK、发布控制或触发音频操作。
- 初次本地构建清单因 Windows 换行造成哈希不符，已按实际文件字节修正，未改上游源码。初次传输超过远端单参数长度限制，已改用 SSH 标准输入；失败发生在远端部署前。日志仍留在本轮对话。

## 原版能力与规划差异

此处部署的是上游命令行项目，不代表已实现 [右臂连续示教规划](../research/g1-arm-kinesthetic-teach-replay-plan-20261015.md)。

| 项目 | 已部署原版行为 | 规划中的适配 |
|---|---|---|
| 录制 | 人工按 Enter 保存双臂 14 轴关键点 | 右臂七轴连续时间序列 + 关键点标记 |
| 拖动控制 | 同时放软双臂，腰部发送初姿保持 | 仅右臂拖动，其他有效字段按本机接口核实 |
| 补偿 | teach 源码 kp=0、kd=0.8、tau=0 | 核实固件与负载后决定重力补偿；不能保证松手悬停 |
| 回放 | play.cpp 的 INTERP_T=0.3 s，线性关节插值 | 依据实际速度/加速度限制重新定时 |
| 回正 | 上游按关键点反向返回 | 明确撤离按钮、恢复保存初姿、核对再交还 |
| 手指与按钮 | 未接入本项目已有伸指动作和按钮成功反馈 | 联动 RH56DFTP 既有姿态，接入任务反馈 |
| 入口 | 命令行 | 规划的网页示教/回放入口未实现 |

0.3 s 是本次锁定源码中的段时长；README 有不同描述，部署记录以实际源码为准。该值不是本机经过验证的实物速度。

原项目的二进制需要运行在有效 SDK/DDS 网络环境，并可能在启动后自动接管。当前仅安装与构建，不提供未经验证的真机启动验收结论。上游 watchdog 和 systemd 文件仅作为源码保存，没有启动或安装到系统服务目录。

已有接管后后倾、抬臂迈步的记录依然需要排查；本次没有解除原有中止记录。后续实机操作需清场、防倾保护、现场监护、遥控器与明确停止路径就绪，并从最小范围验证开始。

## 本地证据

- [源码清单](source-manifest.json)
- [构建与依赖结果](deployment.json)
- [最终文件核验](verification.json)
- 部署工具和日志：`../artifacts/g1-arm-teach-play-20261015/`

本部署没有新增机器人动作轨迹，也没有录制或回放人体/机器人动作。
