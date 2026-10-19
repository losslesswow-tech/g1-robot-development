# g1-bridge 容器与镜像

> 宇树 G1 的 **DDS ↔ ROS1 桥接容器**（`g1_ros1_sdk`）的镜像身份、获取方式与运行参数。
> 本文件最后核实：**2026-10-09**（navigation-host，`docker inspect` 实测）。

---

## 1. 镜像身份

| 项 | 值 |
|---|---|
| Tag | `g1-bridge:1.0` |
| **Image ID / manifest digest** | `sha256:ee6129fa6be8b3a412b28e51349230d49b7a0805464e092c4f4cbbc875de6e29` |
| 构建时间 | `2026-08-28T14:52:38+08:00` |
| 大小 | `9,367,125,997` 字节（8.72 GiB） |
| 基础镜像 | `registry.cn-guangzhou.aliyuncs.com/z_nav/bridge:1.0`（ROS noetic + humble 双栈） |
| 构建目录 | `/home/operator/g1bridge/`（Dockerfile + `g1_start.sh` + `pkg/` + `unitree_sdk2/`） |
| 源码 | 本仓库 `bridge/` 目录 |

**同一镜像 ID 的四个 tag / 位置**（已逐一实测比对 digest）：

| 位置 | 引用 |
|---|---|
| 本地（navigation-host） | `g1-bridge:1.0` |
| **实验室 Gitea registry** | `192.0.2.17:3000/operator/g1-bridge:1.0` |
| 阿里云个人版 CR | `registry.example.invalid/robot/navigation:example` |
| 本仓库 Release 附件 | `g1-bridge-1.0.tar`（离线包） |

> 判断"是不是同一个镜像"请比对 **Image ID / digest**，不要比对 tag——tag 是可变的，几处都叫 `1.0` 不代表内容相同。
> 阿里云那份是多架构 index（amd64 manifest digest 为 `sha256:f1132fb1...`），其余三处是同一个单架构 manifest `sha256:ee6129fa...`。

---

## 2. 获取方式（四种，任选）

### 方式 A：从实验室 Gitea registry 拉取（最省事，推荐）

```bash
docker login 192.0.2.17:3000          # 需实验室 Gitea 账号
docker pull 192.0.2.17:3000/operator/g1-bridge:1.0
docker tag  192.0.2.17:3000/operator/g1-bridge:1.0 g1-bridge:1.0
```

| 项 | 值 |
|---|---|
| 镜像地址 | `192.0.2.17:3000/operator/g1-bridge:1.0` |
| **服务端 manifest digest** | `sha256:ee6129fa6be8b3a412b28e51349230d49b7a0805464e092c4f4cbbc875de6e29` |
| 与本地镜像 ID 关系 | **完全相同**（已实测比对，同一份内容） |

> ⚠️ **必须先 `docker login`**：匿名访问该 registry 返回 `401`。Gitea 的容器 registry 即使对公开仓库也要求认证，不能用"仓库是公开的"推断"不用登录"。

### 方式 B：从阿里云个人版 CR 拉取（已实测可拉）

```bash
docker pull registry.example.invalid/robot/navigation:example
docker tag  registry.example.invalid/robot/navigation:example g1-bridge:1.0
```

- 该仓库需要凭据；`192.0.2.5` 上已配置好，可直接使用。
- amd64 manifest digest：`sha256:f1132fb1d2335e0d66674d6534ecc787beee80cc8dda662ff19836e28c6ee139`
- **注意**：`navigation-host` 本机没有配置**阿里云**该仓库的凭据，直接 `docker manifest inspect` 会被拒（`authentication required`）。在 navigation-host 上请改用方式 A / C / D。

### 方式 C：从仓库 Release 附件下载离线包

**下载页**：<https://source.example.invalid/robot/reference>

| 项 | 值 |
|---|---|
| 文件名 | `g1-bridge-1.0.tar` |
| 大小 | `1,863,300,096` 字节（1.8 GB） |
| **MD5** | `3198a839b47724c646dad617c1ef8d1c` |
| **SHA256** | `a1704c22f7440afd79f722a8c9ccfc161ccf5335da5df74ef1eb40d3cebdede8` |
| 格式 | **OCI layout**（`blobs/sha256/*` + `index.json`，非 docker-archive） |

```bash
sha256sum g1-bridge-1.0.tar     # 期望 a1704c22f7440afd79f722a8c9ccfc161ccf5335da5df74ef1eb40d3cebdede8
docker load -i g1-bridge-1.0.tar
```

**关于格式**：该包由 navigation-host 的 `docker save` 生成，因 navigation-host 启用了 containerd 镜像存储（`io.containerd.snapshotter.v1`），产物是 OCI layout 而非传统的 `manifest.json + layer.tar`。

> ✅ **兼容性已实测**：在 `192.0.2.5`（Docker 29.1.3，**经典 `overlay2`**，未启用 containerd 存储）上 `docker load` 该 OCI 格式 tar **成功**（`Loaded image: oci-loadtest:1`）。用小体积镜像做过等价验证，确认不是"只有同款 daemon 才吃得下"。

### 方式 D：从 navigation-host 本机直接取出

```bash
# 镜像已在 navigation-host 本地
docker image inspect g1-bridge:1.0 --format '{{.Id}}'   # 应输出 sha256:ee6129fa6be8...

# 重新导出（注意：snap 版 docker 不能写 /tmp，且 `-o` 会因隐藏临时文件被拒）
docker save g1-bridge:1.0 > /home/operator/g1-bridge-1.0.tar
```

> ⚠️ **snap 版 docker 的两个坑（都实测踩过）**：
> 1. 输出路径必须在 `/home` 下，写 `/tmp` 会静默失败（exit 0 但无文件）；
> 2. `docker save --output xxx.tar` 会报 `permission denied`（无法创建 `.tmp-xxx.tar<随机数>` 隐藏文件），**必须改用 stdout 重定向** `docker save img > file.tar`。

---

## 3. 容器运行参数

```bash
docker run -d --name g1-bridge --network host --restart unless-stopped \
  g1-bridge:1.0 /ros_entrypoint.sh /bin/bash /g1_start.sh
```

| 参数 | 值 |
|---|---|
| `NetworkMode` | `host`（与导航容器共用宿主机 `roscore`） |
| `Privileged` | `false` |
| `RestartPolicy` | `unless-stopped` |
| `Entrypoint` | `/ros_entrypoint.sh` |
| `Cmd` | `/ros_entrypoint.sh /bin/bash /g1_start.sh` |
| `Binds` | 无 |
| `CapAdd` | 无 |

容器内 PID 1：

```
/catkin_ws/devel/lib/g1_ros1_sdk/g1_ros1_sdk _networkInterface:=enp4s0
```

`g1_start.sh` 内容（`bridge/g1_start.sh`）：

```bash
#!/bin/bash
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash
export ROS_MASTER_URI=http://127.0.0.1:11311
exec rosrun g1_ros1_sdk g1_ros1_sdk _networkInterface:=enp4s0
```

---

## 4. 运行状态快照

> 快照时间：**2026-10-09**。容器状态是易变的，使用前请重新 `docker inspect` 核实。

| 项 | 值 |
|---|---|
| Status | `running` |
| StartedAt | `2026-10-05T15:48:26Z` |
| RestartCount | `0` |
| 宿主 | navigation-host |

**判读要点**：

- `RestartCount=0` + `StartedAt` 变新 ⇒ 容器是**被手动 `stop`/`start` 的**，不是重启策略自动拉起。
- **每次 `docker start|restart g1-bridge` 都会重新执行 `StandUp() → 3s → Start()`**——真人形会自己站起来。这两条命令**不能当"重连手段"用**，只能当"有监护的显式动作"。
- 节点"进程在"**不等于**"链路通"。G1 关机或网段不对时，节点会**假活**：进程存活但持续刷
  `ddsi_udp_conn_write to udp/239.255.0.1:7400 failed`，此时 `/sdk/imu` 无数据。
  **判据永远是数据流，不是 `ps`。**

---

## 5. 部署到其他宿主机

### 5.1 必须改网卡名

`g1_start.sh` 硬编码 `_networkInterface:=enp4s0`。**网卡名因机器而异**：

| 宿主机 | 机器人网段网卡 | 是否需要改 |
|---|---|---|
| navigation-host | `enp4s0`（`192.0.2.16`） | 否 |
| `192.0.2.5` | `enp132s0`（`192.0.2.6`） | **要改** |

不改会报 `does not match an available interface`，DDS 建链失败。

### 5.2 前置条件

1. 宿主机网卡在 `192.0.2.7/24` 网段，且能 `ping` 通 MCU `192.0.2.10`；
2. 导航容器已在跑（`roscore` 在 `127.0.0.1:11311`）——**先起导航，再起桥接**；
3. 机器人已上电，且**现场清场、有人监护**。

### 5.3 重新编译

源码改动后（宿主机改 `bridge/pkg/src/*.cpp`）：

```bash
docker cp bridge/pkg/src/g1_control.cpp g1-bridge:/catkin_ws/src/g1_ros1_sdk/src/g1_control.cpp
docker exec g1-bridge bash -lc 'source /opt/ros/noetic/setup.bash && cd /catkin_ws && \
  catkin_make -DCATKIN_WHITELIST_PACKAGES=g1_ros1_sdk'
```

若要从头构建镜像，先用 `bridge/fetch_unitree_sdk2.sh` 从导航容器提取 44 MB 预编译库，再 `docker build`。

---

## 6. 安全红线

- **未清场禁止** `docker start|restart g1-bridge`（必然触发站立）。
- **未确认清场禁止** `g1_control` 的 `move` / `action` / `g1_arm` / 灵巧手写寄存器。
- FSM：`500` 只能 `damp` 回 `1`，不能直接跳 `4`/`0`。
- 收工顺序：`damp` → 停桥接容器 → 断电。
- 用户说「停」立即停。
