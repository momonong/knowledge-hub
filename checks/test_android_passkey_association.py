import base64
import json
import unittest
from pathlib import Path

from knowledge_hub.passkeys import ORIGINS


class AndroidPasskeyAssociation(unittest.TestCase):
    def test_release_signer_has_both_passkey_relations_on_the_readers_domain(self):
        release_entry = Path(__file__).resolve().parents[1] / 'android' / 'assetlinks.release-entry.json'
        statement = json.loads(release_entry.read_text(encoding='utf-8'))
        target = statement['target']
        self.assertEqual(target['namespace'], 'android_app')
        self.assertEqual(target['package_name'], 'com.momonong.knowledgehub')
        self.assertEqual(len(target['sha256_cert_fingerprints']), 1)
        fingerprint = bytes.fromhex(target['sha256_cert_fingerprints'][0].replace(':', ''))
        self.assertEqual(len(fingerprint), 32)
        origin_hash = base64.urlsafe_b64encode(fingerprint).rstrip(b'=').decode('ascii')
        self.assertIn('android:apk-key-hash:' + origin_hash, ORIGINS)
        self.assertCountEqual(statement['relation'], [
            'delegate_permission/common.get_login_creds',
            'delegate_permission/common.handle_all_urls',
        ])
