/**
 * watchdog.cpp - G1 Arm7 Joystick Watchdog
 * ================================================
 * Background daemon. Subscribes to rt/lowstate, waits for L2+R1 held
 * for >=300ms, then fork+exec's ./play and waitpid's until play exits
 * before re-arming.
 *
 * Design constraints (important):
 *   - Subscriber only, never publishes any DDS topic - zero impact on
 *     other programs.
 *   - Single instance (flock /tmp/g1_arm7_watchdog.lock).
 *   - Does not touch LED or arm_sdk.
 *   - While play is running, all keys are ignored - no re-launch.
 *   - After play exits, requires the combo to be fully released for
 *     >=200ms before re-arming.
 *
 * Network: reuses play.cpp's auto-selection logic
 *          (override with UNITREE_IFACE env var).
 */

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <arpa/inet.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <sys/file.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

using namespace unitree::robot;
using unitree_hg::msg::dds_::LowState_;

// Remote key bits (must match play.cpp)
static constexpr uint16_t KEY_R1 = (1 << 0);
static constexpr uint16_t KEY_L1 = (1 << 1);
static constexpr uint16_t KEY_R2 = (1 << 4);
static constexpr uint16_t KEY_L2 = (1 << 5);
// Trigger combo: L2 + R1
static constexpr uint16_t TRIGGER_MASK = KEY_L2 | KEY_R1;

// Timing parameters
static constexpr int POLL_MS        = 10;    // 100 Hz polling
static constexpr int HOLD_MS        = 300;   // must hold combo this long to trigger
static constexpr int RELEASE_MS     = 200;   // must release for this long after play exits before re-arming

// Absolute path to play binary
static const char* PLAY_BIN = "/home/operator/g1_arm7/build/play";
// cwd for play so it can find arm7_action.dat
static const char* PLAY_CWD = "/home/operator/g1_arm7/build";

// Single-instance lock file
static const char* LOCK_PATH = "/tmp/g1_arm7_watchdog.lock";

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
    std::cout << "\n[WD] exit signal received\n";
    g_running = false;
}

static uint16_t read_keys() {
    uint16_t k = 0;
    std::lock_guard<std::mutex> lk(g_state_mtx);
    std::memcpy(&k, &g_state.wireless_remote()[2], 2);
    return k;
}

// Auto-pick 192.0.2.x interface
static std::string pick_robot_iface() {
    if (const char* env = std::getenv("UNITREE_IFACE")) {
        if (env[0]) { std::cout << "[WD][NET] use env iface: " << env << "\n"; return env; }
    }
    struct ifaddrs* ifs = nullptr;
    if (getifaddrs(&ifs) == 0) {
        for (auto* p = ifs; p; p = p->ifa_next) {
            if (!p->ifa_addr || p->ifa_addr->sa_family != AF_INET) continue;
            auto* sin = reinterpret_cast<sockaddr_in*>(p->ifa_addr);
            uint32_t ip = ntohl(sin->sin_addr.s_addr);
            if ((ip & 0xFFFFFF00u) == 0xC0A87B00u) {
                std::string name = p->ifa_name ? p->ifa_name : "";
                std::cout << "[WD][NET] auto-selected iface: " << name << "\n";
                freeifaddrs(ifs);
                return name;
            }
        }
        freeifaddrs(ifs);
    }
    std::cerr << "[WD][NET][WARN] no 192.0.2.x iface, fallback to eth0\n";
    return "eth0";
}

// Single-instance protection.
// Returns fd (>=0) on success, -1 if another watchdog already holds the lock.
static int acquire_single_instance_lock() {
    int fd = ::open(LOCK_PATH, O_RDWR | O_CREAT, 0644);
    if (fd < 0) {
        std::cerr << "[WD] open lock failed: " << std::strerror(errno) << "\n";
        return -1;
    }
    if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
        std::cerr << "[WD] another watchdog already running (lock held)\n";
        ::close(fd);
        return -1;
    }
    return fd;
}

// fork + exec to launch play
static pid_t spawn_play() {
    pid_t pid = ::fork();
    if (pid < 0) {
        std::cerr << "[WD] fork failed: " << std::strerror(errno) << "\n";
        return -1;
    }
    if (pid == 0) {
        if (::chdir(PLAY_CWD) != 0) {
            std::cerr << "[WD][child] chdir(" << PLAY_CWD << ") failed\n";
            _exit(127);
        }
        char* const argv[] = { const_cast<char*>(PLAY_BIN), nullptr };
        ::execv(PLAY_BIN, argv);
        // execv only returns on failure
        std::cerr << "[WD][child] execv(" << PLAY_BIN << ") failed: "
                  << std::strerror(errno) << "\n";
        _exit(127);
    }
    return pid;
}

// ========================================================================
int main() {
    // Keep stdout/stderr line-buffered even when redirected to a file,
    // so `tail -f` shows events in real time.
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    std::setvbuf(stderr, nullptr, _IOLBF, 0);

    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);
    // We waitpid() on our child ourselves, no need to ignore SIGCHLD.

    int lock_fd = acquire_single_instance_lock();
    if (lock_fd < 0) return 1;

    std::cout << "===========================================\n"
              << "  G1 Arm7 - Watchdog\n"
              << "  trigger: L2 + R1 held for " << HOLD_MS << " ms\n"
              << "  launches: " << PLAY_BIN << "\n"
              << "===========================================\n";

    const std::string iface = pick_robot_iface();
    ChannelFactory::Instance()->Init(0, iface);

    auto sub = std::make_shared<ChannelSubscriber<LowState_>>("rt/lowstate");
    sub->InitChannel(on_lowstate, 10);

    std::cout << "[WD] waiting for lowstate ...\n";
    while (!g_state_received && g_running)
        std::this_thread::sleep_for(std::chrono::milliseconds(POLL_MS));
    if (!g_running) return 0;
    std::cout << "[WD] ready, listening for L2+R1\n";

    // Boot-success voice prompt.
    // Uses the G1 on-board TTS engine via DDS RPC on the internal
    // 192.0.2.x LAN -- works regardless of WiFi / public internet
    // status.  If the internal bus is broken the RPC just times out and
    // returns a non-zero code; we log and carry on.
    {
        unitree::robot::g1::AudioClient audio;
        audio.SetTimeout(3.0f);
        audio.Init();
        int32_t rv = audio.SetVolume(100);
        if (rv != 0) std::cerr << "[WD][AUDIO] SetVolume(100) returned " << rv << "\n";
        int32_t rt = audio.TtsMaker("motion activated", 1);  // 1 = English
        if (rt != 0) std::cerr << "[WD][TTS] TtsMaker returned " << rt << "\n";
        else         std::cout << "[WD] announced 'motion activated'\n";
    }

    // State machine
    enum State { IDLE, ARMING };
    State st = IDLE;
    auto  t_start = std::chrono::steady_clock::now();

    while (g_running) {
        const uint16_t k = read_keys();
        const bool combo = (k & TRIGGER_MASK) == TRIGGER_MASK;
        const auto  now  = std::chrono::steady_clock::now();

        if (st == IDLE) {
            if (combo) {
                st = ARMING;
                t_start = now;
                std::cout << "[WD] combo detected, arming (hold " << HOLD_MS << " ms)...\n";
            }
        } else { // ARMING
            if (!combo) {
                st = IDLE;
                std::cout << "[WD] released before threshold, cancel\n";
            } else {
                auto held = std::chrono::duration_cast<std::chrono::milliseconds>(
                                now - t_start).count();
                if (held >= HOLD_MS) {
                    std::cout << "[WD] fire! launching play (pid will follow) ...\n";

                    pid_t pid = spawn_play();
                    if (pid > 0) {
                        int wstatus = 0;
                        while (true) {
                            pid_t w = ::waitpid(pid, &wstatus, 0);
                            if (w == pid) break;
                            if (w == -1 && errno == EINTR) continue;
                            if (w == -1) {
                                std::cerr << "[WD] waitpid err: "
                                          << std::strerror(errno) << "\n";
                                break;
                            }
                        }
                        if (WIFEXITED(wstatus)) {
                            std::cout << "[WD] play exited code="
                                      << WEXITSTATUS(wstatus) << "\n";
                        } else if (WIFSIGNALED(wstatus)) {
                            std::cout << "[WD] play killed by signal "
                                      << WTERMSIG(wstatus) << "\n";
                        }
                    }

                    // Wait for the combo to stay released for RELEASE_MS
                    // so a still-held key doesn't auto-relaunch.
                    std::cout << "[WD] waiting for L2+R1 release >= "
                              << RELEASE_MS << " ms ...\n";
                    auto  released_since = std::chrono::steady_clock::now();
                    bool  was_pressed    = true;
                    while (g_running) {
                        const uint16_t kk = read_keys();
                        const bool pressed = (kk & TRIGGER_MASK) != 0;
                        if (pressed) {
                            released_since = std::chrono::steady_clock::now();
                            was_pressed = true;
                        } else {
                            if (was_pressed) {
                                released_since = std::chrono::steady_clock::now();
                                was_pressed = false;
                            }
                            auto dur = std::chrono::duration_cast<
                                std::chrono::milliseconds>(
                                    std::chrono::steady_clock::now() - released_since).count();
                            if (dur >= RELEASE_MS) break;
                        }
                        std::this_thread::sleep_for(std::chrono::milliseconds(POLL_MS));
                    }

                    st = IDLE;
                    std::cout << "[WD] re-armed, listening again\n\n";
                }
            }
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(POLL_MS));
    }

    std::cout << "[WD] bye\n";
    return 0;
}
