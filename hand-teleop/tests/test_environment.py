"""Validate the new host boundary without SSH, credentials or hardware."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('hand_environment',ROOT/'environment.py')
env=importlib.util.module_from_spec(spec);spec.loader.exec_module(env)


class HandEnvironmentTests(unittest.TestCase):
    def test_default_profile_is_backpack_with_existing_raw_limits(self):
        p=env.load_profile(env.DEFAULT_PROFILE)
        self.assertEqual(p['management_host'],'192.0.2.1')
        self.assertEqual(p['navigation_container'],'robot-navigation-example')
        self.assertEqual(p['min_raw'],[0]*5+[950])
        self.assertEqual(p['max_raw'],[1000]*6)

    def test_invalid_profile_cannot_redirect_hand_endpoints_or_expand_rotation(self):
        for field,value in [('hands',{'L':'192.0.2.5','R':'192.0.2.4'}),
                            ('min_raw',[0]*6),('navigation_container','x;bad'),
                            ('host_development_root','/home/../etc')]:
            p=env.load_profile(env.DEFAULT_PROFILE);p[field]=value
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'profile.json';path.write_text(json.dumps(p))
                with self.assertRaises(ValueError):env.load_profile(path)

    def test_ssh_preserves_host_key_checks_and_specific_identity(self):
        cmd=env.ssh_command(env.load_profile(env.DEFAULT_PROFILE),Path('private-key'))
        self.assertIn('StrictHostKeyChecking=yes',cmd)
        self.assertIn('IdentitiesOnly=yes',cmd)
        self.assertIn('operator@192.0.2.1',cmd)

    def test_adapter_package_contains_no_credentials_or_active_control_plan(self):
        files=env.adapter_files(env.load_profile(env.DEFAULT_PROFILE))
        self.assertEqual(set(files),{'environment.py','ENVIRONMENT.md','environments/backpack-20261013.json'})
        self.assertNotIn('connection.token',files)

    def test_remote_program_compiles_and_exposes_no_motion_route(self):
        compile(env.REMOTE,'remote-check','exec')
        for route in ('/api/arm','/api/pose','set_angles(','write_regs('):
            self.assertNotIn(route,env.REMOTE)


if __name__=='__main__':unittest.main()
