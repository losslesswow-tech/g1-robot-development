/**
 * play.cpp - G1 Arm7 Joystick Playback  [Final]
 * ================================================
 * Reference: unitree_sdk2/example/g1/high_level/g1_arm7_sdk_dds_example.cpp
 *
 * Waist: locked at initial angle on startup, KP=200/KD=2 throughout.
 *
 * Controls:
 *   L1     -> Do action (forward playback)
 *   L2     -> Retract (reverse playback back to rest pose)
 *   R1+R2  -> Exit
 *   Ctrl+C -> Exit
 *
 * LED: green=rest, blue=moving, red(255,80,80)=action held, off=exit
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
#include <stdexcept>
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

// Joint indices (3-DOF waist version)
static constexpr int N_ARM      = 14;
static constexpr int N_TOTAL    = 17;
static constexpr int WEIGHT_IDX = 29;

static constexpr int JOINT_IDX[N_TOTAL] = {
    15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28,
    12, 13, 14
};

// Control parameters
static constexpr float KP_ARM   = 60.0f;
static constexpr float KD_ARM   = 1.5f;
static constexpr float KP_WAIST = 200.0f;
static constexpr float KD_WAIST = 2.0f;
static constexpr float ENGAGE_T = 2.0f;
static constexpr float INTERP_T = 0.3f;   // time per waypoint segment (seconds) -- 10x speed
static constexpr int   LOOP_US  = 2000;

// Remote key bits
static constexpr uint16_t KEY_R1 = (1 << 0);
static constexpr uint16_t KEY_L1 = (1 << 1);
static constexpr uint16_t KEY_R2 = (1 << 4);
static constexpr uint16_t KEY_L2 = (1 << 5);

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
            if ((ip & 0xFFFFFF00u) == 0xC0A87B00u) {
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

static uint16_t read_keys() {
    // wireless_remote format: [0..1]=head, [2..3]=btn(uint16), [4..]=stick floats
    uint16_t k = 0;
    std::lock_guard<std::mutex> lk(g_state_mtx);
    std::memcpy(&k, &g_state.wireless_remote()[2], 2);
    return k;
}

// R1+R2 pressed together -> exit (can trigger at any time)
static bool check_exit_combo() {
    const uint16_t k = read_keys();
    if ((k & KEY_R1) && (k & KEY_R2)) {
        std::cout << "\n[EXIT] R1+R2 pressed\n";
        g_running = false;
        return true;
    }
    return false;
}

// cmd writer
static void apply_cmd(LowCmd_& cmd,
                      const ArmQ& arm_q,
                      const TotalQ& q_init)
{
    for (int i = 0; i < N_ARM; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(arm_q[i]); mc.kp(KP_ARM); mc.kd(KD_ARM); mc.dq(0.0f); mc.tau(0.0f);
    }
    for (int i = N_ARM; i < N_TOTAL; ++i) {
        auto& mc = cmd.motor_cmd()[JOINT_IDX[i]];
        mc.q(q_init[i]); mc.kp(KP_WAIST); mc.kd(KD_WAIST); mc.dq(0.0f); mc.tau(0.0f);
    }
}

static void set_weight(LowCmd_& cmd, float w) {
    cmd.motor_cmd()[WEIGHT_IDX].q(std::max(0.0f, std::min(1.0f, w)));
}

static ArmQ lerp_arm(const ArmQ& a, const ArmQ& b, float t) {
    ArmQ r{};
    for (int i = 0; i < N_ARM; ++i) r[i] = a[i] + t*(b[i]-a[i]);
    return r;
}

// LED: retry 3x and print error code to help debug
static void set_led(unitree::robot::g1::AudioClient& audio,
                    uint8_t r, uint8_t g, uint8_t b) {
    for (int attempt = 0; attempt < 3; ++attempt) {
        int32_t ret = -1;
        try { ret = audio.LedControl(r, g, b); } catch (...) { ret = -999; }
        if (ret == 0) return;
        std::cerr << "[LED] LedControl(" << (int)r << "," << (int)g << ","
                  << (int)b << ") attempt#" << (attempt+1) << " returned " << ret << "\n";
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

// weight ramp
static void ramp_weight_up(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration,
    const ArmQ& arm_hold, const TotalQ& q_init)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps && g_running; ++i) {
        set_weight(cmd, static_cast<float>(i+1)/steps);
        apply_cmd(cmd, arm_hold, q_init);
        pub->Write(cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
}

static void ramp_down(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd, float duration,
    const ArmQ& arm_hold, const TotalQ& q_init)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    for (int i = 0; i < steps; ++i) {
        set_weight(cmd, 1.0f - static_cast<float>(i+1)/steps);
        apply_cmd(cmd, arm_hold, q_init);
        pub->Write(cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
    set_weight(cmd, 0.0f);
    apply_cmd(cmd, arm_hold, q_init);
    pub->Write(cmd);
}

// Interpolated motion between waypoints. R1+R2 can interrupt at any time.
static ArmQ interp_move(
    std::shared_ptr<ChannelPublisher<LowCmd_>>& pub,
    LowCmd_& cmd,
    const ArmQ& q_from, const ArmQ& q_to,
    float duration, const TotalQ& q_init)
{
    const int steps = static_cast<int>(duration / (LOOP_US * 1e-6f));
    ArmQ q_cur = q_from;
    for (int i = 0; i < steps && g_running; ++i) {
        // Check exit combo every 50 steps (~100ms)
        if ((i % 50) == 0 && check_exit_combo()) break;
        q_cur = lerp_arm(q_from, q_to, static_cast<float>(i+1)/steps);
        apply_cmd(cmd, q_cur, q_init);
        pub->Write(cmd);
        std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
    }
    return q_cur;
}

// Load action file
static std::vector<ArmQ> load_action() {
    std::ifstream f(ACTION_FILE);
    if (!f) throw std::runtime_error(
        std::string("file not found: ") + ACTION_FILE + " (run ./teach first)");
    int n_wp=0, n_j=0;
    f >> n_wp >> n_j;
    if (n_j != N_ARM) throw std::runtime_error("joint count mismatch, please re-run ./teach");
    std::vector<ArmQ> wps(n_wp);
    for (auto& wp : wps) for (float& v : wp) f >> v;
    if (f.fail()) throw std::runtime_error("file read failed");
    return wps;
}

// ========================================================================
int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    std::vector<ArmQ> waypoints;
    try { waypoints = load_action(); }
    catch (const std::exception& e) {
        std::cerr << "[ERROR] " << e.what() << "\n"; return 1;
    }

    std::cout << "===========================================\n"
              << "  G1 Arm7 - Playback Mode\n"
              << "  loaded " << waypoints.size() << " waypoint(s)\n"
              << "  L1 = Do action    L2 = Retract\n"
              << "  R1+R2 = Exit    Ctrl+C = Exit\n"
              << "===========================================\n\n";

    const std::string iface = pick_robot_iface();
    ChannelFactory::Instance()->Init(0, iface);

    auto pub = std::make_shared<ChannelPublisher<LowCmd_>>("rt/arm_sdk");
    pub->InitChannel();

    auto sub = std::make_shared<ChannelSubscriber<LowState_>>("rt/lowstate");
    sub->InitChannel(on_lowstate, 10);

    unitree::robot::g1::AudioClient audio;
    audio.SetTimeout(3.0f);
    audio.Init();
    // Max out the volume so the voice prompt is clearly audible
    {
        int32_t ret = audio.SetVolume(100);
        if (ret != 0) std::cerr << "[AUDIO] SetVolume(100) returned " << ret << "\n";
    }
    // Preempt LED: blank a few times to override the system daemon's color
    for (int i = 0; i < 5; ++i) {
        set_led(audio, 0, 0, 0);
        std::this_thread::sleep_for(std::chrono::milliseconds(80));
    }

    std::cout << "[WAIT] waiting for lowstate ...\n";
    while (!g_state_received && g_running)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    if (!g_running) return 0;

    LowCmd_ cmd{};
    for (auto& mc : cmd.motor_cmd()) {
        mc.q(0.0f); mc.dq(0.0f); mc.kp(0.0f); mc.kd(0.0f); mc.tau(0.0f);
    }

    TotalQ q_init = get_total_q();
    std::cout << "[INFO] waist initial angles (rad): "
              << std::fixed << std::setprecision(3)
              << q_init[14] << " " << q_init[15] << " " << q_init[16] << "\n";

    ArmQ arm_init{};
    for (int i = 0; i < N_ARM; ++i) arm_init[i] = q_init[i];

    apply_cmd(cmd, arm_init, q_init);
    set_weight(cmd, 0.0f);
    pub->Write(cmd);

    std::cout << "[1/2] engaging (weight 0->1) ...\n";
    ramp_weight_up(pub, cmd, ENGAGE_T, arm_init, q_init);

    static constexpr uint8_t RED_R = 255, RED_G = 80, RED_B = 80;

    set_led(audio, 0, 255, 0);
    // Startup voice prompt (English, once, on successful engage)
    {
        int32_t ret = audio.TtsMaker("welcome to the interactive combat league", 1);
        if (ret != 0) std::cerr << "[TTS] TtsMaker (enter) returned " << ret << "\n";
    }
    std::cout << "[2/2] engaged | green LED | press L1 to do action, L2 to retract\n\n";

    ArmQ  q_rest      = arm_init;
    ArmQ  q_current   = q_rest;
    bool  is_up       = false;
    bool  prev_l1     = false;
    bool  prev_l2     = false;
    int   led_refresh = 0;

    while (g_running) {
        const uint16_t keys = read_keys();

        // R1+R2 pressed together -> exit (highest priority)
        if ((keys & KEY_R1) && (keys & KEY_R2)) {
            std::cout << "[EXIT] R1+R2 pressed\n";
            g_running = false;
            break;
        }

        const bool l1_cur  = (keys & KEY_L1) != 0;
        const bool l2_cur  = (keys & KEY_L2) != 0;
        const bool l1_rise = l1_cur && !prev_l1;
        const bool l2_rise = l2_cur && !prev_l2;
        prev_l1 = l1_cur;
        prev_l2 = l2_cur;

        if (l1_rise && !is_up) {
            std::cout << "[ACTION] do action (L1) | blue LED ...\n";
            set_led(audio, 0, 0, 255);

            ArmQ q_from = get_arm_q();
            for (size_t i = 0; i < waypoints.size() && g_running; ++i) {
                std::cout << "  -> wp" << i+1 << "/" << waypoints.size() << "\n";
                q_from = interp_move(pub, cmd, q_from, waypoints[i], INTERP_T, q_init);
            }
            if (!g_running) break;

            q_current = waypoints.back();
            is_up = true;

            for (int i = 0; i < 250 && g_running; ++i) {
                apply_cmd(cmd, q_current, q_init);
                pub->Write(cmd);
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            { const uint16_t k = read_keys();
              prev_l1 = (k & KEY_L1) != 0;
              prev_l2 = (k & KEY_L2) != 0; }

            set_led(audio, RED_R, RED_G, RED_B);
            std::cout << "[ACTION] action held | red LED | press L2 to retract\n\n";

        } else if (l2_rise && is_up) {
            std::cout << "[ACTION] retract (L2) | blue LED ...\n";
            set_led(audio, 0, 0, 255);

            ArmQ q_from = get_arm_q();
            for (int i=(int)waypoints.size()-2; i>=0 && g_running; --i) {
                std::cout << "  <- wp" << i+1 << "/" << waypoints.size() << "\n";
                q_from = interp_move(pub, cmd, q_from, waypoints[i], INTERP_T, q_init);
            }
            if (g_running) {
                std::cout << "  <- rest pose\n";
                interp_move(pub, cmd, q_from, q_rest, INTERP_T, q_init);
            }
            if (!g_running) break;

            q_current = q_rest;
            is_up = false;

            for (int i = 0; i < 250 && g_running; ++i) {
                apply_cmd(cmd, q_current, q_init);
                pub->Write(cmd);
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            { const uint16_t k = read_keys();
              prev_l1 = (k & KEY_L1) != 0;
              prev_l2 = (k & KEY_L2) != 0; }

            set_led(audio, 0, 255, 0);
            std::cout << "[ACTION] retract done | green LED | press L1 to do action\n\n";

        } else {
            apply_cmd(cmd, q_current, q_init);
            pub->Write(cmd);
            // Refresh LED every ~500ms to preempt the system daemon
            if (++led_refresh >= 250) {
                led_refresh = 0;
                if (is_up) set_led(audio, RED_R, RED_G, RED_B);
                else       set_led(audio, 0, 255, 0);
            }
            std::this_thread::sleep_for(std::chrono::microseconds(LOOP_US));
        }
    }

    std::cout << "[EXIT] weight 1->0 ...\n";
    set_led(audio, 0, 0, 0);
    // Exit voice prompt (fires before ramp_down so it plays while the
    // arm is smoothly releasing)
    {
        int32_t ret = audio.TtsMaker("play mode terminated", 1);
        if (ret != 0) std::cerr << "[TTS] TtsMaker (exit) returned " << ret << "\n";
    }
    ArmQ q_exit = get_arm_q();
    ramp_down(pub, cmd, ENGAGE_T, q_exit, q_init);

    std::cout << "[EXIT] done\n";
    return 0;
}
