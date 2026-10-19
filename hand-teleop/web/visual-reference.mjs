import {rawToDegrees} from './mapping.mjs';

// Display-only anchors: left-hand photographs and the user's confirmation
// that the right hand has the same appearance. These are not motion calibration.
export const visualReference = {
  L: {raw:[4,4,43,34,0,993]},
  R: {raw:[0,6,0,0,15,987]},
};

export function displayAngleScale(mapping, side, index) {
  // Thumb flexion is toward +Z; opposition swings its base in the palm plane.
  // This affects rendering and mouse gestures, never register commands.
  return !mapping.hands[side].verified && (index===4||index===5) ? -1 : 1;
}

export function displayedPose(mapping, side, raw) {
  const hand=mapping.hands[side];
  const angles=hand.channels.map((channel,index)=>rawToDegrees(channel,raw[index]));
  const reference=hand.verified?null:visualReference[side];
  if(reference) {
    // Move the drawing's origin to the photographed thumb posture.
    // Its flexion sign is a display correction, not a hardware calibration.
    for(const index of [4,5])angles[index]-=rawToDegrees(hand.channels[index],reference.raw[index]);
    angles[4]*=displayAngleScale(mapping,side,4);
    angles[5]*=displayAngleScale(mapping,side,5);
  }
  return {
    angles,
    thumbBase:reference?[-0.63,-0.35,0]:mapping.model.thumb_base,
    thumbRestYaw:reference?0:mapping.model.thumb_rest_yaw_degrees,
    thumbRotationAxis:reference?[0,0,1]:mapping.model.thumb_rotation_axis,
    referenceApplied:!!reference,
  };
}
