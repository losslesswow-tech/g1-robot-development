/**
 * tts_probe.cpp - G1 Arm7 TTS speaker_id scanner
 * ================================================
 * Iterates speaker_id from MIN_ID to MAX_ID, speaks a numbered phrase
 * for each and logs the AudioClient::TtsMaker return code.
 *
 * Usage:
 *   ./tts_probe              # scan 0..15
 *   ./tts_probe 16 31        # scan 16..31
 *
 * Only touches AudioClient (TTS + volume). Does NOT publish arm_sdk,
 * LED, or any control topic -- safe to run alongside watchdog/play.
 */

#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/g1/audio/g1_audio_client.hpp>

using namespace unitree::robot;

static std::string pick_robot_iface() {
    if (const char* env = std::getenv("UNITREE_IFACE")) {
        if (env[0]) { std::cout << "[NET] use env iface: " << env << "\n"; return env; }
    }
    struct ifaddrs* ifs = nullptr;
    if (getifaddrs(&ifs) == 0) {
        for (auto* p = ifs; p; p = p->ifa_next) {
            if (!p->ifa_addr || p->ifa_addr->sa_family != AF_INET) continue;
            auto* sin = reinterpret_cast<sockaddr_in*>(p->ifa_addr);
            uint32_t ip = ntohl(sin->sin_addr.s_addr);
            if ((ip & 0xFFFFFF00u) == 0xC0A87B00u) {
                std::string name = p->ifa_name ? p->ifa_name : "";
                std::cout << "[NET] auto-selected iface: " << name << "\n";
                freeifaddrs(ifs);
                return name;
            }
        }
        freeifaddrs(ifs);
    }
    return "eth0";
}

static const char* number_word(int n) {
    static const char* w[] = {"zero","one","two","three","four","five","six","seven",
                              "eight","nine","ten","eleven","twelve","thirteen",
                              "fourteen","fifteen","sixteen","seventeen","eighteen",
                              "nineteen","twenty","twenty one","twenty two","twenty three",
                              "twenty four","twenty five","twenty six","twenty seven",
                              "twenty eight","twenty nine","thirty","thirty one"};
    if (n >= 0 && n < 32) return w[n];
    return "unknown";
}

int main(int argc, char** argv) {
    int min_id = 0, max_id = 15;
    if (argc >= 2) min_id = std::atoi(argv[1]);
    if (argc >= 3) max_id = std::atoi(argv[2]);
    if (max_id < min_id) std::swap(min_id, max_id);

    std::cout << "===========================================\n"
              << "  TTS speaker_id probe: scanning [" << min_id << ".." << max_id << "]\n"
              << "===========================================\n";

    ChannelFactory::Instance()->Init(0, pick_robot_iface());

    unitree::robot::g1::AudioClient audio;
    audio.SetTimeout(5.0f);
    audio.Init();
    int32_t vret = audio.SetVolume(100);
    std::cout << "[AUDIO] SetVolume(100) -> " << vret << "\n\n";

    struct Row { int id; int32_t ret; };
    std::vector<Row> rows;

    for (int id = min_id; id <= max_id; ++id) {
        std::string text = std::string("speaker ") + number_word(id) +
                           ", this is voice " + number_word(id);
        std::cout << "[SAY] id=" << id << "  text=\"" << text << "\"  -> " << std::flush;
        int32_t ret = -1;
        try { ret = audio.TtsMaker(text, id); } catch (...) { ret = -999; }
        std::cout << "ret=" << ret << "\n";
        rows.push_back({id, ret});
        // Wait enough for the phrase to finish playing before moving on.
        std::this_thread::sleep_for(std::chrono::seconds(4));
    }

    std::cout << "\n===========================================\n"
              << "  Summary  (ret=0 means RPC accepted; listen to tell male/female)\n"
              << "===========================================\n";
    for (const auto& r : rows) {
        std::cout << "  speaker_id " << (r.id<10?" ":"") << r.id
                  << " -> " << (r.ret==0 ? "OK " : "ERR") << "  (" << r.ret << ")\n";
    }
    std::cout << "\nDone. Note which id numbers corresponded to male voices.\n";
    return 0;
}
