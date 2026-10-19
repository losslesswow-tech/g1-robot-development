import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import network_probe as probe


class LeaseTest(unittest.TestCase):
    def test_setup_grace_then_heartbeat_expiry_and_hard_limit(self):
        lease=probe.LeaseWindow(10,180)
        self.assertTrue(lease.active(189,False))
        self.assertFalse(lease.active(191,False))
        lease=probe.LeaseWindow(10,180)
        self.assertTrue(lease.active(20,True))
        self.assertTrue(lease.active(21,False))
        self.assertTrue(lease.active(200,True))
        self.assertFalse(lease.active(201,False))
        self.assertFalse(lease.active(3610,True))

    def test_heartbeat_requires_private_regular_single_link_and_expected_uid(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'beat'; file.write_text(''); file.chmod(0o600)
            now=file.stat().st_mtime
            self.assertTrue(probe.heartbeat_alive(file,os.getuid(),now+5))
            self.assertFalse(probe.heartbeat_alive(file,os.getuid(),now+31))
            self.assertFalse(probe.heartbeat_alive(file,os.getuid()+1,now))
            self.assertFalse(probe.heartbeat_alive(file,os.getuid(),now-1))
            link=Path(temp)/'link'; link.symlink_to(file)
            self.assertFalse(probe.heartbeat_alive(link,os.getuid(),now))
            file.chmod(0o644)
            self.assertFalse(probe.heartbeat_alive(file,os.getuid(),now))
            file.chmod(0o600); os.link(file,Path(temp)/'hardlink')
            self.assertFalse(probe.heartbeat_alive(file,os.getuid(),now))

    def test_missing_caller_lease_rejected_before_any_mutation(self):
        with patch.object(probe,'inventory',return_value={'bridges':[]}), patch.object(probe.os,'geteuid',return_value=0), patch.dict(os.environ,{},clear=True), patch.object(probe,'run') as execute:
            with self.assertRaises(ValueError): probe.apply('host',probe.private_state('a'*32),180,session_lease=True)
            execute.assert_not_called()


if __name__=='__main__': unittest.main()
