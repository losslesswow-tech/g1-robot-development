// Offline executable: never includes SDK headers or initializes DDS.
#include "gravity.hpp"
#include <iostream>
#include <iomanip>
#include <string>
int main(int argc,char** argv){
 if(argc==2&&std::string(argv[1])=="--probe"){
  std::array<double,17> q{};std::array<double,4> imu{};std::cout<<std::setprecision(17);
  while(std::cin>>imu[0]){for(int i=1;i<4;++i)std::cin>>imu[i];for(double& v:q)std::cin>>v;if(!std::cin)return 2;
   try{auto t=gravity::calculate(q,imu);for(int i=0;i<14;++i){if(i)std::cout<<' ';std::cout<<t[i];}std::cout<<'\n';}catch(const std::exception&){return 3;}}
  return 0;
 }
 bool ok=true;int checks=0;
 auto check=[&](bool value){ok=ok&&value;++checks;};
 std::array<gravity::Joint,7> chain{};for(auto& j:chain)j.axis={0,1,0};
 auto t=gravity::arm(chain,{1,{1,0,0}},std::array<double,7>{},{0,0,-9.80665});check(std::abs(t[0]+9.80665)<1e-10);
 auto mirrored=gravity::arm(chain,{1,{-1,0,0}},std::array<double,7>{},{0,0,-9.80665});check(std::abs(mirrored[0]-9.80665)<1e-10);
 std::array<double,17> q{};auto raw=gravity::calculate(q,{1,0,0,0});auto neg=gravity::calculate(q,{-1,0,0,0});
 for(int i=0;i<14;++i)check(std::abs(raw[i]-neg[i])<1e-12);
 for(auto imu:std::array<std::array<double,4>,2>{{{0,0,0,0},{2,0,0,0}}}){bool rejected=false;try{gravity::calculate(q,imu);}catch(const std::exception&){rejected=true;}check(rejected);}
 gravity::Ramp ramp;gravity::Torques huge{};huge.fill(1000);auto limited=gravity::limit(huge);check(gravity::exceeds(huge));
 for(int i=0;i<14;++i)check(limited[i]==gravity::FF_LIMIT[i]);
 auto zero=ramp.update(raw,10,1,0);for(double v:zero)check(v==0);
 auto first=ramp.update(huge,0,.02,1);for(double v:first)check(v==0);
 for(int n=1;n<=200;++n){auto before=ramp.value;auto after=ramp.update(huge,n*.02,.02,1);
  for(int i=0;i<14;++i){check(std::abs(after[i])<=gravity::FF_LIMIT[i]+1e-12);check(std::abs(after[i]-before[i])<=gravity::FF_SLEW[i%7]*.02+1e-12);}}
 auto emergency=ramp.update(raw,10,.02,0);for(double v:emergency)check(v==0);
 auto half=gravity::handback(limited,.8,.4),end=gravity::handback(limited,.8,0),urgent=gravity::handback(limited,0,0);
 for(int i=0;i<14;++i){check(half[i]==limited[i]*.5);check(end[i]==0);check(urgent[i]==0);}
 std::cout<<"{\"gravity_self_test\":"<<(ok?"true":"false")<<",\"checks\":"<<checks<<",\"dds_initialized\":false,\"sent\":0}"<<std::endl;return ok?0:1;
}
