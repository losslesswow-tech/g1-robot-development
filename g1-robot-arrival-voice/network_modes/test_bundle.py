import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from verify_bundle import verify


class BundleTest(unittest.TestCase):
    def test_tamper_is_detected_before_install_or_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); data=b'candidate'; (root/'source.cpp').write_bytes(data)
            manifest={'files':{'source.cpp':{'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}
            (root/'MANIFEST.json').write_text(json.dumps(manifest))
            self.assertEqual(verify(root)['verified_files'],1)
            (root/'source.cpp').write_bytes(b'modified!')
            with self.assertRaises(ValueError): verify(root)

    def test_manifest_cannot_traverse_outside_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('../outside','/etc/passwd','a\\outside'):
                (root/'MANIFEST.json').write_text(json.dumps({'files':{name:{'bytes':0,'sha256':''}}}))
                with self.assertRaises(ValueError): verify(root)


if __name__=='__main__': unittest.main()
