"""Versioned guide and reading actions run through a deterministic DOM fixture."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is required for the interactive reader fixture')
class ReadingActions(unittest.TestCase):
    def test_model_guide_renders_and_sends_one_grounded_question(self):
        self.check_fixture('model')

    def test_local_guide_exposes_explicit_retry_and_switches_to_fallback(self):
        self.check_fixture('retry')

    def check_fixture(self, mode):
        directory = Path(__file__).resolve().parent
        script = directory / 'reading_actions_test.cjs'
        client = directory.parent / 'knowledge_hub' / 'static' / 'layers.js'
        result = subprocess.run(['node', str(script), str(client), mode], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
