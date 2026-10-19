// Passive G1 telemetry only. No LowCmd, publishers, clients, or actuator calls.
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

using Clock = std::chrono::steady_clock;
using State = unitree_hg::msg::dds_::LowState_;
namespace {
std::mutex guard;
State latest;
Clock::time_point received_at{};
std::uint64_t samples = 0, tick_changes = 0, rejected = 0;
std::array<double,14> peak_speed{}, min_q{}, max_q{};
std::array<std::uint64_t,14> nonzero_flags{};
std::array<double,12> leg_peak_speed{},leg_min_q{},leg_max_q{};
std::array<double,3> peak_gyro{},peak_rpy{};
const std::array<const char*, 14> names = {
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
    "left_elbow", "left_wrist_roll", "left_wrist_pitch", "left_wrist_yaw",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
    "right_elbow", "right_wrist_roll", "right_wrist_pitch", "right_wrist_yaw"};

// Same word-wise CRC convention as the installed SDK's HG LowState examples.
std::uint32_t crc32(const State& state) {
    static_assert(sizeof(State) % 4 == 0, "LowState layout must contain words");
    std::uint32_t crc = 0xffffffff;
    const auto* bytes = reinterpret_cast<const unsigned char*>(&state);
    for (std::size_t i = 0; i < sizeof(State) / 4 - 1; ++i) {
        std::uint32_t word;
        std::memcpy(&word, bytes + i * 4, 4);
        for (std::uint32_t mask = 0x80000000; mask; mask >>= 1) {
            const bool high = (crc & 0x80000000) != 0;
            crc <<= 1;
            if (high) crc ^= 0x04c11db7;
            if (word & mask) crc ^= 0x04c11db7;
        }
    }
    return crc;
}

void on_state(const void* message) {
    if (!message) return;
    const auto& state = *static_cast<const State*>(message);
    bool valid = crc32(state) == state.crc();
    for (int index = 15; index <= 28; ++index) {
        const auto& motor = state.motor_state().at(index);
        valid = valid && std::isfinite(motor.q()) && std::isfinite(motor.dq())
                      && std::isfinite(motor.tau_est());
    }
    for(int i=0;i<12;++i)valid=valid&&std::isfinite(state.motor_state()[i].q())&&std::isfinite(state.motor_state()[i].dq());
    for(int i=0;i<3;++i)valid=valid&&std::isfinite(state.imu_state().rpy()[i])&&std::isfinite(state.imu_state().gyroscope()[i]);
    std::lock_guard<std::mutex> lock(guard);
    if (!valid) { ++rejected; return; }
    for(int i=0;i<12;++i){const auto& motor=state.motor_state()[i];
        leg_peak_speed[i]=std::max(leg_peak_speed[i],double(std::abs(motor.dq())));
        if(!samples)leg_min_q[i]=leg_max_q[i]=motor.q();
        else {leg_min_q[i]=std::min(leg_min_q[i],double(motor.q()));leg_max_q[i]=std::max(leg_max_q[i],double(motor.q()));}
    }
    for(int i=0;i<3;++i){peak_gyro[i]=std::max(peak_gyro[i],double(std::abs(state.imu_state().gyroscope()[i])));peak_rpy[i]=std::max(peak_rpy[i],double(std::abs(state.imu_state().rpy()[i])));}
    for(int i=0;i<14;++i){const auto& motor=state.motor_state()[i+15];
        peak_speed[i]=std::max(peak_speed[i],double(std::abs(motor.dq())));
        if(!samples)min_q[i]=max_q[i]=motor.q();
        else {min_q[i]=std::min(min_q[i],double(motor.q()));max_q[i]=std::max(max_q[i],double(motor.q()));}
        if(motor.motorstate()!=0)++nonzero_flags[i];
    }
    if (samples && state.tick() != latest.tick()) ++tick_changes;
    latest = state;
    received_at = Clock::now();
    ++samples;
}
} // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "Usage: g1_arm_state NETWORK_INTERFACE OBSERVE_SECONDS(1..10)\n";
        return 2;
    }
    try {
        std::size_t consumed = 0;
        const auto seconds = std::stod(argv[2], &consumed);
        if (consumed != std::string(argv[2]).size() || !std::isfinite(seconds)
            || seconds < 1 || seconds > 10) throw std::invalid_argument("duration");
        unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
        auto channel = unitree::robot::ChannelFactory::Instance()
            ->CreateRecvChannel<State>("rt/lowstate", on_state, 1);
        std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
        // Detach the native DDS listener while its SDK callback object is alive.
        // Close the reader before destroying the SDK listener/queue thread.
        auto reader = channel->GetReader()->GetNative();
        reader.listener(nullptr, ::dds::core::status::StatusMask::none());
        reader.close();
        reader = ::dds::core::null;
        channel.reset();
        unitree::robot::ChannelFactory::Instance()->Release();
        std::lock_guard<std::mutex> lock(guard);
        const double age = samples ? std::chrono::duration<double, std::milli>(
            Clock::now() - received_at).count() : -1;
        const bool fresh = samples >= 2 && tick_changes > 0 && age >= 0 && age <= 500;
        std::cout << std::setprecision(9)
                  << "{\"marker\":\"G1_ARM_STATE_READONLY\",\"motion_commands_sent\":0,"
                  << "\"topic\":\"rt/lowstate\",\"dds_domain\":0,"
                  << "\"samples\":" << samples << ",\"tick_changes\":" << tick_changes
                  << ",\"rejected_samples\":" << rejected << ",\"age_ms\":" << age
                  << ",\"fresh\":" << (fresh ? "true" : "false")
                  << ",\"fsm_id\":null,\"arm_ownership_confirmed\":false";
        if (samples) {
            std::cout << ",\"tick\":" << latest.tick()
                      << ",\"mode_machine_raw\":" << static_cast<int>(latest.mode_machine())
                      << ",\"mode_pr_raw\":" << static_cast<int>(latest.mode_pr())
                      << ",\"version_raw\":[" << latest.version()[0] << ','
                      << latest.version()[1] << "],\"joints\":[";
            for (int i = 0; i < 14; ++i) {
                const auto& motor = latest.motor_state().at(i + 15);
                if (i) std::cout << ',';
                std::cout << "{\"name\":\"" << names[i] << "\",\"index\":" << i + 15
                          << ",\"q_rad\":" << motor.q() << ",\"dq_rad_s\":" << motor.dq()
                          << ",\"tau_est_raw\":" << motor.tau_est()
                          << ",\"motorstate_raw\":" << motor.motorstate()
                          << ",\"peak_abs_dq_rad_s\":" << peak_speed[i]
                          << ",\"nonzero_motorstate_samples\":" << nonzero_flags[i]
                          << ",\"q_range_rad\":[" << min_q[i] << ',' << max_q[i] << "]}";
            }
            std::cout << ']';
            std::cout << ",\"body_observation\":{\"imu_rpy_rad\":[";
            for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<latest.imu_state().rpy()[i];}
            std::cout << "],\"imu_gyro_rad_s\":[";
            for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<latest.imu_state().gyroscope()[i];}
            std::cout << "],\"peak_abs_gyro_rad_s\":[";
            for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<peak_gyro[i];}
            std::cout << "],\"peak_abs_rpy_rad\":[";
            for(int i=0;i<3;++i){if(i)std::cout<<',';std::cout<<peak_rpy[i];}
            std::cout << "],\"legs\":[";
            for(int i=0;i<12;++i){if(i)std::cout<<',';std::cout<<"{\"index\":"<<i<<",\"peak_abs_dq_rad_s\":"<<leg_peak_speed[i]<<",\"q_range_rad\":["<<leg_min_q[i]<<','<<leg_max_q[i]<<"]}";}
            std::cout << "]}";
        }
        std::cout << "}" << std::endl;
        return fresh ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "G1 read-only observation failed: " << error.what() << '\n';
        return 1;
    }
}
