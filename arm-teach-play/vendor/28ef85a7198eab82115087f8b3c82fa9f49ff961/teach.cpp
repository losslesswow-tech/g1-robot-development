/**
 * teach.cpp - G1 Arm7 Kinesthetic Teaching  [Final]
 * ====================================================
 * Reference: unitree_sdk2/example/g1/high_level/g1_arm7_sdk_dds_example.cpp
 *
 * Waist: 3 joints (12,13,14) are controlled via arm_sdk,
 *        locked at their initial angle on startup, KP=200/KD=2 throughout.
 * Arms:  gravity compensation (kp=0, kd=0.8), free to drag.
 *
 * Network: auto-pick the 192.0.2.x interface
 *          (override with UNITREE_IFACE env var)
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

using namespace unitree::robot;
using unitree_hg::msg::dds_::LowCmd_;
using unitree_hg::msg::dds_::LowState_;

// Joint indices (3-DOF waist version)
static constexpr int N_ARM      = 14;
static constexpr int N_TOTAL    = 17;   // 14 arm + 3 waist
static constexpr int WEIGHT_IDX = 29;

static constexpr int JOINT_IDX[N_TOTAL] = {
    15, 16, 17, 18, 19, 20, 21,  // left arm
    22, 23, 24, 25, 26, 27, 28,  // right arm
    12, 13, 14                    // waist yaw/roll/pitch
};

// Control parameters
static constexpr float KP_ARM   = 60.0f;
static constexpr float KD_ARM   = 1.5f;
static constexpr float KP_WAIST = 200.0f;
static constexpr float KD_WAIST = 2.0f;
static constexpr float KD_GCOMP = 0.8f;
static constexpr float ENGAGE_T = 2.0f;
static constexpr int   LOOP_US  = 2000;   // 500 Hz

static const char* ACTION_FILE = "arm7_action.dat";

// Global state
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
    std::cout << "\n[SIGNAL] exit signal received\n";
    g_running = false;
}

// Auto-pick robot intranet interface (192.0.2.7/24)
static std::string pick_robot_iface() {
    if (const char* env = std::getenv("UNITREE_IFACE")) {
        if (env[0]) { std::cout << "[NET] using env iface: " << env << "\n"; return env; }
    }
    struct ifaddrs* ifs = nullptr;
    if (getifaddrs(&ifs) == 0) {
        for (auto* p = ifs; p; p = p->ifa_next) {
            if (!p->ifa_addr || p->ifa_addr->sa_family != AF_INET) continue;
            auto* sin = reinterpret_cast<sockaddr_in*>(p->ifa_addr);
            uint32_t ip = ntohl(sin->sin_addr.s_addr);
            if ((ip & 0xFFFFFF00u) == 0xC0A87B00u) {  // 192.0.2.7/24
                std::string name = p->ifa_name ? p->ifa_name : "";
                std::cout << "[NET] auto-selected iface: " << name
                          << " (IP " << ((ip>>24)&0xff) << "." << ((ip>>16)&0xff)
                          << "." << ((ip>>8)&0xff) << "." << (ip&0xff) << ")\n";
                freeifaddrs(ifs);
                return name;
            }
        }
        freeifaddrs(ifs);
    }
    std::cerr << "[NET][WARN] no 192.0.2.x iface found, falling back to eth0\n";
    return "eth0";
}

// Type aliases
using ArmQ   = std::array<float, N_ARM>;
using TotalQ = std::array<float, N_TOTAL>;

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

// cmd writer (locked to avoid races)
static std::mutex g_cmd_mtx;

static void apply_cmd(LowCmd_& cmd, const TotalQ& q,
                      float arm_kp, float arm_kd,
                      float waist_kp, float waist_kd) {
    std::lock_guard<std::mutex> lk(g_cmd_mtx);
    for (int i = 0; i < N_ARM; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q[i]); mc.kp(arm_kp); mc.kd(arm_kd); mc.dq(0.0f); mc.tau(0.0f);
    }
    for (int i = N_ARM; i < N_TOTAL; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q[i]); mc.kp(waist_kp); mc.kd(waist_kd); mc.dq(0.0f); mc.tau(0.0f);
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

static void ramp_weight_up(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration, const TotalQ& q_hold)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps && g_running; ++i) {
        const float w = static_cast<float>(i + 1) / steps;
        set_weight_locked(cmd, w);
        apply_cmd(cmd, q_hold, KP_ARM, KD_ARM, KP_WAIST, KD_WAIST);
        publish_locked(pub, cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
}

static void ramp_down(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration, const TotalQ& q_hold)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps; ++i) {
        const float w = 1.0f - static_cast<float>(i + 1) / steps;
        set_weight_locked(cmd, w);
        apply_cmd(cmd, q_hold, KP_ARM, KD_ARM, KP_WAIST, KD_WAIST);
        publish_locked(pub, cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
    set_weight_locked(cmd, 0.0f);
    apply_cmd(cmd, q_hold, 0.0f, 0.0f, 0.0f, 0.0f);
    publish_locked(pub, cmd);
}

// ========================================================================
int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    const std::string iface = pick_robot_iface();
    ChannelFactory::Instance()->Init(0, iface);

    auto pub = std::make_shared<ChannelPublisher<LowCmd_>>("rt/arm_sdk");
    pub->InitChannel();

    auto sub = std::make_shared<ChannelSubscriber<LowState_>>("rt/lowstate");
    sub->InitChannel(on_lowstate, 10);

    std::cout << "[WAIT] waiting for lowstate ...\n";
    while (!g_state_received && g_running)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    if (!g_running) return 0;

    LowCmd_ cmd{};
    for (auto& mc : cmd.motor_cmd()) {
        mc.q(0.0f); mc.dq(0.0f); mc.kp(0.0f); mc.kd(0.0f); mc.tau(0.0f);
    }

    std::cout << "===========================================\n"
              << "  G1 Arm7 - Teach Mode\n"
              << "  waist locked, arms free to drag\n"
              << "  Enter = record    q = save & exit\n"
              << "===========================================\n\n";

    TotalQ q_init = get_total_q();
    std::cout << "[INFO] waist initial angles (rad): "
              << std::fixed << std::setprecision(3)
              << q_init[14] << " " << q_init[15] << " " << q_init[16] << "\n";

    apply_cmd(cmd, q_init, KP_ARM, KD_ARM, KP_WAIST, KD_WAIST);
    set_weight_locked(cmd, 0.0f);
    publish_locked(pub, cmd);

    std::cout << "[1/3] engaging (weight 0->1) ...\n";
    ramp_weight_up(pub, cmd, ENGAGE_T, q_init);
    std::cout << "[2/3] engaged, switching to gravity compensation ...\n";

    std::atomic<bool> teach_done{false};

    std::thread gcomp([&]() {
        while (!teach_done.load() && g_running.load()) {
            TotalQ q_now = get_total_q();
            {
                std::lock_guard<std::mutex> lk(g_cmd_mtx);
                for (int i = 0; i < N_ARM; ++i) {
                    auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
                    mc.q(q_now[i]); mc.kp(0.0f); mc.kd(KD_GCOMP);
                    mc.dq(0.0f); mc.tau(0.0f);
                }
                for (int i = N_ARM; i < N_TOTAL; ++i) {
                    auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
                    mc.q(q_init[i]); mc.kp(KP_WAIST); mc.kd(KD_WAIST);
                    mc.dq(0.0f); mc.tau(0.0f);
                }
                cmd.motor_cmd()[WEIGHT_IDX].q(1.0f);
            }
            pub->Write(cmd);
            std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
        }
    });

    std::cout << "[3/3] gravity comp enabled | waist locked | drag arms freely\n\n";

    std::vector<ArmQ> waypoints;
    std::string line;

    while (g_running.load()) {
        std::cout << "[Enter]=record  [q]=save & exit > " << std::flush;
        if (!std::getline(std::cin, line)) break;
        if (line == "q" || line == "Q") break;
        ArmQ snap = get_arm_q();
        waypoints.push_back(snap);
        std::cout << "  * waypoint #" << waypoints.size() << " L[";
        for (int i=0;i<7;++i) std::cout<<(i?" ":"")<<std::fixed<<std::setprecision(3)<<snap[i];
        std::cout << "] R[";
        for (int i=7;i<14;++i) std::cout<<(i>7?" ":"")<<std::fixed<<std::setprecision(3)<<snap[i];
        std::cout << "]\n";
    }

    teach_done = true;
    g_running  = false;
    gcomp.join();

    std::cout << "\n[EXIT] weight 1->0 ...\n";
    TotalQ q_exit = get_total_q();
    for (int i = N_ARM; i < N_TOTAL; ++i) q_exit[i] = q_init[i];
    ramp_down(pub, cmd, ENGAGE_T, q_exit);

    if (!waypoints.empty()) {
        std::ofstream f(ACTION_FILE);
        if (!f) { std::cerr << "[ERROR] cannot write " << ACTION_FILE << "\n"; return 1; }
        f << waypoints.size() << "\n" << N_ARM << "\n";
        for (const auto& wp : waypoints)
            for (int i = 0; i < N_ARM; ++i)
                f << std::fixed << std::setprecision(6)
                  << wp[i] << (i < N_ARM-1 ? " " : "\n");
        std::cout << "[SAVED] " << waypoints.size() << " waypoints -> " << ACTION_FILE << "\n";
    } else {
        std::cout << "[WARN] no waypoints recorded, not saved\n";
    }
    return 0;
}
