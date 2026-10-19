# Livox MID360 配置变更记录（2026-09-23）

## 实施状态

2026-09-23 已在导航主机 `operator-2204` 完成静态修改：

- 备份：`/home/operator/nav_map/MID360s_config.json.bak-20260923`
- `python3 -m json.tool`：通过（`JSON OK`）
- `diff`：仅包含计划中的三个字段
- 运行时重启与 ROS 话题验收：通过

2026-09-23 运行时实测：

| 话题 | 实测频率 | 结论 |
|---|---:|---|
| `/livox/lidar` | 约 `10.00 Hz` | Livox 点云持续发布 |
| `/livox/imu` | 约 `200.00 Hz` | Livox IMU 持续发布 |
| `/sdk/imu` | 约 `1041.7 Hz` | G1 DDS/ROS Bridge 跨机器持续发布 |

ROS Master `192.0.2.6:11311` 同时验证为可达。以上结果只验收传感器
通讯和 ROS 话题频率，不代表 TF、点云内容、定位质量、规划器或机器人运动控制
已经验收。

后续只读数据检查结果：

- `/livox/lidar` 的 `frame_id` 为 `lidar_link`。
- 单帧点数 `width=19968`、`height=1`、`point_step=26`。
- `row_step=519168`，与 `19968 × 26` 完全一致。
- `data` 长度为 `519168`，消息结构自洽；`is_dense=true`。
- `/robot/nav_odom` 稳定约 `30.00 Hz`。
- 导航容器最近十分钟日志未匹配到 `error`、`failed`、`exception` 或 `bind`。
- 静止状态下 `/robot/nav_odom` 使用 `nav_init` 父坐标系和 `base_link` 子坐标系。
- 同一静止帧的位置为 `(0, 0, 0)`，姿态四元数为 `(0, 0, 0, 1)`，线速度和
  角速度均为零；这与初始静止状态一致，但单帧恒零不能单独证明定位更新有效。
- `/robot/nav_odom` 实测平均时延约 `0–1 ms`、最大约 `3 ms`，时间戳与传输正常。
- `tf_echo nav_init base_link` 失败，TF 当前帧列表为空；`nav_init` 尚未被 TF
  broadcaster 发布。因此 TF 连通性未通过，导航运动控制仍不可验收。
- `/tf` 与 `/tf_static` 均已确认无发布者；列表中的订阅者只是诊断用
  `tf_echo`。
- `/robot/nav_odom` 的唯一发布者为 `/w_nav`，但 `/w_nav` 未声明发布 `/tf`。
  当前不能确定这是专有导航的预期设计还是启动项缺失，在检查活动启动文件和
  参数前不得补发固定 TF。

点云消息结构与频率已验收；点云几何正确性、里程计数值合理性和 TF 连通性
仍需分别检查。

## 目的与边界

本变更用于修复导航主机上的 Livox ROS Driver2 接收配置，使雷达
`192.0.2.14` 将点云和 IMU 数据发送到实际运行导航容器的主机地址
`192.0.2.6`。

本变更只涉及传感器网络接收配置，不修改 G1 DDS Bridge，不启用机器人运动，
不发布 `/cmd_vel`。G1 Bridge 必须继续保持：

```text
ENABLE_MOTION=false
AUTO_START=false
```

## 已观察事实

- 导航宿主机接口：`enp132s0`
- 导航宿主机现场地址：`192.0.2.6/24`
- Livox 地址：`192.0.2.14`，已通过 ping 与邻居表验证可达
- Livox ROS Driver2：`1.2.5`
- 当前故障：`bind failed`、`Init lds lidar failed`
- `/livox/lidar` 无发布者或无新消息，`/livox/imu` 未发布
- 当前配置错误地使用旧接收地址 `192.0.2.15`

## 文件位置

宿主机持久文件：

```text
/home/operator/nav_map/MID360s_config.json
```

导航容器中的挂载位置：

```text
/nav_map/MID360s_config.json
```

Docker 挂载关系：

```text
/home/operator/nav_map:/nav_map
```

应修改宿主机文件，不修改镜像层内文件。

## 修改内容

只修改 `MID360.host_net_info[0]` 中的三个值：

| 字段 | 修改前 | 修改后 |
|---|---:|---:|
| `host_ip` | `192.0.2.15` | `192.0.2.6` |
| `point_data_port` | `56300` | `56301` |
| `imu_data_port` | `56400` | `56401` |

修改后的完整 `host_net_info` 应为：

```json
"host_net_info": [
  {
    "lidar_ip": ["192.0.2.14"],
    "host_ip": "192.0.2.6",
    "cmd_data_port": 56101,
    "push_msg_port": 56201,
    "point_data_port": 56301,
    "imu_data_port": 56401,
    "log_data_port": 56501
  }
]
```

不得修改 `MID360.lidar_net_info`。雷达侧端口必须继续保持：

```text
cmd_data_port=56100
push_msg_port=56200
point_data_port=56300
imu_data_port=56400
log_data_port=56500
```

## 备份与实施

在导航宿主机 `operator-2204` 上执行：

```bash
cfg=/home/operator/nav_map/MID360s_config.json
backup=/home/operator/nav_map/MID360s_config.json.bak-20260923

if ! test -f "$cfg"; then
  echo "ABORT: config not found: $cfg" >&2
elif test -e "$backup"; then
  echo "ABORT: backup already exists: $backup" >&2
else
  python3 -m json.tool "$cfg" >/dev/null && \
  grep -q '"host_ip"[[:space:]]*:[[:space:]]*"192.0.2.15"' "$cfg" && \
  cp -a -- "$cfg" "$backup" && \
  cmp -- "$cfg" "$backup" && \
  echo "BACKUP OK: $backup"
fi
```

仅在终端显示 `BACKUP OK` 后，定点修改 `host_net_info`：

```bash
cfg=/home/operator/nav_map/MID360s_config.json
sed -i '/"host_net_info"[[:space:]]*:[[:space:]]*\[/,/"log_data_port"[[:space:]]*:[[:space:]]*56501/ {
  s/"host_ip"[[:space:]]*:[[:space:]]*"192\.168\.123\.171"/"host_ip": "192.0.2.6"/
  s/"point_data_port"[[:space:]]*:[[:space:]]*56300/"point_data_port": 56301/
  s/"imu_data_port"[[:space:]]*:[[:space:]]*56400/"imu_data_port": 56401/
}' "$cfg"
```

## 修改后静态校验

```bash
python3 -m json.tool "$cfg" >/dev/null && echo "JSON OK"
grep -A10 -B2 '"host_net_info"' "$cfg"
diff -u -- "$backup" "$cfg"
```

差异必须只包含上述三个字段。若有额外差异，不要重启容器，立即回滚。

## 运行时生效与验证

重启导航容器会重启 ROS Master。操作时保持机器人周围净空、有人监护且急停
可用；Bridge 仍必须保持禁动模式。

在导航主机执行：

```bash
docker restart -t 20 nav_robot
docker logs --since 2m nav_robot 2>&1 | grep -Ei 'livox|bind|socket|failed|error|success'
```

随后在 Jetson 上重新注册 Bridge：

```bash
docker restart -t 10 g1-bridge-safe
```

最后在导航主机验证：

```bash
docker exec nav_robot bash -lc '
source /catkin_ws/devel/setup.bash
timeout 10 rostopic hz /livox/lidar
'
```

```bash
docker exec nav_robot bash -lc '
source /catkin_ws/devel/setup.bash
timeout 10 rostopic hz /livox/imu
'
```

未收到上述话题前，不得宣称 Livox 链路恢复，也不得启用运动控制。

## 回滚

若 JSON 校验失败、出现额外差异或重启后 Livox 状态恶化：

```bash
cp -a -- /home/operator/nav_map/MID360s_config.json.bak-20260923 \
  /home/operator/nav_map/MID360s_config.json
docker restart -t 20 nav_robot
```

ROS Master 重启后，再在 Jetson 上执行：

```bash
docker restart -t 10 g1-bridge-safe
```
