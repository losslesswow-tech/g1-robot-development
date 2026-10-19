"""Fail-closed profile and deployment package checks; no SSH or DDS."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('g1_environment', ROOT / 'environment.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class EnvironmentTests(unittest.TestCase):
    def reject(self, field, value):
        data = json.loads(adapter.DEFAULT_PROFILE.read_text())
        data[field] = value
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                adapter.load_profile(path)

    def test_refuses_motion_enabled_profile(self):
        self.reject('motion_paused', False)

    def test_refuses_wrong_model_or_domain(self):
        self.reject('robot_model', 'Unitree B2')
        self.reject('dds_domain', 1)

    def test_refuses_ros_address_mismatch(self):
        self.reject('ros_master_uri', 'http://192.0.2.6:11311')

    def test_refuses_shell_tokens_and_parent_traversal(self):
        self.reject('dds_interface', 'eno1; reboot')
        self.reject('host_development_root', '/home/operator/../other')

    def test_package_is_reproducible_and_excludes_private_files(self):
        _, entries, release = adapter.package()
        _, repeated, repeated_release = adapter.package()
        self.assertEqual(entries, repeated)
        self.assertEqual(release, repeated_release)
        self.assertIn('control/g1_right_arm_bounded.cpp', entries)
        self.assertIn('environments/backpack-20261013.json', entries)
        self.assertFalse(any('token' in x or 'id_ed25519' in x or '__pycache__' in x for x in entries))
        compile(adapter.REMOTE, '<remote adapter>', 'exec')


if __name__ == '__main__':
    unittest.main()
