"""Independent six-channel scopes, shared ownership and exact hardware targets."""
import unittest
from test_right_trial import ROOT, step


class BothFakeDriver:
    def __init__(self, plans):
        self.targets = {p['side']: p['hold_targets'][:] for p in plans}
        self.actual = {p['side']: p['reference_actual'][:] for p in plans}
        self.writes = []

    def read_targets(self, side): return self.targets[side][:]
    def read(self, side): return {'actual': self.actual[side][:], 'force_raw': [0]*6}
    def move(self, side, values):
        self.writes.append((side, values[:]))
        self.actual[side] = [a+v-t for a,v,t in zip(self.actual[side],values,self.targets[side])]
        self.targets[side] = values[:]
        return self.actual[side][:]


class AllChannelTests(unittest.TestCase):
    def setUp(self):
        self.plans = [step.load_plan(ROOT/name) for name in ('manual-all-right.json','manual-all-left.json')]
        self.driver = BothFakeDriver(self.plans)
        self.bench = step.StepWorkbench(step.load_mapping(ROOT/'calibration.example.json'),
                        self.driver,self.plans[0],extra_plans=self.plans[1:])

    def arm(self, side):
        session = self.bench.arm(side, 'all-channels-offline')
        return dict(client='all-channels-offline',lease=session['lease'],side=side,seq=1,
                    values=session['target'])

    def test_each_hand_can_send_all_six_together_without_touching_other_hand(self):
        for side in ('L','R'):
            with self.subTest(side=side):
                self.setUp()
                other = 'R' if side=='L' else 'L'
                preserved = self.driver.targets[other][:]
                payload = self.arm(side)
                payload['values'] = [100,200,300,400,100,950]
                result = self.bench.pose(payload)
                self.assertEqual(self.driver.writes,[(side,payload['values'])])
                self.assertEqual(self.driver.targets[other],preserved)
                self.assertFalse(result['session_active'])
                self.assertFalse(result['reached'])

    def test_prepare_each_side_reads_its_own_targets_and_never_moves(self):
        for side in ('L','R'):
            payload = self.arm(side)
            self.assertEqual(payload['values'],self.driver.targets[side])
            self.bench.disarm(payload)
        self.assertEqual(self.driver.writes,[])
        self.assertTrue(all(not h['verified'] for h in self.bench.mapping['hands'].values()))

    def test_cannot_claim_other_hand_or_change_side_during_a_session(self):
        payload = self.arm('R')
        with self.assertRaises(ValueError): self.arm('L')
        payload.update(side='L',values=[100,200,300,400,100,950])
        with self.assertRaises(ValueError): self.bench.pose(payload)
        self.assertEqual(self.driver.writes,[])

    def test_each_side_rejects_501_and_rotation949_without_clipping(self):
        for side in ('L','R'):
            self.setUp()
            payload = self.arm(side)
            for index,value in ((0,501),(4,501),(5,949)):
                values = payload['values'][:];values[index]=value
                with self.assertRaises(ValueError): self.bench.pose(dict(payload,values=values))
            self.assertEqual(self.driver.writes,[])

    def test_command_targets_survive_offset_feedback_on_next_prepare(self):
        payload = self.arm('L');payload['values']=[100,200,300,400,100,950]
        self.bench.pose(payload)
        self.assertEqual(self.arm('L')['values'],payload['values'])
        self.assertEqual(self.driver.targets['R'],[500,500,500,500,0,1000])

    def test_target_change_on_one_side_does_not_corrupt_other_scope(self):
        self.driver.targets['L'][0] = 3
        with self.assertRaises(ValueError): self.arm('L')
        payload = self.arm('R')
        self.assertEqual(payload['values'],self.driver.targets['R'])
        self.assertEqual(self.driver.writes,[])


if __name__ == '__main__': unittest.main()
