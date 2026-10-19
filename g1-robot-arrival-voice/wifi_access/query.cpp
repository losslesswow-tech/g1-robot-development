// Read-only DDS probe. Never constructs or calls any motion operation.
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/g1/loco/g1_loco_client.hpp>
#include <unitree/dds_wrapper/robots/g1/g1_sub.h>
#include <atomic>
#include <chrono>
#include <iostream>
#include <regex>
#include <thread>

std::atomic<unsigned long> samples{0};
void low_state(const void*) { ++samples; }

int main(int argc, char** argv) {
  try {
    const bool loopback = argc == 2 && std::string(argv[1]) == "--loopback";
    const bool parse_only = argc == 2 && std::string(argv[1]) == "--reject-invalid";
    const bool wired = argc == 2 && std::string(argv[1]) == "--wired";
    const bool explicit_peers = argc >= 3 && argc <= 66 && std::string(argv[1]) == "--peers";
    if (!(loopback || parse_only || wired || explicit_peers || (argc == 2 && std::string(argv[1]) == "--query"))) {
      std::cerr << "Usage: g1_wifi_readonly --reject-invalid|--loopback|--query|--wired|--peers IP:PORT ...\n";
      return 2;
    }
    const std::string address = loopback || parse_only ? "127.0.0.1" : "192.0.2.6";
    std::string peers = loopback || parse_only ? "" :
      "<Peers><Peer Address=\"192.0.2.10\"/><Peer Address=\"192.0.2.8\"/></Peers>";
    if (explicit_peers) {
      peers = "<Peers>";
      for (int i = 2; i < argc; ++i) {
        const std::string value(argv[i]);
        if (!std::regex_match(value, std::regex("192\\.168\\.123\\.(161|164):[0-9]{1,5}")))
          throw std::invalid_argument("Peer must be internal G1 address with numeric port");
        const auto port = std::stoi(value.substr(value.find(':') + 1));
        if (port < 1 || port > 65535) throw std::invalid_argument("Invalid peer port");
        peers += "<Peer Address=\"" + value + "\"/>";
      }
      peers += "</Peers>";
    }
    const std::string xml = "<CycloneDDS><Domain Id=\"0\"><General><Interfaces>"
      "<NetworkInterface address=\"" + address + "\"/>"
      "</Interfaces><AllowMulticast>false</AllowMulticast><DontRoute>false</DontRoute>"
      "</General><Discovery><ParticipantIndex>auto</ParticipantIndex>"
      "<MaxAutoParticipantIndex>30</MaxAutoParticipantIndex>" + peers +
      "</Discovery></Domain></CycloneDDS>";
    unitree::common::JsonMap parameter;
    parameter["DomainId"] = uint32_t(0);
    parameter["Config"] = parse_only ? "<INVALID_DDS_CONFIG/>" : xml;
    if (parse_only) {
      try {
        unitree::robot::ChannelFactory::Instance()->Init(parameter);
      } catch (const std::exception&) {
        std::cout << "{\"invalid_config_rejected\":true,\"motion_calls\":0}" << std::endl;
        return 0;
      }
      std::cerr << "SDK ignored explicit DDS configuration" << std::endl;
      return 3;
    }
    if (wired) {
      unitree::robot::ChannelFactory::Instance()->Init(0, "enp132s0");
    } else {
      unitree::robot::ChannelFactory::Instance()->Init(parameter);
    }
    if (loopback) {
      std::cout << "{\"loopback_init\":true,\"motion_calls\":0}" << std::endl;
      return 0;
    }
    unitree::robot::ChannelSubscriber<unitree_hg::msg::dds_::LowState_> sub("rt/lowstate");
    sub.InitChannel(low_state, 1);
    unitree::robot::g1::LocoClient client;
    client.SetTimeout(3.0f);
    client.Init();
    std::this_thread::sleep_for(std::chrono::seconds(3));
    int fsm = -1, mode = -1;
    const auto fsm_code = client.GetFsmId(fsm);
    const auto mode_code = client.GetFsmMode(mode);
    std::this_thread::sleep_for(std::chrono::seconds(3));
    std::cout << "{\"fsm_code\":" << fsm_code << ",\"fsm_id\":" << fsm
      << ",\"mode_code\":" << mode_code << ",\"fsm_mode\":" << mode
      << ",\"lowstate_samples\":" << samples.load()
      << ",\"motion_calls\":0}" << std::endl;
    return fsm_code == 0 && mode_code == 0 && samples > 0 ? 0 : 1;
  } catch (const std::exception& error) {
    std::cerr << error.what() << std::endl;
    return 2;
  }
}
