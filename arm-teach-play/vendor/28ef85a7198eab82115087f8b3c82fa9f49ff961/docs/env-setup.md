# 环境配置 / Environment Setup

[English](#english) | [中文](#中文)

---

## 中文

本文件介绍把 G1 PC2 从出厂状态配到能跑 `g1-arm-teach-play` 的网络 / 环境步骤。PC2 = Jetson Orin NX + Ubuntu 20.04。

G1 PC2 默认通过 `192.0.2.x` 子网和运控主控通信（DDS 走这里），公网则通过 WiFi 出口。

### Step 0 — 让 PC2 先连上 WiFi

装 SDK、clone 本项目都需要外网。

图形方式（推荐）：
```bash
nmtui
# 方向键选 Activate a connection → 选 SSID → 输密码
```

命令行方式：
```bash
nmcli device wifi list
sudo nmcli device wifi connect "YOUR_SSID" password "YOUR_PASSWORD"
sudo nmcli connection modify "YOUR_SSID" connection.autoconnect yes
```

验证：
```bash
ip -4 addr show wlan0
ping -c 2 8.8.8.8
ping -c 2 github.com
```

### Step 1 — 网卡选择

默认 eth0 连运控主控（`192.0.2.8`）。本项目会自动选第一个 `192.0.2.x` 网卡；也可手动指定：
```bash
UNITREE_IFACE=eth1 ./play
```

### Step 2 — 不要让 `192.0.2.9` 抢走默认路由

运控主控 IP 不走公网，若它成了默认网关，你**上不了外网**（无法 `apt` / `git clone`）。

临时修：
```bash
sudo ip route del default via 192.0.2.9
```

持久化 —— 存到 `/etc/NetworkManager/dispatcher.d/99-robot-route` 并 `chmod +x`：
```bash
#!/bin/bash
IFACE="$1"; ACTION="$2"
case "$IFACE" in lo|docker*|veth*|l4tbr*|rndis*|usb*|dummy*) exit 0 ;; esac
case "$ACTION" in
  up|dhcp4-change|dhcp6-change|connectivity-change)
    if ip -4 addr show dev "$IFACE" | grep -qE 'inet 192\.168\.123\.'; then
      for i in 1 2 3; do
        if ip route show default | grep -qE "default via 192\.168\.123\.1.*dev $IFACE"; then
          ip route del default via 192.0.2.9 dev "$IFACE" 2>/dev/null || true
          sleep 1
        else break; fi
      done
    fi ;;
esac
exit 0
```

### Step 3 — WiFi 默认网关缓存问题

NetworkManager 偶尔缓存旧的 `.1`，WiFi 连上但 ping 不通。重新 DHCP：
```bash
sudo dhclient -r wlan0 && sudo dhclient -v wlan0
```

### Step 4 — DDS 环境变量

代码内部 `ChannelFactory::Init(0, iface)` 自动绑定网卡，**不需要**手动设 `CYCLONEDDS_URI`。

### 常见坑

- PC2 自带 Realtek USB WiFi，5GHz 偶尔掉速：离路由器近一点、或改走 2.4GHz
- `nmcli device wifi list` 空 → `sudo systemctl restart NetworkManager` 再试
- 终端刷满 `unregister_netdevice: waiting for eth0 ...` → 内核 netdev 引用计数泄露（和本项目无关），重启可清；临时屏蔽：
  ```bash
  sudo sed -i 's|^\*\.emerg\s*:omusrmsg:\*|*.emerg;kern.none\t:omusrmsg:*|' \
      /etc/rsyslog.d/50-default.conf
  sudo systemctl restart rsyslog
  echo 'kernel.printk = 1 4 1 3' | sudo tee /etc/sysctl.d/99-quiet-kernel.conf
  sudo sysctl --system
  ```
- Jetson RTC 电池没电导致开机回到 1970：
  ```bash
  sudo apt install -y systemd-timesyncd
  sudo systemctl enable --now systemd-timesyncd
  sudo hwclock --systohc   # 等系统时间同步好后，写入硬件 RTC
  ```

---

## English

This document walks through the network / environment steps needed to take a fresh G1 PC2 (Jetson Orin NX + Ubuntu 20.04) to the point of running `g1-arm-teach-play`.

G1 PC2 talks to the locomotion controller over the `192.0.2.x` subnet (DDS traffic). Internet goes out over WiFi.

### Step 0 — Get PC2 online over WiFi

Needed to install the SDK and clone this repo.

TUI (easiest):
```bash
nmtui
# Activate a connection → pick SSID → enter password
```

CLI:
```bash
nmcli device wifi list
sudo nmcli device wifi connect "YOUR_SSID" password "YOUR_PASSWORD"
sudo nmcli connection modify "YOUR_SSID" connection.autoconnect yes
```

Verify:
```bash
ip -4 addr show wlan0
ping -c 2 8.8.8.8
ping -c 2 github.com
```

### Step 1 — Interface selection

`eth0` is the default link to the controller (`192.0.2.8`). This project auto-picks the first interface with a `192.0.2.x` address. Override manually:
```bash
UNITREE_IFACE=eth1 ./play
```

### Step 2 — Don't let `192.0.2.9` hijack your default route

The controller IP has no internet. If it becomes the default gateway you lose `apt` / `git clone`.

Temporary fix:
```bash
sudo ip route del default via 192.0.2.9
```

Persistent fix — save to `/etc/NetworkManager/dispatcher.d/99-robot-route` and `chmod +x`:
```bash
#!/bin/bash
IFACE="$1"; ACTION="$2"
case "$IFACE" in lo|docker*|veth*|l4tbr*|rndis*|usb*|dummy*) exit 0 ;; esac
case "$ACTION" in
  up|dhcp4-change|dhcp6-change|connectivity-change)
    if ip -4 addr show dev "$IFACE" | grep -qE 'inet 192\.168\.123\.'; then
      for i in 1 2 3; do
        if ip route show default | grep -qE "default via 192\.168\.123\.1.*dev $IFACE"; then
          ip route del default via 192.0.2.9 dev "$IFACE" 2>/dev/null || true
          sleep 1
        else break; fi
      done
    fi ;;
esac
exit 0
```

### Step 3 — Stale WiFi gateway cache

NetworkManager sometimes caches an old `.1`. Refresh DHCP if WiFi is up but the gateway is unreachable:
```bash
sudo dhclient -r wlan0 && sudo dhclient -v wlan0
```

### Step 4 — DDS env vars

The code calls `ChannelFactory::Init(0, iface)` internally — **no need** to set `CYCLONEDDS_URI` manually.

### Common pitfalls

- PC2 has a Realtek USB WiFi chip; 5 GHz can be flaky. Move closer or fall back to 2.4 GHz.
- `nmcli device wifi list` empty → `sudo systemctl restart NetworkManager` and retry.
- Terminal flooded with `unregister_netdevice: waiting for eth0 ...` → kernel netdev refcount leak (unrelated to this project); reboot clears it. To silence:
  ```bash
  sudo sed -i 's|^\*\.emerg\s*:omusrmsg:\*|*.emerg;kern.none\t:omusrmsg:*|' \
      /etc/rsyslog.d/50-default.conf
  sudo systemctl restart rsyslog
  echo 'kernel.printk = 1 4 1 3' | sudo tee /etc/sysctl.d/99-quiet-kernel.conf
  sudo sysctl --system
  ```
- Jetson RTC battery flat → clock resets to 1970 on every boot:
  ```bash
  sudo apt install -y systemd-timesyncd
  sudo systemctl enable --now systemd-timesyncd
  sudo hwclock --systohc   # after time is synced, write back to hardware RTC
  ```
