#include <arpa/inet.h>
#include <sys/socket.h>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
bool g1_livox_search(const void*,size_t,const sockaddr*,socklen_t,const sockaddr_in&,int);
extern "C" ssize_t __wrap_sendto(int,const void*,size_t,int,const sockaddr*,socklen_t);
sockaddr_in fake_local{}, seen_remote{};
int fake_type=SOCK_DGRAM, seen_flags=0;
const void* seen_buffer=nullptr;
size_t seen_length=0;
extern "C" int getsockname(int,sockaddr* address,socklen_t* length) noexcept {
  std::memcpy(address,&fake_local,sizeof(fake_local)); *length=sizeof(fake_local); return 0;
}
extern "C" int getsockopt(int,int,int,void* value,socklen_t* length) noexcept {
  std::memcpy(value,&fake_type,sizeof(fake_type)); *length=sizeof(fake_type); return 0;
}
extern "C" ssize_t __real_sendto(int,const void* buffer,size_t length,int flags,const sockaddr* address,socklen_t) {
  std::memcpy(&seen_remote,address,sizeof(seen_remote));
  seen_buffer=buffer; seen_length=length; seen_flags=flags; return 42;
}
int main() {
  sockaddr_in local{}, remote{};
  local.sin_family=remote.sin_family=AF_INET;
  local.sin_addr.s_addr=inet_addr("192.0.2.6"); local.sin_port=htons(56000);
  remote.sin_addr.s_addr=INADDR_BROADCAST; remote.sin_port=htons(56000);
  uint8_t packet[25]{}; packet[0]=0xaa; packet[2]=24;
  auto matches=[&](size_t size=24, int type=SOCK_DGRAM) {
    return g1_livox_search(packet,size,reinterpret_cast<sockaddr*>(&remote),sizeof(remote),local,type);
  };
  assert(matches());
  assert(!matches(23)); assert(!matches(25)); assert(!matches(24,SOCK_STREAM));
  for (int i: {0,1,2,3,8,9,10,11,12,13,14,15,16,17}) {
    auto old=packet[i]; packet[i]^=1; assert(!matches()); packet[i]=old;
  }
  for (int i: {4,5,6,7,18,19,20,21,22,23}) {
    packet[i]=1; assert(matches()); packet[i]=0;
  }
  local.sin_addr.s_addr=inet_addr("192.0.2.5"); assert(!matches());
  local.sin_addr.s_addr=inet_addr("192.0.2.6");
  local.sin_port=htons(56101); assert(!matches()); local.sin_port=htons(56000);
  remote.sin_port=htons(56100); assert(!matches()); remote.sin_port=htons(56000);
  remote.sin_addr.s_addr=inet_addr("192.0.2.14"); assert(!matches());
  assert(!g1_livox_search(nullptr,24,reinterpret_cast<sockaddr*>(&remote),sizeof(remote),local,SOCK_DGRAM));
  remote.sin_addr.s_addr=INADDR_BROADCAST;
  fake_local=local;
  auto send=[&]() {return __wrap_sendto(77,packet,24,MSG_DONTWAIT,reinterpret_cast<sockaddr*>(&remote),sizeof(remote));};
  assert(send()==42);
  assert(seen_remote.sin_addr.s_addr==inet_addr("192.0.2.14"));
  assert(seen_buffer==packet); assert(seen_length==24); assert(seen_flags==MSG_DONTWAIT);
  packet[8]=1; send(); assert(seen_remote.sin_addr.s_addr==INADDR_BROADCAST); packet[8]=0;
  fake_local.sin_addr.s_addr=inet_addr("192.0.2.5"); send(); assert(seen_remote.sin_addr.s_addr==INADDR_BROADCAST);
  fake_local=local; fake_type=SOCK_STREAM; send(); assert(seen_remote.sin_addr.s_addr==INADDR_BROADCAST);
  std::cout << "livox_search_guard_assertions=41 packet_sends=0 motion_calls=0\n";
}
