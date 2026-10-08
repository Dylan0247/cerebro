"""Structural checks for the public copy; no network or private files."""
import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicBundleTests(unittest.TestCase):
    def test_python_syntax(self):
        for path in (ROOT / 'src').glob('*.py'):
            with self.subTest(file=path.name):
                ast.parse(path.read_text(encoding='utf-8'))

    def test_paths_are_configurable(self):
        for filename, variable in [('service.py', 'CEREBRO_VAULT'), ('cerebro_memoria.py', 'CEREBRO_VAULT'), ('cerebro_chat_sync.py', 'CEREBRO_CODEX_SESSIONS')]:
            text = (ROOT / 'src' / filename).read_text(encoding='utf-8')
            self.assertIn("os.environ['" + variable + "']", text)

    def test_workflows_are_inactive_and_connected(self):
        for path in (ROOT / 'workflows').glob('*.json'):
            with self.subTest(file=path.name):
                workflow = json.loads(path.read_text(encoding='utf-8'))
                self.assertIs(workflow['active'], False)
                names = {node['name'] for node in workflow['nodes']}
                self.assertEqual(len(names), len(workflow['nodes']))
                for source, outputs in workflow['connections'].items():
                    self.assertIn(source, names)
                    for channels in outputs.values():
                        for edges in channels:
                            for edge in edges:
                                self.assertIn(edge['node'], names)

    def test_no_private_workflow_metadata(self):
        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(set(value) & {'credentials', 'pinData', 'staticData', 'webhookId', 'cachedResultName', 'cachedResultUrl'})
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        for path in (ROOT / 'workflows').glob('*.json'):
            walk(json.loads(path.read_text(encoding='utf-8')))

    def test_webhooks_require_authentication(self):
        for path in (ROOT / 'workflows').glob('*.json'):
            workflow = json.loads(path.read_text(encoding='utf-8'))
            for node in workflow['nodes']:
                self.assertFalse(node.get('disabled', False))
                if node['type'] == 'n8n-nodes-base.webhook':
                    self.assertEqual(node['parameters'].get('authentication'), 'headerAuth')

    def test_no_private_artifacts(self):
        forbidden = {'.sqlite', '.db', '.log', '.jpg', '.png', '.jsonl', '.pyc'}
        for path in ROOT.rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts or '.git' in path.parts:
                continue
            self.assertNotIn(path.suffix, forbidden)
            self.assertNotEqual(path.name, 'token')


if __name__ == '__main__':
    unittest.main()
