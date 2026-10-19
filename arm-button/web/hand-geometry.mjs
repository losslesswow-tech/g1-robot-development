export const hand = {
  "name": "Inspire RH56DFTP (Unitree official FTP integration)",
  "physical_model_confirmed_by_user": true,
  "official_repository_commit": "5994d4faef0a9cadd3287f8de0199a67eeb2a259",
  "official_urdf_sha256": "63097d7366e1903b595c6adbdb187b205f5c1b1189088ca5aea32d25622415cb",
  "joints": [
    {
      "name": "right_base_joint",
      "type": "fixed",
      "parent": "right_wrist_yaw_link",
      "child": "right_base_link",
      "xyz": [
        0.0415,
        0.0,
        0.0
      ],
      "rpy": [
        0.0,
        1.5707963267,
        0.0
      ]
    },
    {
      "name": "right_index_1_joint",
      "type": "revolute",
      "parent": "right_base_link",
      "child": "right_index_1",
      "xyz": [
        -0.038679,
        0.00056467,
        0.1564
      ],
      "rpy": [
        1.6057,
        0.0,
        -1.5708
      ],
      "axis": [
        0.0,
        0.0,
        1.0
      ],
      "lower": 0.0,
      "upper": 1.4381
    },
    {
      "name": "right_index_2_joint",
      "type": "revolute",
      "parent": "right_index_1",
      "child": "right_index_2",
      "xyz": [
        -0.0034259,
        0.032596,
        -0.00055
      ],
      "rpy": [
        0.0,
        0.0,
        0.0
      ],
      "axis": [
        0.0,
        0.0,
        1.0
      ],
      "lower": 0.0,
      "upper": 3.14,
      "mimic": {
        "joint": "right_index_1_joint",
        "multiplier": 1.0843,
        "offset": 0.0
      }
    }
  ],
  "tip": [
    -0.0058306391001679,
    0.05348015980174144,
    -0.006203434080816805
  ],
  "tip_definition": "Unique-vertex centroid within 0.2 mm of maximum local Y in official right_index_2.STL; geometric proxy, not calibrated contact TCP",
  "tip_calibrated": false,
  "meshes": {
    "right_base_link": {
      "min_m": [
        -0.04466964676976204,
        -0.03501144051551819,
        8.940696794024205e-11
      ],
      "max_m": [
        0.044647783041000366,
        0.030138932168483734,
        0.1609961837530136
      ],
      "tip_proxy_m": [
        -0.02634771727025509,
        0.03013082096974055,
        0.06036972006162008
      ],
      "triangles": 90344,
      "sha256": "77930c4a5df7536f95883e3f50b3fc21a12cb34bfc0b03b71ab859d166595b70"
    },
    "right_index_1": {
      "min_m": [
        -0.014050020836293697,
        -0.011496064253151417,
        -0.01586500182747841
      ],
      "max_m": [
        0.010269008576869965,
        0.03583249822258949,
        0.002565000904724002
      ],
      "tip_proxy_m": [
        -0.003469866770319641,
        0.03574308007955551,
        -0.006650414667092264
      ],
      "triangles": 15454,
      "sha256": "841fb085be3cb6241afd61e980c7d485979ea148d4d5fc7f3ef18cbc965849c7"
    },
    "right_index_2": {
      "min_m": [
        -0.01164789218455553,
        -0.008494295179843903,
        -0.014474269933998585
      ],
      "max_m": [
        0.011443568393588066,
        0.05359547585248947,
        0.0023746604565531015
      ],
      "tip_proxy_m": [
        -0.0058306391001679,
        0.05348015980174144,
        -0.006203434080816805
      ],
      "triangles": 7448,
      "sha256": "6fe0e0a17722a4f94da64f2968dca657968749cbc4e3e4efd923004b1fe220c4"
    }
  }
};
