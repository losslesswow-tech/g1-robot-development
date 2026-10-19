#!/usr/bin/env python3
"""
Inspire RH56DFTP 灵巧手控制 (Modbus TCP)
========================================
G1 上两只 Inspire 灵巧手，通过网线接入 192.0.2 网段：
  右手 = 192.0.2.4:6000
  左手 = 192.0.2.3:6000

协议：Modbus TCP
  ANGLE_SET  = 1486 (FC=0x10 写, 目标角度 0..1000/自由度)
  SPEED_SET  = 1522 (FC=0x10 写, 电机速度 0..1000)
  ANGLE_ACT  = 1546 (FC=0x03 读, 实际角度)
  FORCE_ACT  = 1582 (FC=0x03 读, 抓取力)
DOF 顺序: [小指, 无名指, 中指, 食指, 拇指弯, 拇指旋]

用法:
  inspire_hand_ctl.py status                 # 读两只手角度+力
  inspire_hand_ctl.py open   [L|R|B] [pct]   # 张开到 pct% (默认 10%)
  inspire_hand_ctl.py close  [L|R|B] [pct]   # 闭合到 pct% (默认 100%)
  inspire_hand_ctl.py grip   [L|R|B] [pct]   # 全部手指到 pct% (默认 50%)
  inspire_hand_ctl.py pos    L|R <a1..a6>    # 精确设置 6 个角度 0-1000
  inspire_hand_ctl.py finger L|R <0-5> <ang> # 单指设置
  inspire_hand_ctl.py speed  L|R <n>         # 设置速度 0-1000
  inspire_hand_ctl.py reg    L|R <addr> [n]  # 读寄存器(调试)
"""
import socket
import struct
import sys
import time

HANDS = {"L": "192.0.2.3", "R": "192.0.2.4"}
PORT = 6000
DOF = ["小指", "无名指", "中指", "食指", "拇指弯", "拇指旋"]

ANGLE_SET, SPEED_SET, ANGLE_ACT, FORCE_ACT = 1486, 1522, 1546, 1582


def read_regs(ip, addr, count, timeout=3.0):
    s = socket.create_connection((ip, PORT), timeout=timeout)
    try:
        pdu = struct.pack(">BHH", 0x03, addr, count)
        mbap = struct.pack(">HHHB", 1, 0, 1 + len(pdu), 1)
        s.sendall(mbap + pdu)
        resp = s.recv(4096)
        if len(resp) < 4 + 2 * count:
            raise RuntimeError(f"短响应 {len(resp)}B")
        return list(struct.unpack(">%dH" % count, resp[-2 * count:]))
    finally:
        s.close()


def write_regs(ip, addr, values, timeout=3.0):
    """写保持寄存器，并必须读取 12 字节 ACK（否则对方发送缓冲满会堵死）。"""
    s = socket.create_connection((ip, PORT), timeout=timeout)
    try:
        n = len(values)
        pdu = struct.pack(">BHHB", 0x10, addr, n, 2 * n)
        pdu += b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
        mbap = struct.pack(">HHHB", 1, 0, 1 + len(pdu), 1)
        s.sendall(mbap + pdu)
        s.recv(1024)  # 排空 ACK
    finally:
        s.close()


def set_speed(ip, speed):
    write_regs(ip, SPEED_SET, [speed] * 6)


def set_angles(ip, angles, speed=300, settle=0.1):
    """设置 6 个目标角度（0-1000），慢速安全，写至稳定。"""
    set_speed(ip, speed)
    time.sleep(0.05)
    write_regs(ip, ANGLE_SET, angles)
    # 等待到位（最多 8 秒）
    for _ in range(80):
        time.sleep(settle)
        cur = read_regs(ip, ANGLE_ACT, 6)
        if max(abs(a - b) for a, b in zip(cur, angles)) < 8:
            return cur
    return read_regs(ip, ANGLE_ACT, 6)


def show_status(side=None):
    for k, ip in HANDS.items():
        if side and k != side:
            continue
        try:
            a = read_regs(ip, ANGLE_ACT, 6)
            f = read_regs(ip, FORCE_ACT, 6)
            print(f"--- 手 {k} ({ip}) ---")
            for i in range(6):
                bar = "#" * (a[i] // 50)
                print(f"  {DOF[i]:4s} 角度={a[i]:4d} 力={f[i]:4d}  {bar}")
        except Exception as e:
            print(f"--- 手 {k} ({ip}) : 连接失败 {e}")


def pct_to_angles(pct):
    return [int(pct * 1000)] * 6


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1].lower()

    if cmd == "status":
        side = sys.argv[2].upper() if len(sys.argv) > 2 else None
        show_status(side)
    elif cmd in ("open", "close", "grip"):
        side = sys.argv[2].upper() if len(sys.argv) > 2 else "B"
        pct = float(sys.argv[3]) / 100.0 if len(sys.argv) > 3 else (
            0.1 if cmd == "open" else (1.0 if cmd == "close" else 0.5))
        targets = pct_to_angles(pct)
        for k in (["L", "R"] if side == "B" else [side]):
            ip = HANDS[k]
            print(f"手 {k} ({ip}): {cmd} -> {int(pct*100)}% {targets}")
            set_angles(ip, targets)
            show_status(k)
    elif cmd == "pos":
        if len(sys.argv) < 9:
            print("pos 需要: L|R 和 6 个角度 (0-1000)")
            return 1
        side = sys.argv[2].upper()
        angles = [max(0, min(1000, int(float(x)))) for x in sys.argv[3:9]]
        print(f"手 {side} ({HANDS[side]}): 设置角度 {angles} (速度 300)")
        cur = set_angles(HANDS[side], angles)
        print("到位角度:", cur)
    elif cmd == "finger":
        if len(sys.argv) < 5:
            print("finger 需要: L|R <0-5> <角度0-1000>")
            return 1
        side = sys.argv[2].upper()
        idx = int(sys.argv[3])
        ang = max(0, min(1000, int(sys.argv[4])))
        ip = HANDS[side]
        cur = read_regs(ip, ANGLE_ACT, 6)
        cur[idx] = ang
        print(f"手 {side}: {DOF[idx]} -> {ang}")
        set_angles(ip, cur)
    elif cmd == "speed":
        side = sys.argv[2].upper()
        n = max(0, min(1000, int(sys.argv[3])))
        set_speed(HANDS[side], n)
        print(f"手 {side} 速度 -> {n}")
    elif cmd == "reg":
        side = sys.argv[2].upper()
        addr = int(sys.argv[3], 0)
        count = int(sys.argv[4]) if len(sys.argv) > 4 else 6
        vals = read_regs(HANDS[side], addr, count)
        print(f"手 {side} reg[{addr}] ({count}):", vals)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
