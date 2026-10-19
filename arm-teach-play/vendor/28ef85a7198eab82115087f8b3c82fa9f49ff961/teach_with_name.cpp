/**
 * teach_with_name.cpp — G1 Arm7 Kinesthetic Teaching with motion name
 * =====================================================================
 * 同 teach.cpp，区别：
 *   - 保存时询问动作名（如 punch_left）
 *   - 输出到 motions/<name>.dat
 *   - 文件每行 = [duration_sec] [14 joint angles]，默认 0.3s/段
 *   - 已存在同名文件时提示是否覆盖
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
#include <sys/stat.h>
#include <sys/types.h>
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

static constexpr int N_ARM      = 14;
static constexpr int N_TOTAL    = 17;
static constexpr int WEIGHT_IDX = 29;

static constexpr int JOINT_IDX[N_TOTAL] = {
    15, 16, 17, 18, 19, 20, 21,
    22, 23, 24, 25, 26, 27, 28,
    12, 13, 14
};

static constexpr float KP_ARM   = 60.0f;
static constexpr float KD_ARM   = 1.5f;
static constexpr float KP_WAIST = 200.0f;
static constexpr float KD_WAIST = 2.0f;
static constexpr float KD_GCOMP = 0.8f;
static constexpr float ENGAGE_T = 2.0f;
static constexpr int   LOOP_US  = 2000;

static constexpr float DEFAULT_SEG_DURATION = 0.3f;
static const char* MOTIONS_DIR = "motions";

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

static bool ensure_dir(const std::string& path) {
    struct stat st{};
    if (stat(path.c_str(), &st) == 0) return S_ISDIR(st.st_mode);
    return mkdir(path.c_str(), 0755) == 0;
}

static bool file_exists(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0;
}

static bool is_valid_name(const std::string& s) {
    if (s.empty()) return false;
    for (char c : s) {
        if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-'))
            return false;
    }
    return true;
}

int main() {
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);

    const std::string iface = pick_robot_iface();
    ChannelFactory::Instance()->Init(0, iface);

    auto pub = std::make_shared<ChannelPublisher<LowCmd_>>("rt/arm_sdk");
    pub->InitChannel();

    auto sub = std::make_shared<ChannelSubscriber<LowState_>>("rt/lowstate");
    sub->InitChannel(on_lowstate, 10);

    std::cout << "[WAIT] 等待 lowstate 数据...\n";
    while (!g_state_received && g_running)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    if (!g_running) return 0;

    LowCmd_ cmd{};
    for (auto& mc : cmd.motor_cmd()) {
        mc.q(0.0f); mc.dq(0.0f); mc.kp(0.0f); mc.kd(0.0f); mc.tau(0.0f);
    }

    std::cout << "===========================================\n"
              << "  G1 Arm7 - 教学模式 (带命名)\n"
              << "  腰部全程锁定，手臂可自由拖动\n"
              << "  Enter = 记录    q = 命名保存并退出\n"
              << "===========================================\n\n";

    TotalQ q_init = get_total_q();
    std::cout << "[INFO] 腰部初始角 (rad): "
              << std::fixed << std::setprecision(3)
              << q_init[14] << " " << q_init[15] << " " << q_init[16] << "\n";

    apply_cmd(cmd, q_init, KP_ARM, KD_ARM, KP_WAIST, KD_WAIST);
    set_weight_locked(cmd, 0.0f);
    publish_locked(pub, cmd);

    std::cout << "[1/3] 接管 (weight 0→1) ...\n";
    ramp_weight_up(pub, cmd, ENGAGE_T, q_init);
    std::cout << "[2/3] 接管完成，切换重力补偿...\n";

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

    std::cout << "[3/3] 重力补偿启用 | 腰部锁定 | 可拖动手臂\n\n";

    std::vector<ArmQ> waypoints;
    std::string line;

    while (g_running.load()) {
        std::cout << "[Enter]=记录  [q]=命名保存并退出 > " << std::flush;
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

    std::cout << "\n[EXIT] weight 1→0 ...\n";
    TotalQ q_exit = get_total_q();
    for (int i = N_ARM; i < N_TOTAL; ++i) q_exit[i] = q_init[i];
    ramp_down(pub, cmd, ENGAGE_T, q_exit);

    if (waypoints.empty()) {
        std::cout << "[WARN] 无 waypoint，未保存\n";
        return 0;
    }

    if (!ensure_dir(MOTIONS_DIR)) {
        std::cerr << "[ERROR] 无法创建目录 " << MOTIONS_DIR << "\n";
        return 1;
    }

    std::string name;
    while (true) {
        std::cout << "动作名 (字母/数字/_/-，如 punch_left): " << std::flush;
        if (!std::getline(std::cin, name)) return 0;
        if (!is_valid_name(name)) {
            std::cout << "[WARN] 非法名字，请重输\n"; continue;
        }
        std::string path = std::string(MOTIONS_DIR) + "/" + name + ".dat";
        if (file_exists(path)) {
            std::cout << "[WARN] " << path << " 已存在，覆盖？[y/N]: " << std::flush;
            std::string ans; std::getline(std::cin, ans);
            if (ans != "y" && ans != "Y") continue;
        }
        std::ofstream f(path);
        if (!f) { std::cerr << "[ERROR] 无法写入 " << path << "\n"; return 1; }
        f << waypoints.size() << "\n" << N_ARM << "\n";
        for (const auto& wp : waypoints) {
            f << std::fixed << std::setprecision(6) << DEFAULT_SEG_DURATION;
            for (int i = 0; i < N_ARM; ++i) f << " " << wp[i];
            f << "\n";
        }
        std::cout << "[SAVED] " << waypoints.size() << " waypoints (每段 "
                  << DEFAULT_SEG_DURATION << "s) -> " << path << "\n";
        break;
    }
    return 0;
}
