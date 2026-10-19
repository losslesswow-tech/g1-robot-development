"""Independent full-URDF potential-energy reference, for offline validation only."""
import math
import xml.etree.ElementTree as ET
from generate_gravity_model import SOURCE,PARTS,origin,rpy,add,mv,mm

def axis_rotation(a,t):
    x,y,z=a;c=math.cos(t);s=math.sin(t);v=1-c
    return [[c+x*x*v,x*y*v-z*s,x*z*v+y*s],[y*x*v+z*s,c+y*y*v,y*z*v-x*s],
            [z*x*v-y*s,z*y*v+x*s,c+z*z*v]]

class EnergyReference:
    def __init__(self):
        robot=ET.parse(SOURCE).getroot();self.links={e.get('name'):e for e in robot.findall('link')};self.children={}
        for j in robot.findall('joint'):self.children.setdefault(j.find('parent').get('link'),[]).append(j)
        self.names=['waist_'+p+'_joint' for p in ('yaw','roll','pitch')]+[s+'_'+p+'_joint' for s in ('left','right') for p in PARTS]
        self.joints={j.get('name'):j for j in robot.findall('joint')}

    def potential(self,q,quat,finger_positions=None):
        w,x,y,z=quat;n=math.sqrt(sum(v*v for v in quat));w,x,y,z=[v/n for v in quat]
        R=[[1-2*y*y-2*z*z,2*x*y-2*z*w,2*x*z+2*y*w],
           [2*x*y+2*z*w,1-2*x*x-2*z*z,2*y*z-2*x*w],
           [2*x*z-2*y*w,2*y*z+2*x*w,1-2*x*x-2*y*y]]
        positions=dict(zip(self.names,q));positions.update(finger_positions or {});p=[0.]*3
        def transform(j,R,p):
            xyz,angles=origin(j);position=add(p,mv(R,xyz));rotation=mm(R,rpy(angles))
            if j.get('type')!='fixed':
                axis=[float(v) for v in j.find('axis').get('xyz').split()]
                angle=positions.get(j.get('name'),0.)
                mimic=j.find('mimic')
                if mimic is not None:angle=positions.get(mimic.get('joint'),0.)*float(mimic.get('multiplier','1'))+float(mimic.get('offset','0'))
                rotation=mm(rotation,axis_rotation(axis,angle))
            return rotation,position
        for name in self.names[:3]:R,p=transform(self.joints[name],R,p)
        total=0.
        def walk(j,R,p):
            nonlocal total
            R,p=transform(j,R,p);name=j.find('child').get('link');inertial=self.links[name].find('inertial')
            if inertial is not None:total+=float(inertial.find('mass').get('value'))*9.80665*add(p,mv(R,origin(inertial)[0]))[2]
            for child in self.children.get(name,[]):walk(child,R,p)
        for side in ('left','right'):walk(self.joints[side+'_shoulder_pitch_joint'],R,p)
        return total

    def torques(self,q,quat,finger_positions=None):
        result=[];h=1e-6
        for i in range(3,17):
            plus=list(q);minus=list(q);plus[i]+=h;minus[i]-=h
            result.append((self.potential(plus,quat,finger_positions)-self.potential(minus,quat,finger_positions))/(2*h))
        return result
