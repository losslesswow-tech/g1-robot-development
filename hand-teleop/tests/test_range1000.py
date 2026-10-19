"""Full raw range is explicit, bilateral and never silently clipped."""
import json
from pathlib import Path
import tempfile
import unittest

from test_all_channels import BothFakeDriver
from test_right_trial import ROOT, step


class Range1000Tests(unittest.TestCase):
    def setUp(self):
        self.plans = [step.load_plan(ROOT/name) for name in
                      ('manual-range1000-right.json','manual-range1000-left.json')]
        # Ideal endpoint feedback in this fake; offset holding is covered separately.
        # Extending an observed +42 offset linearly would fabricate feedback >1000.
        for plan in self.plans:
            plan['reference_actual'] = plan['hold_targets'][:]
        self.driver = BothFakeDriver(self.plans)
        self.bench = step.StepWorkbench(step.load_mapping(ROOT/'calibration.example.json'),
                        self.driver,self.plans[0],extra_plans=self.plans[1:])

    def arm(self, side):
        prepared = self.bench.arm(side, 'range1000-offline')
        return dict(client='range1000-offline',lease=prepared['lease'],side=side,
                    seq=1,values=prepared['target'])

    def test_both_hands_accept_exact1000_and_return_to_zero_without_hidden500_cap(self):
        for side in ('L','R'):
            self.setUp()
            other = 'L' if side=='R' else 'R'
            held = self.driver.targets[other][:]
            payload = self.arm(side)
            self.assertEqual(self.driver.writes,[])
            payload['values'] = [1000]*5+[950]
            self.bench.pose(payload)
            payload = self.arm(side)
            self.assertEqual(payload['values'],[1000]*5+[950])
            payload['values'] = [0]*5+[1000]
            self.bench.pose(payload)
            self.assertEqual(self.driver.writes,[(side,[1000]*5+[950]),(side,[0]*5+[1000])])
            self.assertEqual(self.driver.targets[other],held)

    def test_all_first_five_boundaries_and_rotation_remain_enforced(self):
        for side in ('L','R'):
            self.setUp()
            payload = self.arm(side)
            for index in range(6):
                for value in ((-1,1001) if index<5 else (949,1001)):
                    target = payload['values'][:];target[index]=value
                    with self.assertRaises(ValueError): self.bench.pose(dict(payload,values=target))
            self.assertEqual(self.driver.writes,[])

    def test_plan_cannot_expand_rotation_or_retain_hidden500_step(self):
        for field,index,value in [('min_raw',5,0),('max_step',0,500),('max_raw',0,1001)]:
            plan = json.loads(json.dumps(self.plans[0]));plan[field][index]=value
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'plan.json';path.write_text(json.dumps(plan))
                with self.assertRaises(ValueError): step.load_plan(path)

    def test_config_publishes1000_for_both_sides_without_marking_calibrated(self):
        config = self.bench.config()
        for side in ('L','R'):
            self.assertEqual(config['single_step_scopes'][side]['max_raw'],[1000]*6)
            self.assertEqual(config['single_step_scopes'][side]['min_raw'],[0]*5+[950])
            self.assertFalse(self.bench.mapping['hands'][side]['verified'])
        self.assertEqual(self.driver.writes,[])

    def test_old_schema5_still_rejects_targets_above500(self):
        old = step.load_plan(ROOT/'manual-all-right.json')
        driver = step.RightTrialDriver(BothFakeDriver([old]),old)
        target = old['hold_targets'][:];target[0]=1000
        with self.assertRaises(ValueError): driver.validate('R',target)


if __name__ == '__main__': unittest.main()
