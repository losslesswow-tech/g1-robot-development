// Query-only RPCs. No motion, action execution, or mode-changing calls.
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/robot/g1/arm/g1_arm_action_client.hpp>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2 && (argc != 3 || std::string(argv[2]) != "--include-actions")) {
        std::cerr << "Usage: g1_mode_query NETWORK_INTERFACE [--include-actions]\n";
        return 2;
    }
    try {
        unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
        unitree::robot::g1::LocoClient loco;
        loco.Init();
        loco.SetTimeout(10.0f);
        int id = -1, mode = -1;
        const auto id_ret = loco.GetFsmId(id);
        const auto mode_ret = loco.GetFsmMode(mode);
        int balance = -1;
        const auto balance_ret = loco.GetBalanceMode(balance);
        const bool include_actions = argc == 3;
        std::string actions;
        int actions_ret = -1;
        if (include_actions) {
            unitree::robot::g1::G1ArmActionClient arm;
            arm.Init();
            arm.SetTimeout(3.0f);
            actions_ret = arm.GetActionList(actions);
        }
        std::cout << "{\"marker\":\"G1_MODE_QUERY_READONLY\",\"motion_commands_sent\":0,"
                  << "\"fsm_id_return_code\":" << id_ret << ",\"fsm_id\":";
        if (id_ret == 0) std::cout << id; else std::cout << "null";
        std::cout << ",\"fsm_mode_return_code\":" << mode_ret << ",\"fsm_mode\":";
        if (mode_ret == 0) std::cout << mode; else std::cout << "null";
        std::cout << ",\"balance_mode_return_code\":" << balance_ret << ",\"balance_mode_raw\":";
        if (balance_ret == 0) std::cout << balance; else std::cout << "null";
        std::cout << ",\"action_list_queried\":" << (include_actions ? "true" : "false")
                  << ",\"action_list_return_code\":";
        if (include_actions) std::cout << actions_ret; else std::cout << "null";
        std::cout << "}\n";
        // Kept separately as received, so firmware text is never injected into JSON.
        if (actions_ret == 0) std::cout << "ACTION_LIST_BEGIN\n" << actions << "\nACTION_LIST_END\n";
        return id_ret == 0 && mode_ret == 0 && (!include_actions || actions_ret == 0) ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "G1 read-only query failed: " << error.what() << '\n';
        return 1;
    }
}
