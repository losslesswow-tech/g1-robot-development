# 背负电脑开发环境（2026-10-14）

已阅读对话《克隆机器人开发容器环境》及迁移最终接管记录。当前开发以
`environments/backpack-20261013.json` 为配置；旧机地址、旧网卡和 `/tmp`
编译目录仅保留为历史证据。迁移期间曾暂停运动，之后用户明确恢复受限试动；
后续停服及试动结果见下方更新。常规部署版本继续禁用实物试动入口。

| 项目 | 当前配置 |
|---|---|
| 管理 SSH | `operator@192.0.2.1`，严格校验已知主机身份 |
| G1 DDS | `eno1`，domain 0，订阅 `rt/lowstate` |
| ROS | `ROS_IP=192.0.2.2`，Master `http://192.0.2.2:11311` |
| 导航容器 | `robot-navigation-example` |
| 桥接容器 | `g1-bridge-migrated-20261013` |
| 宿主机 / 开发运行时 | Ubuntu 22.04 / 容器内 Ubuntu 20.04、ROS Noetic、Python 3.8、x86_64 |
| 已安装 SDK | `unitree_sdk2` CMake 包 2.0.0；不是固件版本 |
| 离线三维预览 | <http://192.0.2.1:8000/g1-arm/> |

## 适配方式

`environment.py` 只提供配置校验、环境检查、部署编译、离线数学自检、被动
状态观察及模式查询。没有动作转发入口，也不启动、重启或注册任何服务。
SSH 私钥由 `--ssh-key` 指定，内容不会加入源码、配置、日志或部署包。

```powershell
# 在 arm-button 目录运行；Python 3.8+。
python environment.py validate
python environment.py check --ssh-key <本机已有私钥路径>
python environment.py deploy --ssh-key <本机已有私钥路径>
python environment.py self-test --ssh-key <本机已有私钥路径>
python environment.py observe --seconds 3 --ssh-key <本机已有私钥路径>
python environment.py query-mode --ssh-key <本机已有私钥路径>
python environment.py service-query --ssh-key <本机已有私钥路径>
```

部署前校验活动容器的 ROS 地址、host 网络、网卡地址及既有只读开发挂载。
源码逐文件 SHA-256 校验后放入宿主机持久目录：

```
/home/operator/g1-migration-20261013/cutover/development/arm-button/releases/<内容版本>/
  source/         独立源码与离线网页副本
  bin/            新环境编译的四个可执行文件
  manifest.json   源码摘要
  build.log       编译日志
```

容器内对应 `/opt/g1-development/arm-button/releases/<内容版本>/`。
`current.json` 只记录开发版本，不是自启或执行任务配置。
构建工作区位于容器 `/var/tmp/g1-arm-build-<内容版本>`，可重建；最终二进制在
宿主机保存，容器重建后仍可通过已有挂载读取。现有导航静态预览挂载单独保留。

控制工具的网卡必须显式传入，例如 `--inspect eno1`；不再回退旧网卡。
默认 CMake 选项 `G1_ENABLE_APPROVED_TRIAL=OFF`，部署也明确传入 OFF。
该版本会在初始化 ROS/DDS 前拒绝物理试动入口。修改编译选项不构成动作授权。
本轮只编译控制工具并运行其不连接 ROS/DDS 的数学自检。

## 内置手臂动作服务前提（2026-10-14 后续更新）

用户提供宇树官方说明：自行通过 arm_sdk 开发动作前须关闭 `g1_arm_example`。
已使用其指定的 `unitree::robot::b2::RobotStateClient`，服务端与客户端 API 均为
1.0.0.2；这是G1官方示例沿用的SDK命名空间。本机实际发布话题是 `rt/arm_sdk`。

`control/g1_arm_service.cpp` 默认查询；显式 `--stop eno1` 仅允许关闭固定名称
`g1_arm_example`，校验版本、保护位及 FSM，发一次关闭请求后限时查询实际服务状态。
本次实测关闭回执为 ret=0/status=1；ServiceList稍后才从0变为1，所以回执本身不够。
只读列表确认停止后，内置动作列表接口返回3104，FSM查询仍成功。没有修改开机服务配置。

受限试动工具现在于发布前和运行期间检查该服务保持停止；未知、重新运行或查询失败均拒绝/中止。
环境适配器新增 `service-query` 纯查询，不自动关闭或恢复服务。
模式工具默认只查 FSM；如显式加 `--include-actions`，才查询内置动作列表。
停服后动作列表接口不可用，不应把它误判为 FSM 失联。

关闭后2°诊断仍在接管阶段因左腕19号瞬时速度0.467864156 rad/s超过0.30触发保护；
未进入肩部前摆，已经发送释放指令并退出。这说明停服前提已补齐，腕部过渡仍需排查。
未放宽阈值或改变增益继续试动。详见工作区 `artifacts/arm-service-20261014`。

## 验证边界

2026-10-14 再次更新：用户明确要求忽略自定义0.30 rad/s手臂速度阈值后，
增加显式 `--record-arm-speed`（只记录超限，仍检查有限数值与电机故障）。
2°前摆、返回和释放诊断成功；该次本身没有记录到手臂速度超限，不能把成功归因于忽略阈值。
随后以约5°/秒指令峰值、四段轨迹尝试向前平举；右肩实际前摆约15°时因躯干角速度检查中止。
用户现场报告出现向前行走状态，故停止进一步实物尝试。
已发送三次G1 StopMove零速度RPC（全部返回0），用户确认已经停止并站稳。
身体/腿部运动检查未放宽；默认部署仍运动OFF，实物重试入口记录了事故锁定。
平衡模式查询返回7301（LocoState not available），平衡模式未知，不能据此声称连续踏步开启或关闭。
新增被动IMU、腿部关节速度和角度范围观测，证据见 `artifacts/arm-horizontal-20261014`。
平举仍未完成；需排查运控与手臂接管、负载/平衡或外部速度指令之间的关系。

2026-10-14 首段复测更新：用户要求再测一次后，新增独立
`--execute-approved-first-stage`，只重复原轨迹15%的首段，指令峰值约5°/秒；
发送零速度保持，新增腿部关节相对初始角1°的检查，保留身体及其余保护。
联锁自检覆盖正常状态、超位移、超速度、电机标志和NaN拒绝。
复测右肩实际前摆约15.06°，左踝4号偏移约1.08°后中止。
用户确认这次仍有向前迈步，随后已经停止并站稳。程序记录70次零速度调用，
结束后另发三次StopMove均返回0；请求成功不等于实测身体速度为0。
首段复测与完整平举脚本均保留事故锁定，不自动重试、不切换FSM。
证据见 `artifacts/arm-repeat-20261014`，因果关系尚未确定。

新主机的当前 FSM 必须通过新查询获取，不能复用旧机 FSM 501 的历史结果。
只读观察没有发布器；模式查询仅使用 Get 接口。关节角为 rad，角速度为 rad/s。
离线模型按直立躯干 X 前、Y 左、Z 上解释；肩顶到轴心及指尖接触点仍待标定。

旧只读工具曾在 DDS 退出阶段出现断言。本次调整为先解除原生监听器、关闭
reader，再销毁 SDK 队列和释放工厂；有限次数退出验证不能证明长期稳定性。
必须同时检查进程退出码、状态新鲜度及 CRC 拒收数，不能只看一行输出。

此前 2° 试动在接管阶段被保护中止，未完成向前抬臂；该问题尚未完成根因定位。
迁移和只读验收均不代表平举、按按钮或自动行走已通过实物验收。
