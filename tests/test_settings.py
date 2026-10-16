import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'ttb_miner_farm_v5.py'
spec = importlib.util.spec_from_file_location('ttb_v52_settings', SRC)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / 'farm_config.json'
        self.db = Path(self.tmp.name) / 'testing.sqlite3'
        self.cfg.write_text(json.dumps({
            'farm_name': 'My farm', 'wallet': '', 'poll_seconds': 30,
            'pool_poll_seconds': 90, 'custom_plugin_field': 'keep this',
            'miners': [{'id': 'esp01', 'name': 'Old name', 'ip': '192.168.1.201',
                        'expected_khs': 990, 'max_hashrate_factor': 3,
                        'enabled': True, 'custom_miner_setting': 'keep me'}]
        }))
        self.farm = app.Farm(self.cfg, self.db)

    def changed(self):
        snap = self.farm.settings_snapshot()
        snap['settings']['farm_name'] = 'TtB Workshop Farm'
        snap['settings']['miners'][0]['name'] = 'Garden Shed'
        return snap

    def test_save_preserves_ids_and_unedited_fields_and_backup(self):
        new = self.farm.save_settings(self.changed())
        self.assertEqual(new['settings']['farm_name'], 'TtB Workshop Farm')
        loaded = json.loads(self.cfg.read_text())
        self.assertEqual(loaded['miners'][0]['id'], 'esp01')
        self.assertEqual(loaded['miners'][0]['name'], 'Garden Shed')
        self.assertEqual(loaded['miners'][0]['custom_miner_setting'], 'keep me')
        self.assertEqual(loaded['custom_plugin_field'], 'keep this')
        backups = list(self.cfg.parent.glob('farm_config.json.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['farm_name'], 'My farm')
        self.assertEqual(self.cfg.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.farm.metrics()['farm_name'], 'TtB Workshop Farm')

    def test_edit_conflict_does_not_touch_file(self):
        first = self.changed()
        original = self.cfg.read_bytes()
        self.cfg.write_bytes(original + b' \n')
        with self.assertRaises(app.ConfigConflict):
            self.farm.save_settings(first)
        self.assertFalse(list(self.cfg.parent.glob('farm_config.json.backup-*')))

    def test_invalid_settings_rejected_without_writing(self):
        first = self.changed()
        first['settings']['miners'][0]['expected_khs'] = -42
        original = self.cfg.read_bytes()
        with self.assertRaises(ValueError):
            self.farm.save_settings(first)
        self.assertEqual(self.cfg.read_bytes(), original)
        self.assertFalse(list(self.cfg.parent.glob('farm_config.json.backup-*')))

    def test_add_disable_and_remove(self):
        first = self.changed()
        first['settings']['miners'][0]['enabled'] = False
        first['settings']['miners'].append({'id':'esp02','name':'Solar Shed','ip':'192.168.1.202',
                  'enabled':True,'expected_khs':990,'max_hashrate_factor':3})
        ans = self.farm.save_settings(first)
        self.assertEqual(len(ans['settings']['miners']), 2)
        self.assertEqual(self.farm.metrics()['miner_count'], 1)
        second = ans
        second['settings']['miners'] = second['settings']['miners'][1:]
        self.farm.save_settings(second)
        self.assertEqual(json.loads(self.cfg.read_text())['miners'][0]['id'],'esp02')

    def test_endpoint_protects_settings_write(self):
        class TestHandler(app.Handler):
            pass
        TestHandler.farm = self.farm
        TestHandler.admin_key = 'long-test-key-this-is-at-least-24chars'
        srv = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        worker = threading.Thread(target=srv.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (srv.shutdown(),srv.server_close(),worker.join(timeout=5)))
        base = 'http://127.0.0.1:' + str(srv.server_port)
        payload = json.dumps(self.changed()).encode()
        def post(key, origin=None, body=payload):
            headers={'Content-Type':'application/json','X-TTB-Admin-Key':key}
            if origin is not None: headers['Origin']=origin
            try:
                with urllib.request.urlopen(urllib.request.Request(base+'/api/config',body,headers,method='POST')) as r:
                    return r.status,json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code,json.loads(e.read())
        self.assertEqual(post('wrong',base)[0],403)
        self.assertEqual(post(TestHandler.admin_key,None)[0],403)
        self.assertEqual(post(TestHandler.admin_key,'http://other-site.test')[0],403)
        self.assertEqual(post(TestHandler.admin_key,base)[0],200)
        self.assertEqual(self.farm.config['farm_name'],'TtB Workshop Farm')
        self.assertEqual(post(TestHandler.admin_key,base)[0],409) # stale revision
        TestHandler.admin_key=None
        self.assertEqual(post('anything',base)[0],403)
        with urllib.request.urlopen(base+'/api/config') as r:
            self.assertEqual(json.loads(r.read())['settings']['farm_name'],'TtB Workshop Farm')

if __name__ == '__main__':
    unittest.main()
