// Analytic gravity only. Coordinates: meters, radians, kg, N m; quaternion wxyz.
#pragma once
#include <array>
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace gravity {
using V=std::array<double,3>;
using M=std::array<V,3>;
using Torques=std::array<double,14>;
struct Joint{V xyz,rpy,axis;double mass;V com;};
struct Load{double mass;V com;};
inline V add(V a,V b){for(int i=0;i<3;++i)a[i]+=b[i];return a;}
inline V sub(V a,V b){for(int i=0;i<3;++i)a[i]-=b[i];return a;}
inline V scale(V a,double s){for(double& x:a)x*=s;return a;}
inline double dot(V a,V b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];}
inline V cross(V a,V b){return {a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]};}
inline M identity(){return {{{1,0,0},{0,1,0},{0,0,1}}};}
inline V mul(const M& a,V b){return {dot(a[0],b),dot(a[1],b),dot(a[2],b)};}
inline V transpose_mul(const M& a,V b){V c{};for(int i=0;i<3;++i)for(int j=0;j<3;++j)c[i]+=a[j][i]*b[j];return c;}
inline M mul(const M& a,const M& b){M c{};for(int i=0;i<3;++i)for(int j=0;j<3;++j)for(int k=0;k<3;++k)c[i][j]+=a[i][k]*b[k][j];return c;}
inline M rotation(V axis,double angle){
 double c=std::cos(angle),s=std::sin(angle);M out{};
 for(int i=0;i<3;++i)for(int j=0;j<3;++j)out[i][j]=(i==j?c:0)+(1-c)*axis[i]*axis[j];
 out[0][1]-=s*axis[2];out[0][2]+=s*axis[1];out[1][0]+=s*axis[2];
 out[1][2]-=s*axis[0];out[2][0]-=s*axis[1];out[2][1]+=s*axis[0];return out;
}
inline M rpy(V angles){return mul(mul(rotation({0,0,1},angles[2]),rotation({0,1,0},angles[1])),rotation({1,0,0},angles[0]));}
inline M quaternion(std::array<double,4> q){
 double n=0;for(double x:q){if(!std::isfinite(x))throw std::runtime_error("gravity_invalid_imu");n+=x*x;}
 if(n<.81||n>1.21)throw std::runtime_error("gravity_invalid_imu");
 for(double& x:q)x/=std::sqrt(n);
 double w=q[0],x=q[1],y=q[2],z=q[3];
 return {{{1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)},
          {2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)},
          {2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)}}};
}
inline std::array<double,7> arm(const std::array<Joint,7>& chain,Load load,const std::array<double,7>& q,V g){
 std::array<V,7> axes{},positions{};std::array<double,7> tau{};M R=identity();V p{};
 auto mass_at=[&](double mass,V com,int count){V force=scale(g,mass);for(int i=0;i<count;++i)tau[i]-=dot(axes[i],cross(sub(com,positions[i]),force));};
 for(int i=0;i<7;++i){
  if(!std::isfinite(q[i]))throw std::runtime_error("gravity_invalid_joint");
  const auto& j=chain[i];p=add(p,mul(R,j.xyz));R=mul(R,rpy(j.rpy));positions[i]=p;axes[i]=mul(R,j.axis);
  R=mul(R,rotation(j.axis,q[i]));mass_at(j.mass,add(p,mul(R,j.com)),i+1);
 }
 mass_at(load.mass,add(p,mul(R,load.com)),7);return tau;
}
}
#include "gravity_model.generated.hpp"
namespace gravity {
inline Torques calculate(const std::array<double,17>& upper,std::array<double,4> imu){
 M torso=quaternion(imu);
 for(int i=0;i<3;++i){if(!std::isfinite(upper[i]))throw std::runtime_error("gravity_invalid_joint");torso=mul(mul(torso,rpy(WAIST[i].rpy)),rotation(WAIST[i].axis,upper[i]));}
 V g=transpose_mul(torso,{0,0,-9.80665});Torques out{};
 for(int side=0;side<2;++side){std::array<double,7> q{};for(int i=0;i<7;++i)q[i]=upper[3+side*7+i];
  auto t=arm(side?RIGHT:LEFT,LOADS[side],q,g);for(int i=0;i<7;++i){if(!std::isfinite(t[i]))throw std::runtime_error("gravity_nonfinite_torque");out[side*7+i]=t[i];}}
 return out;
}
inline Torques limit(Torques raw){for(int i=0;i<14;++i){if(!std::isfinite(raw[i]))throw std::runtime_error("gravity_nonfinite_torque");raw[i]=std::clamp(raw[i],-FF_LIMIT[i],FF_LIMIT[i]);}return raw;}
inline bool exceeds(const Torques& raw){for(int i=0;i<14;++i)if(!std::isfinite(raw[i])||std::abs(raw[i])>FF_LIMIT[i]+1e-9)return true;return false;}
inline Torques handback(Torques start,double start_weight,double weight){
 if(weight<=0||start_weight<=0){start.fill(0);return start;}
 double factor=std::clamp(weight/start_weight,0.,1.);for(double& x:start)x*=factor;return start;
}
struct Ramp{
 Torques value{};
 Torques update(Torques raw,double elapsed,double dt,double weight){
  if(weight==0){value.fill(0);return value;}
  auto desired=limit(raw);double gain=std::clamp(elapsed/2.,0.,1.);dt=std::clamp(dt,0.,.05);
  for(int i=0;i<14;++i){double step=FF_SLEW[i%7]*dt;value[i]+=std::clamp(desired[i]*gain-value[i],-step,step);}
  return value;
 }
};
}
