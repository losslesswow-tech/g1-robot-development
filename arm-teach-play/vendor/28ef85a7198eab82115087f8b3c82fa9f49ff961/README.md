# g1-arm-teach-play

[English](#english) | [中文](#中文)

---

## 中文

基于 Unitree G1 controller（运控模式）的**最快捷动作开发**工具：手把手示教 → 保存 → 一键回放。

- 适用范围：**腰部及以上 17 个电机**（7×2 手臂 + 3 腰；目前腰部 pitch / roll 官方未开放，实际会被忽略）。
- 通道：官方 `rt/arm_sdk` 高层混控，腿部平衡由运控主控继续负责。

### 前置要求（请先自行配置好）

- Unitree **controller / 运控模式** 已可正常站立
- **unitree_sdk2** 已安装到 `/opt/unitree_robotics/unitree_sdk2`（需要最新版，含 G1 `hg` IDL 和 `AudioClient`）
- **CycloneDDS** 使用 SDK 自带的 `thirdparty/lib/aarch64/` 版本，不要用系统版
- CMake ≥ 3.10、GCC ≥ 7、C++14
- PC2 网络已配好（WiFi、路由、网卡）—— 详见文末 [环境配置指南](#-环境配置--environment-setup)

### 编译

```bash
git clone git@github.com:lez666/g1-arm-teach-play.git
cd g1-arm-teach-play && mkdir build && cd build
cmake .. && make -j4
```

SDK 装到非默认路径时：`cmake .. -DUNITREE_SDK2_PATH=/your/path`

### 使用

**⚠️ 第一次务必吊着或有人扶。机器人必须处在运控模式（正常站立），阻尼/零力矩下 arm_sdk 不生效。**

**1. 示教录制**
```bash
./teach
```
- weight 0→1 接管，进入重力补偿，腰部锁定
- 手动把手臂拖到想要的姿态 → `Enter` 记录一个 waypoint
- 继续拖、继续记 → `q` 保存到 `arm7_action.dat` 并退出

**2. 回放**
```bash
./play
```

| 按键 | 动作 | LED |
|---|---|---|
| L1 | 正向播放（示教顺序依次到达） | 蓝→红 |
| L2 | 反向回到休息位 | 蓝→绿 |
| R1+R2 | 安全退出（weight 平滑降 0） | 熄 |
| Ctrl+C | 同上 | 熄 |

- 回放速度由 `INTERP_T` 控制（默认 `1.5s`，约 2× 初版速度）。
- 启动接管完成后用英文喊一句 **`welcome to the interactive combat league`**，退出（R1+R2 / Ctrl+C）时喊 **`play mode terminated`**。
- `audio.SetVolume(100)` 在启动时把音量拉满，确保现场听得清。

指定网卡（可选）：`UNITREE_IFACE=eth1 ./play`，默认自动挑 `192.0.2.x` 所在网卡。

### Watchdog（一键启动 play 的常驻守护）

不想每次都手动敲 `./play` 可以跑一个后台守护进程，按 **L2+R1** 自动拉起 `play`：

```bash
nohup ./watchdog > ../logs/watchdog.log 2>&1 & disown
tail -f ../logs/watchdog.log
```

- 纯订阅 `rt/lowstate`，**不发布任何控制 DDS 数据**（只在启动时用 `AudioClient` RPC 喊一句语音），对其它程序零影响
- 启动就绪时机器人会用英文播报一句 **"motion activated"**（通过片上 TTS，不依赖公网；WiFi 断了也能响）
- 触发条件：**L2+R1 同按 ≥ 300ms**
- `fork + exec` 启动 `play`，`waitpid` 等 play 退出后才重新待命
- play 退出后要求组合键**完全释放 ≥ 200ms** 才允许再次触发，避免重按时自动再启一次
- `/tmp/g1_arm7_watchdog.lock` 做单实例保护；play 运行期间忽略所有按键
- 停止：`kill $(pgrep -x watchdog)`

#### 开机自启（systemd）

仓库内自带 `systemd/g1-arm7-watchdog.service`。一次安装，永久开机自启：

```bash
sudo install -m 644 systemd/g1-arm7-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now g1-arm7-watchdog.service
```

常用命令：

```bash
systemctl status g1-arm7-watchdog     # 看状态
journalctl -u g1-arm7-watchdog -f     # 看实时日志
sudo systemctl restart g1-arm7-watchdog
sudo systemctl disable --now g1-arm7-watchdog   # 停用开机自启
```

- 以 `unitree` 用户身份运行，不是 root
- 启动前 `sleep 10` 让运控主控的 DDS 先就绪
- 崩溃会 `Restart=on-failure`；正常退出（例如你 `systemctl stop`）不重启
- 日志走 journald；`/tmp` 非私有以保持和交互式 watchdog 共用同一把 flock

### 拳击模式（状态机示例）

`play_with_motions` 是一个在 `play` 基础上加了**状态机**的示例，用来做连招（拳击 / 技能）。

**先用 `teach_with_name` 录制动作文件**（保存到 `motions/<名字>.dat`，每段默认 0.3s）：

```bash
./teach_with_name
# Enter 记录 → q → 输入名字 (如 guard / punch_left / punch_right / skill_y / skill_a)
```

需要至少录好这些：

| 文件 | 录制内容 | 触发按键 |
|---|---|---|
| `motions/guard.dat` | 拳击预备姿势（1 个 waypoint 即可） | — |
| `motions/punch_left.dat` | guard → 左拳峰值 → guard | X |
| `motions/punch_right.dat` | guard → 右拳峰值 → guard | B |
| `motions/skill_y.dat` | 任意，自包含（起止都是 guard） | Y |
| `motions/skill_a.dat` | 任意，自包含（起止都是 guard） | A |

**运行**：
```bash
./play_with_motions
```

状态机：
```
REST ──L1──→ GUARD ──L2──→ REST
              ├──X──→ 左出拳（→ GUARD）
              ├──B──→ 右出拳（→ GUARD）
              ├──Y──→ 技能 Y（→ GUARD）
              └──A──→ 技能 A（→ GUARD）
```

- **R1+R2 只在 REST 状态下有效**（按 L2 回到 REST 后才能退出）；随时可用 Ctrl+C
- 动作进行中**忽略所有按键**，做完自动回 GUARD
- 进入 GUARD / 动作完成后会**清空输入缓冲**，必须松开按键再按下才触发（防止"刚进入就冲出去"）
- LED 全程**虹-蓝-虹-蓝**闪烁
- 动作文件每段时长可编辑：`<duration> q0 q1 ... q13` 每行，不填就用默认 0.3s

### 常见问题

- **按键没反应** → 确认运控模式、`[2/2] engaged` 已出现
- **找不到 `arm7_action.dat`** → 先跑 `./teach`
- **回放姿态偏很多** → 示教和回放请在**同一姿态**下进行（都在运控站立下录和放）
- **网络相关问题**（上不了网、网卡选错、WiFi 连不上）→ 见文末 [环境配置指南](#-环境配置--environment-setup)

### License

MIT

---

## English

Fastest teach-and-playback workflow for Unitree G1 upper body, built on top of the official `arm_sdk` high-level mixing channel. Teach a motion by hand → save → replay with one button.

- Scope: **17 joints from waist up** (7×2 arms + 3 waist; waist pitch / roll are currently not exposed by the controller and are silently ignored).
- Legs stay under the locomotion controller for balance.

### Prerequisites (install these yourself first)

- G1 in **normal locomotion mode** (standing, balanced).
- **unitree_sdk2** installed at `/opt/unitree_robotics/unitree_sdk2` (latest version — needs G1 `hg` IDL and `AudioClient`).
- **CycloneDDS** bundled under `thirdparty/lib/aarch64/` — do NOT use the system build.
- CMake ≥ 3.10, GCC ≥ 7, C++14.
- PC2 network already configured (WiFi, routing, interface) — see [Environment Setup](#-环境配置--environment-setup) at the bottom.

### Build

```bash
git clone git@github.com:lez666/g1-arm-teach-play.git
cd g1-arm-teach-play && mkdir build && cd build
cmake .. && make -j4
```

Custom SDK path: `cmake .. -DUNITREE_SDK2_PATH=/your/path`

### Usage

**⚠️ First run: hang the robot or have someone hold it. The robot MUST be in locomotion mode (standing). `arm_sdk` has no effect in damping / zero-torque modes.**

**1. Teach**
```bash
./teach
```
- weight ramps 0→1, gravity compensation on, waist locked.
- Drag arms by hand → `Enter` to record a waypoint.
- Repeat, then `q` to save `arm7_action.dat`.

**2. Replay**
```bash
./play
```

| Key | Action | LED |
|---|---|---|
| L1 | Forward playback (through recorded waypoints) | blue→red |
| L2 | Reverse back to rest pose | blue→green |
| R1+R2 | Graceful exit (weight smoothly to 0) | off |
| Ctrl+C | Same as above | off |

- Playback speed is controlled by `INTERP_T` (default `1.5s`, ~2× the original).
- On successful engage the robot says **`welcome to the interactive combat league`**; on exit (R1+R2 / Ctrl+C) it says **`play mode terminated`**.
- `audio.SetVolume(100)` on startup maxes the volume so the announcements are audible.

Pick interface (optional): `UNITREE_IFACE=eth1 ./play`. Default: auto-detect the `192.0.2.x` interface.

### Watchdog (background auto-launcher)

If you don't want to type `./play` manually each time, run the background watchdog. It launches `play` when you hold **L2+R1**:

```bash
nohup ./watchdog > ../logs/watchdog.log 2>&1 & disown
tail -f ../logs/watchdog.log
```

- Subscribes to `rt/lowstate` only, **publishes no control topic** (the only outgoing traffic is a one-shot `AudioClient` RPC for the boot announcement) — zero interference with other programs.
- On successful boot the robot speaks **"motion activated"** in English via the on-board TTS engine. This uses the internal 192.0.2.x LAN only, so it works even when WiFi / public internet is down.
- Trigger: **L2+R1 held for ≥ 300 ms**.
- `fork + exec` launches `play`; `waitpid` blocks until `play` exits before re-arming.
- Requires the combo to be **fully released for ≥ 200 ms** after `play` exits to prevent auto-retrigger.
- Single-instance protected via `/tmp/g1_arm7_watchdog.lock`; all keys are ignored while `play` is running.
- Stop it with: `kill $(pgrep -x watchdog)`

#### Auto-start at boot (systemd)

The repo ships with `systemd/g1-arm7-watchdog.service`. Install once, auto-start forever:

```bash
sudo install -m 644 systemd/g1-arm7-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now g1-arm7-watchdog.service
```

Useful commands:

```bash
systemctl status g1-arm7-watchdog     # current state
journalctl -u g1-arm7-watchdog -f     # tail live logs
sudo systemctl restart g1-arm7-watchdog
sudo systemctl disable --now g1-arm7-watchdog   # undo auto-start
```

- Runs as user `unitree`, not root.
- Has a 10 s `sleep` prelude so the locomotion controller's DDS is up first.
- Restarts on crash (`Restart=on-failure`), but stays down if you explicitly stop it.
- Logs go to journald. `PrivateTmp=false` so the `/tmp/g1_arm7_watchdog.lock` stays visible to interactive shells too.

### Boxing mode (state-machine demo)

`play_with_motions` is a state-machine demo on top of `play` for combos (boxing / skills).

**First, record motion files with `teach_with_name`** (saved under `motions/<name>.dat`, 0.3s per segment by default):

```bash
./teach_with_name
# Enter to record → q → type a name (e.g. guard / punch_left / punch_right / skill_y / skill_a)
```

Expected files:

| File | Content | Button |
|---|---|---|
| `motions/guard.dat` | Boxing guard pose (1 waypoint is enough) | — |
| `motions/punch_left.dat` | guard → left punch peak → guard | X |
| `motions/punch_right.dat` | guard → right punch peak → guard | B |
| `motions/skill_y.dat` | Self-contained (starts & ends at guard) | Y |
| `motions/skill_a.dat` | Self-contained (starts & ends at guard) | A |

**Run**:
```bash
./play_with_motions
```

State machine:
```
REST ──L1──→ GUARD ──L2──→ REST
              ├──X──→ left jab (→ GUARD)
              ├──B──→ right jab (→ GUARD)
              ├──Y──→ skill Y (→ GUARD)
              └──A──→ skill A (→ GUARD)
```

- **R1+R2 only works in REST** (press L2 to return to REST first); Ctrl+C always works
- All buttons are ignored while a motion is playing; control returns to GUARD when it finishes
- Input buffer is cleared when entering GUARD / after a motion — you must release and re-press to trigger
- LED flashes rainbow-blue throughout
- Per-segment duration in file: `<duration> q0 q1 ... q13` per line; omit to use the default 0.3s

### Troubleshooting

- **Buttons do nothing** → confirm locomotion mode and that `[2/2] engaged` has printed.
- **`arm7_action.dat` not found** → run `./teach` first.
- **Replay pose drifts a lot** → teach & replay must be in the **same robot pose** (both in locomotion standing).
- **Network issues** (no internet, wrong interface, WiFi won't connect) → see [Environment Setup](#-环境配置--environment-setup) at the bottom.

### License

MIT

---

## 🔧 环境配置 / Environment Setup

PC2 网络、WiFi、路由、DDS 网卡等配置详见：

### 👉 [**docs/env-setup.md**](docs/env-setup.md)

Network configuration, WiFi setup, routing fixes and DDS interface selection for G1 PC2 — see [**docs/env-setup.md**](docs/env-setup.md).
