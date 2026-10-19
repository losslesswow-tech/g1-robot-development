/**
 * play_with_motions.cpp — G1 Arm7 Boxing State Machine
 * =====================================================
 * 状态机：
 *   REST ──L1──→ ENTER_GUARD ──done──→ GUARD
 *                                        ├──X──→ PUNCH_LEFT  ──done──→ GUARD
 *                                        ├──B──→ PUNCH_RIGHT ──done──→ GUARD
 *                                        ├──Y──→ SKILL_Y     ──done──→ GUARD
 *                                        ├──A──→ SKILL_A     ──done──→ GUARD
 *                                        └──L2──→ EXIT_GUARD ──done──→ REST
 *   R1+R2 只在 REST 状态下退出（GUARD 里不响应）
 *   Ctrl+C 任意时刻退出
 *
 * 动作文件 (motions/<name>.dat)：
 *   行 1: N (waypoint 数)
 *   行 2: D = 14 (关节数)
 *   行 3..N+2: duration q0 q1 ... q13   (duration 可选，缺省 0.3s)
 *
 * LED: 独立线程，全程 "虹-蓝-虹-蓝" 循环闪烁
 *
 * 网卡: 自动选择 192.0.2.x 所在接口（UNITREE_IFACE 可覆盖）
 */

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>

#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

using namespace unitree::robot;
using unitree_hg::msg::dds_::LowCmd_;
using unitree_hg::msg::dds_::LowState_;

// ── 关节 ─────────────────────────────────────────────────────────────────
static constexpr int N_ARM      = 14;
static constexpr int N_TOTAL    = 17;
static constexpr int WEIGHT_IDX = 29;

static constexpr int JOINT_IDX[N_TOTAL] = {
    15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28,
    12, 13, 14
};

// ── 控制参数 ────────────────────────────────────────────────────────────
static constexpr float KP_ARM   = 60.0f;
static constexpr float KD_ARM   = 1.5f;
static constexpr float KP_WAIST = 200.0f;
static constexpr float KD_WAIST = 2.0f;
static constexpr float ENGAGE_T = 3.0f;          // weight ramp 0↔1
static constexpr float DEFAULT_SEG_DURATION = 0.3f;
static constexpr int   LOOP_US  = 2000;          // 500Hz

// ── 遥控器按键位 ─────────────────────────────────────────────────────────
static constexpr uint16_t KEY_R1 = (1 << 0);
static constexpr uint16_t KEY_L1 = (1 << 1);
static constexpr uint16_t KEY_R2 = (1 << 4);
static constexpr uint16_t KEY_L2 = (1 << 5);
static constexpr uint16_t KEY_A  = (1 << 8);
static constexpr uint16_t KEY_B  = (1 << 9);
static constexpr uint16_t KEY_X  = (1 << 10);
static constexpr uint16_t KEY_Y  = (1 << 11);

// ── 全局状态 ─────────────────────────────────────────────────────────────
static LowState_          g_state{};
static std::mutex         g_state_mtx;
static std::atomic<bool>  g_running{true};
static std::atomic<bool>  g_state_received{false};

static void on_lowstate(const void* msg) {
    std::lock_guard<std::mutex> lk(g_state_mtx);
    std::memcpy(&g_state, msg, sizeof(LowState_));
    g_state_received = true;
}

static void on_signal(int) {
    std::cout << "\n[SIGNAL] 收到退出信号...\n";
    g_running = false;
}

static std::string pick_robot_iface() {
    if (const char* env = std::getenv("UNITREE_IFACE")) {
        if (env[0]) { std::cout << "[NET] 使用环境变量指定网卡: " << env << "\n"; return env; }
    }
    struct ifaddrs* ifs = nullptr;
    if (getifaddrs(&ifs) == 0) {
        for (auto* p = ifs; p; p = p->ifa_next) {
            if (!p->ifa_addr || p->ifa_addr->sa_family != AF_INET) continue;
            auto* sin = reinterpret_cast<sockaddr_in*>(p->ifa_addr);
            uint32_t ip = ntohl(sin->sin_addr.s_addr);
            if ((ip & 0xFFFFFF00u) == 0xC0A87B00u) {
                std::string name = p->ifa_name ? p->ifa_name : "";
                std::cout << "[NET] 自动选中网卡: " << name
                          << " (IP " << ((ip>>24)&0xff) << "." << ((ip>>16)&0xff)
                          << "." << ((ip>>8)&0xff) << "." << (ip&0xff) << ")\n";
                freeifaddrs(ifs);
                return name;
            }
        }
        freeifaddrs(ifs);
    }
    std::cerr << "[NET][WARN] 没找到 192.0.2.x 网卡，回退到 eth0\n";
    return "eth0";
}

using ArmQ   = std::array<float, N_ARM>;
using TotalQ = std::array<float, N_TOTAL>;

struct Waypoint {
    float duration;          // seconds to reach this wp from previous
    ArmQ  q;
};
struct Motion {
    std::string name;
    std::vector<Waypoint> wps;
    bool loaded = false;
};

static TotalQ get_total_q() {
    TotalQ q{};
    std::lock_guard<std::mutex> lk(g_state_mtx);
    for (int i = 0; i < N_TOTAL; ++i)
        q[i] = g_state.motor_state()[JOINT_IDX[i]].q();
    return q;
}

static ArmQ get_arm_q() {
    ArmQ q{};
    std::lock_guard<std::mutex> lk(g_state_mtx);
    for (int i = 0; i < N_ARM; ++i)
        q[i] = g_state.motor_state()[JOINT_IDX[i]].q();
    return q;
}

static uint16_t read_keys() {
    uint16_t k = 0;
    std::lock_guard<std::mutex> lk(g_state_mtx);
    std::memcpy(&k, &g_state.wireless_remote()[2], 2);
    return k;
}

// ── cmd 写入 ────────────────────────────────────────────────────────────
static std::mutex g_cmd_mtx;

static void apply_cmd(LowCmd_& cmd, const ArmQ& arm_q, const TotalQ& q_full_hold) {
    // arm 用实时目标，腰部锁定到 q_full_hold 的腰部分量
    std::lock_guard<std::mutex> lk(g_cmd_mtx);
    for (int i = 0; i < N_ARM; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(arm_q[i]); mc.kp(KP_ARM); mc.kd(KD_ARM); mc.dq(0.0f); mc.tau(0.0f);
    }
    for (int i = N_ARM; i < N_TOTAL; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q_full_hold[i]); mc.kp(KP_WAIST); mc.kd(KD_WAIST); mc.dq(0.0f); mc.tau(0.0f);
    }
}

static void apply_total(LowCmd_& cmd, const TotalQ& q) {
    std::lock_guard<std::mutex> lk(g_cmd_mtx);
    for (int i = 0; i < N_ARM; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q[i]); mc.kp(KP_ARM); mc.kd(KD_ARM); mc.dq(0.0f); mc.tau(0.0f);
    }
    for (int i = N_ARM; i < N_TOTAL; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q[i]); mc.kp(KP_WAIST); mc.kd(KD_WAIST); mc.dq(0.0f); mc.tau(0.0f);
    }
}

static void set_weight_locked(LowCmd_& cmd, float w) {
    std::lock_guard<std::mutex> lk(g_cmd_mtx);
    cmd.motor_cmd()[WEIGHT_IDX].q(std::max(0.0f, std::min(1.0f, w)));
}

static void publish_locked(std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
                            LowCmd_& cmd) {
    std::lock_guard<std::mutex> lk(g_cmd_mtx);
    pub->Write(cmd);
}

// ── 斜坡 ────────────────────────────────────────────────────────────────
static void ramp_weight_up(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration, const TotalQ& q_hold)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps && g_running; ++i) {
        const float w = static_cast<float>(i + 1) / steps;
        set_weight_locked(cmd, w);
        apply_total(cmd, q_hold);
        publish_locked(pub, cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
}

static void ramp_weight_down(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration, const TotalQ& q_hold)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps; ++i) {
        const float w = 1.0f - static_cast<float>(i + 1) / steps;
        set_weight_locked(cmd, w);
        apply_total(cmd, q_hold);
        publish_locked(pub, cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
    set_weight_locked(cmd, 0.0f);
    {
        std::lock_guard<std::mutex> lk(g_cmd_mtx);
        for (int i = 0; i < N_TOTAL; ++i) {
            auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
            mc.q(q_hold[i]); mc.kp(0.0f); mc.kd(0.0f); mc.dq(0.0f); mc.tau(0.0f);
        }
    }
    publish_locked(pub, cmd);
}

// ── 动作插值 ────────────────────────────────────────────────────────────
static ArmQ lerp_arm(const ArmQ& a, const ArmQ& b, float t) {
    ArmQ r{};
    for (int i = 0; i < N_ARM; ++i) r[i] = a[i] + (b[i] - a[i]) * t;
    return r;
}

// 插值到单个目标
// 返回实际到达的姿态（可能因中途被打断略早）
static ArmQ interp_to(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd,
    const ArmQ& q_from, const ArmQ& q_to,
    float duration, const TotalQ& q_hold)
{
    if (duration <= 0.0f) duration = DEFAULT_SEG_DURATION;
    const int steps = std::max(1, static_cast<int>(duration / (LOOP_US * 1e-6f)));
    ArmQ q_cur = q_from;
    for (int i = 0; i < steps && g_running; ++i) {
        q_cur = lerp_arm(q_from, q_to, static_cast<float>(i+1)/steps);
        apply_cmd(cmd, q_cur, q_hold);
        publish_locked(pub, cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
    return q_cur;
}

// 执行整个 motion：逐段到 wp
static ArmQ run_motion(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd,
    const Motion& m, const ArmQ& q_from,
    const TotalQ& q_hold)
{
    ArmQ q = q_from;
    for (size_t i = 0; i < m.wps.size() && g_running; ++i) {
        q = interp_to(pub, cmd, q, m.wps[i].q, m.wps[i].duration, q_hold);
    }
    return q;
}

// ── 动作文件加载 ────────────────────────────────────────────────────────
static bool load_motion(const std::string& name, Motion& out,
                        const std::string& dir = "motions") {
    out.name = name;
    out.loaded = false;
    out.wps.clear();
    std::ifstream f(dir + "/" + name + ".dat");
    if (!f) return false;

    int N = 0, D = 0;
    if (!(f >> N)) return false;
    if (!(f >> D)) return false;
    if (D != N_ARM) {
        std::cerr << "[MOTION][ERROR] " << name << ".dat D=" << D
                  << " 不是 " << N_ARM << "\n";
        return false;
    }
    std::string rest;
    std::getline(f, rest);  // eat newline

    for (int k = 0; k < N; ++k) {
        std::string line;
        if (!std::getline(f, line)) {
            std::cerr << "[MOTION][ERROR] " << name << ".dat 行数不足\n";
            return false;
        }
        std::istringstream iss(line);
        std::vector<float> vals;
        float x;
        while (iss >> x) vals.push_back(x);
        Waypoint wp{};
        if ((int)vals.size() == N_ARM + 1) {
            wp.duration = vals[0];
            for (int i = 0; i < N_ARM; ++i) wp.q[i] = vals[i+1];
        } else if ((int)vals.size() == N_ARM) {
            wp.duration = DEFAULT_SEG_DURATION;
            for (int i = 0; i < N_ARM; ++i) wp.q[i] = vals[i];
        } else {
            std::cerr << "[MOTION][ERROR] " << name << ".dat 第 " << k+2
                      << " 行列数 " << vals.size() << " 不对\n";
            return false;
        }
        out.wps.push_back(wp);
    }
    out.loaded = true;
    return true;
}

// ── LED 闪烁线程: 虹-蓝 交替闪 ──────────────────────────────────────────
struct LedColor { uint8_t r,g,b; };
static const LedColor RAINBOW[] = {
    {255,  30,  30},   // red
    {255, 120,   0},   // orange
    {255, 220,   0},   // yellow
    { 30, 255,  30},   // green
    {  0, 255, 200},   // cyan
    {200,   0, 255},   // purple
};
static const LedColor BLUE_ACCENT = {30, 60, 255};

static void led_thread_fn(unitree::robot::g1::AudioClient* audio,
                          std::atomic<bool>* stop)
{
    int step = 0;
    while (!stop->load()) {
        LedColor c;
        if (step % 2 == 0) {
            c = RAINBOW[(step / 2) % (sizeof(RAINBOW)/sizeof(RAINBOW[0]))];
        } else {
            c = BLUE_ACCENT;
        }
        (void)audio->LedControl(c.r, c.g, c.b);
        for (int t = 0; t < 15 && !stop->load(); ++t) {  // 15 × 10ms = 150ms
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        step++;
    }
    (void)audio->LedControl(0, 0, 0);
}

// ════════════════════════════════════════════════════════════════════════
int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    // 加载动作
    Motion guard, pL, pR, skY, skA;
    bool ok_guard = load_motion("guard", guard);
    bool ok_pL    = load_motion("punch_left", pL);
    bool ok_pR    = load_motion("punch_right", pR);
    bool ok_skY   = load_motion("skill_y", skY);
    bool ok_skA   = load_motion("skill_a", skA);

    if (!ok_guard || guard.wps.empty()) {
        std::cerr << "[ERROR] motions/guard.dat 必需。请先录制该文件。\n";
        return 1;
    }

    std::cout << "===========================================\n"
              << "  G1 Arm7 - 拳击状态机 play_with_motions\n"
              << "  L1  : 进入拳击 (REST → GUARD)\n"
              << "  L2  : 退出拳击 (GUARD → REST)\n"
              << "  X/B : 左/右出拳 (GUARD 内)\n";
    std::cout << "  Y   : 技能 Y " << (ok_skY ? "(已加载)" : "(未录制，忽略)") << "\n";
    std::cout << "  A   : 技能 A " << (ok_skA ? "(已加载)" : "(未录制，忽略)") << "\n";
    std::cout << "  R1+R2 : 退出程序 (仅 REST 状态有效)\n"
              << "  Ctrl+C: 随时退出\n"
              << "===========================================\n\n";

    if (!ok_pL) std::cout << "[WARN] punch_left  未加载，X 将被忽略\n";
    if (!ok_pR) std::cout << "[WARN] punch_right 未加载，B 将被忽略\n";

    const std::string iface = pick_robot_iface();
    ChannelFactory::Instance()->Init(0, iface);

    auto pub = std::make_shared<ChannelPublisher<LowCmd_>>("rt/arm_sdk");
    pub->InitChannel();

    auto sub = std::make_shared<ChannelSubscriber<LowState_>>("rt/lowstate");
    sub->InitChannel(on_lowstate, 10);

    // LED
    unitree::robot::g1::AudioClient audio;
    audio.SetTimeout(3.0f);
    audio.Init();
    for (int i = 0; i < 3; ++i) {
        audio.LedControl(0, 0, 0);
        std::this_thread::sleep_for(std::chrono::milliseconds(80));
    }
    std::atomic<bool> led_stop{false};
    std::thread led_thr(led_thread_fn, &audio, &led_stop);

    std::cout << "[WAIT] 等待 lowstate 数据...\n";
    while (!g_state_received && g_running)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    if (!g_running) {
        led_stop = true; led_thr.join(); return 0;
    }

    LowCmd_ cmd{};
    for (auto& mc : cmd.motor_cmd()) {
        mc.q(0.0f); mc.dq(0.0f); mc.kp(0.0f); mc.kd(0.0f); mc.tau(0.0f);
    }

    TotalQ q_init = get_total_q();
    std::cout << "[INFO] 腰部初始角 (rad): "
              << std::fixed << std::setprecision(3)
              << q_init[14] << " " << q_init[15] << " " << q_init[16] << "\n";

    ArmQ arm_init{};
    for (int i = 0; i < N_ARM; ++i) arm_init[i] = q_init[i];

    apply_total(cmd, q_init);
    set_weight_locked(cmd, 0.0f);
    publish_locked(pub, cmd);

    std::cout << "[1/2] 接管 (weight 0→1) ...\n";
    ramp_weight_up(pub, cmd, ENGAGE_T, q_init);
    std::cout << "[2/2] 接管完成 | REST (按 L1 进入拳击)\n\n";

    // ── 状态机 ──────────────────────────────────────────────────────────
    enum class State { REST, GUARD };
    State state = State::REST;

    const ArmQ guard_pose = guard.wps.back().q;   // 假定 guard.dat 最后一个 wp 就是 guard 静止姿态

    ArmQ q_cur = arm_init;
    uint16_t prev_keys = 0xFFFF;   // 初始视作"所有键按下"，强制释放后才触发

    int hold_pub_counter = 0;

    while (g_running) {
        const uint16_t keys = read_keys();
        const uint16_t rise = keys & ~prev_keys;   // 本帧新按下的位
        prev_keys = keys;

        if (state == State::REST) {
            // 维持 arm_init
            q_cur = arm_init;
            apply_cmd(cmd, q_cur, q_init);
            publish_locked(pub, cmd);

            if ((keys & KEY_R1) && (keys & KEY_R2)) {
                std::cout << "[EXIT] R1+R2 (REST 状态) 退出\n";
                break;
            }
            if (rise & KEY_L1) {
                std::cout << "[STATE] REST → ENTER_GUARD\n";
                q_cur = interp_to(pub, cmd, q_cur, guard_pose,
                                  guard.wps.back().duration, q_init);
                if (!g_running) break;
                state = State::GUARD;
                prev_keys = 0xFFFF;   // 清输入，要求全部释放再采样
                std::cout << "[STATE] GUARD (X=左拳 B=右拳 L2=退出拳击)\n";
            }
        } else {  // GUARD
            // 保持 guard_pose
            q_cur = guard_pose;
            apply_cmd(cmd, q_cur, q_init);
            publish_locked(pub, cmd);

            if (rise & KEY_L2) {
                std::cout << "[STATE] GUARD → EXIT_GUARD\n";
                q_cur = interp_to(pub, cmd, q_cur, arm_init,
                                  guard.wps.back().duration, q_init);
                if (!g_running) break;
                state = State::REST;
                prev_keys = 0xFFFF;
                std::cout << "[STATE] REST (按 L1 再次进入 / R1+R2 退出)\n";
            } else if ((rise & KEY_X) && ok_pL) {
                std::cout << "[ACTION] 左出拳 (X)\n";
                q_cur = run_motion(pub, cmd, pL, q_cur, q_init);
                // 回到 guard_pose（假定 motion 最后一个 wp 就是 guard 姿态）
                q_cur = guard_pose;
                prev_keys = 0xFFFF;  // 清键防止连击
            } else if ((rise & KEY_B) && ok_pR) {
                std::cout << "[ACTION] 右出拳 (B)\n";
                q_cur = run_motion(pub, cmd, pR, q_cur, q_init);
                q_cur = guard_pose;
                prev_keys = 0xFFFF;
            } else if ((rise & KEY_Y) && ok_skY) {
                std::cout << "[ACTION] 技能 Y\n";
                q_cur = run_motion(pub, cmd, skY, q_cur, q_init);
                q_cur = guard_pose;
                prev_keys = 0xFFFF;
            } else if ((rise & KEY_A) && ok_skA) {
                std::cout << "[ACTION] 技能 A\n";
                q_cur = run_motion(pub, cmd, skA, q_cur, q_init);
                q_cur = guard_pose;
                prev_keys = 0xFFFF;
            }
        }

        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
        hold_pub_counter++;
    }

    std::cout << "\n[EXIT] weight 1→0 ...\n";
    TotalQ q_exit = get_total_q();
    for (int i = N_ARM; i < N_TOTAL; ++i) q_exit[i] = q_init[i];
    ramp_weight_down(pub, cmd, ENGAGE_T, q_exit);

    led_stop = true;
    led_thr.join();
    std::cout << "[BYE]\n";
    return 0;
}
