// G1's documented device-state client lives in the SDK b2 namespace.
// Only g1_arm_example can be stopped. No motion publisher or service start API.
#include <unitree/robot/b2/robot_state/robot_state_client.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
#include <net/if.h>

using unitree::robot::b2::RobotStateClient;
using unitree::robot::b2::ServiceState;
constexpr const char* Service = "g1_arm_example";

int list(RobotStateClient& client, const char* phase, int& target_status, int& target_protect) {
    std::vector<ServiceState> states;
    const int rc = client.ServiceList(states);
    target_status = -1; target_protect = -1;
    std::cout << "{\"event\":\"service_list\",\"phase\":" << std::quoted(phase)
              << ",\"return_code\":" << rc << ",\"services\":[";
    bool first = true;
    for (const auto& state : states) {
        if (std::string(phase) == "after" && state.name != Service) continue;
        if (!first) std::cout << ',';
        first = false;
        std::cout << "{\"name\":" << std::quoted(state.name) << ",\"status_raw\":"
                  << state.status << ",\"protect_raw\":" << state.protect << '}';
        if (state.name == Service) { target_status = state.status; target_protect = state.protect; }
    }
    std::cout << "]}" << std::endl;
    return rc;
}

int run(const std::string& nic, bool stop) {
    unitree::robot::ChannelFactory::Instance()->Init(0, nic);
    RobotStateClient client;
    client.SetTimeout(3.0f); client.Init();
    const auto local = client.GetApiVersion(), remote = client.GetServerApiVersion();
    std::cout << "{\"event\":\"api_version\",\"client\":" << std::quoted(local)
              << ",\"server\":" << std::quoted(remote) << '}' << std::endl;
    if (local.empty() || remote.empty() || local != remote) return 4;
    int status, protect;
    if (list(client, "before", status, protect) || status < 0) return 5;
    if (!stop) return 0;
    // The official stop example expects status != 0 after switch=0. The
    // stopped state is 1 on this interface; reject transitional/unknown values.
    if (status == 1) {
        std::cout << "{\"event\":\"already_stopped\",\"switch_calls\":0}" << std::endl;
        return 0;
    }
    if (status != 0 || protect != 0) return 6;
    unitree::robot::g1::LocoClient loco;
    loco.SetTimeout(3.0f); loco.Init();
    int id=-1, mode=-1;
    const int id_rc=loco.GetFsmId(id), mode_rc=loco.GetFsmMode(mode);
    std::cout << "{\"event\":\"mode_before_stop\",\"id_return_code\":" << id_rc
              << ",\"mode_return_code\":" << mode_rc << ",\"fsm_id\":" << id
              << ",\"fsm_mode\":" << mode << '}' << std::endl;
    if (id_rc || mode_rc || id != 501 || mode != 0) return 7;
    int reply=-1;
    const int rc=client.ServiceSwitch(Service, 0, reply); // exactly one stop request
    std::cout << "{\"event\":\"service_stop\",\"name\":\"g1_arm_example\",\"switch\":0,"
                 "\"return_code\":" << rc << ",\"status_raw\":" << reply << '}' << std::endl;
    if (rc || reply != 1) return 8;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(12);
    while (std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        if (!list(client, "after", status, protect) && status == 1) {
            std::cout << "{\"marker\":\"G1_BUILTIN_ARM_STOP_VERIFIED\",\"status_raw\":1,"
                         "\"switch_calls\":1,\"motion_commands_sent\":0}" << std::endl;
            return 0;
        }
    }
    return 9;
}

int main(int argc, char** argv) {
    if (argc != 3 || (std::string(argv[1]) != "--query" && std::string(argv[1]) != "--stop")) {
        std::cerr << "Use --query NETWORK_INTERFACE or --stop NETWORK_INTERFACE\n"; return 2;
    }
    if (!if_nametoindex(argv[2])) { std::cerr << "network_interface_not_found\n"; return 2; }
    int result=1;
    try { result=run(argv[2], std::string(argv[1]) == "--stop"); }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; }
    unitree::robot::ChannelFactory::Instance()->Release();
    return result;
}
