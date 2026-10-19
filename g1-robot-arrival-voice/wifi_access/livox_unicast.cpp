// Linked only into an isolated copy of the installed Livox driver.
// Preserve the SDK payload and source socket; change only a zero-payload
// SDK2 Search destination. No network configuration or motion API here.
#include <arpa/inet.h>
#include <sys/socket.h>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstdint>

extern "C" ssize_t __real_sendto(int, const void*, size_t, int,
                                const struct sockaddr*, socklen_t);

bool g1_livox_search(const void* buffer, size_t length,
                     const sockaddr* destination, socklen_t destination_length,
                     const sockaddr_in& local, int socket_type) {
  if (!buffer || !destination || length != 24 || socket_type != SOCK_DGRAM ||
      destination_length != sizeof(sockaddr_in) ||
      destination->sa_family != AF_INET || local.sin_family != AF_INET ||
      local.sin_addr.s_addr != inet_addr("192.0.2.6") ||
      local.sin_port != htons(56000)) return false;
  sockaddr_in remote{};
  std::memcpy(&remote, destination, sizeof(remote));
  if (remote.sin_port != htons(56000) ||
      remote.sin_addr.s_addr != INADDR_BROADCAST) return false;
  const auto* data = static_cast<const uint8_t*>(buffer);
  // SDK2 preamble: SOF, version, LE length, sequence, command, type, sender.
  if (data[0] != 0xaa || data[1] != 0 || data[2] != 24 || data[3] != 0 ||
      data[8] != 0 || data[9] != 0 || data[10] != 0 || data[11] != 0)
    return false;
  for (size_t i = 12; i < 18; ++i) if (data[i] != 0) return false;
  return true;
}

extern "C" ssize_t __wrap_sendto(int fd, const void* buffer, size_t length,
                                 int flags, const sockaddr* destination,
                                 socklen_t destination_length) {
  sockaddr_in local{};
  socklen_t local_length = sizeof(local);
  int socket_type = 0;
  socklen_t type_length = sizeof(socket_type);
  if (getsockname(fd, reinterpret_cast<sockaddr*>(&local), &local_length) == 0 &&
      local_length == sizeof(local) &&
      getsockopt(fd, SOL_SOCKET, SO_TYPE, &socket_type, &type_length) == 0 &&
      g1_livox_search(buffer, length, destination, destination_length, local, socket_type)) {
    sockaddr_in target{};
    std::memcpy(&target, destination, sizeof(target));
    target.sin_addr.s_addr = inet_addr("192.0.2.14");
    static std::atomic<bool> announced{false};
    if (!announced.exchange(true))
      std::fputs("G1_WIFI_LIVOX: SDK2 Search destination 192.0.2.14:56000; payload/source unchanged\n", stderr);
    return __real_sendto(fd, buffer, length, flags,
                         reinterpret_cast<const sockaddr*>(&target), sizeof(target));
  }
  return __real_sendto(fd, buffer, length, flags, destination, destination_length);
}
