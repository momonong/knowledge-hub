import shutil
import subprocess
import unittest
from pathlib import Path


class ReaderJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for browser timing regression')
    def test_original_operation_terminal_state_refreshes_same_page(self):
        result = subprocess.run(['node','checks/reader_result_test.cjs'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required for browser timing regression')
    def test_unsaved_draft_navigation_guard_during_submit(self):
        result = subprocess.run(['node','checks/reader_state_test.cjs'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
