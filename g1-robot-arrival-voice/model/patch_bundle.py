#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Patch the minified frontend bundle (index-WlVTOc2a.js) to replace the
drawn ArrowHelper robot with a G1 URDF STL mesh.

Two edits:
  1. `vt = new dl(...)`  (ArrowHelper)  ->  `vt = new Ge`  (empty Group)
  2. inject an async loader right after P.add(...) that fetches
     /assets/g1_model.stl, parses binary STL, and adds the Mesh to vt.

Usage:  patch_bundle.py <path/to/index-WlVTOc2a.js>
"""
import sys

OLD_ARROW = "const vt=new dl(new L(1,0,0),new L(0,0,0),1.1,16763955,.35,.2),"
NEW_GROUP = "const vt=new Ge,"

PADD_ANCHOR = "P.add(A,B,O,V,K,tt,at,ct,j,vt,Nt,k,ot,gt,Ot);"

LOADER = (
    ";!function(){fetch(\"/assets/g1_model.stl\").then(function(t){return t.arrayBuffer()})"
    ".then(function(t){var d=new DataView(t),n=d.getUint32(80,!0),arr=new Float32Array(n*9),o=0;"
    "for(var i=0;i<n;i++){var b=84+i*50;for(var k=0;k<9;k++)arr[o++]=d.getFloat32(b+12+k*4,!0)}"
    "var g=new Ce;g.setAttribute(\"position\",new Re(arr,3));"
    "var mat=new Wi({color:new Vt(12632256),side:2});"
    "vt.add(new ve(g,mat))}).catch(function(){})}();"
)


def main():
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        s = f.read()

    assert s.count(OLD_ARROW) == 1, "arrow anchor count=%d" % s.count(OLD_ARROW)
    assert s.count(PADD_ANCHOR) == 1, "P.add anchor count=%d" % s.count(PADD_ANCHOR)

    s = s.replace(OLD_ARROW, NEW_GROUP)
    s = s.replace(PADD_ANCHOR, PADD_ANCHOR + LOADER)

    with open(path, "w", encoding="utf-8") as f:
        f.write(s)

    print("patched OK:", path)
    print("  arrow -> group:", NEW_GROUP)
    print("  loader injected after P.add(...)")


if __name__ == "__main__":
    main()
