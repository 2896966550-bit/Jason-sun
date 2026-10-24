"""Flask boundary regressions; valid calls use mocks, never model services."""
import os
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from http_support import AssetVersion


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.client = server.app.test_client()

    def test_non_objects_are_rejected_consistently(self):
        endpoints = ("chat", "chat/stream", "conversation", "text-to-speech", "generate-image", "generate-report")
        with patch.object(self.server, 'local_chat', side_effect=AssertionError('model called')):
            for endpoint in endpoints:
                for payload in ([], ['message'], True, 3, 'text'):
                    with self.subTest(endpoint=endpoint, payload=payload):
                        response = self.client.post('/api/'+endpoint, json=payload)
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.json['execution_status'], 'invalid_input')

    def test_malformed_or_missing_json_never_produces_500(self):
        for path in ('chat', 'chat/stream', 'conversation', 'text-to-speech', 'generate-image', 'generate-report'):
            for body in ('{', '', 'null'):
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.client.post('/api/'+path, data=body, content_type='application/json').status_code,400)

    def test_text_fields_reject_wrong_types_and_required_blanks(self):
        for path,key in (('chat','message'),('chat/stream','message'),('text-to-speech','text'),('generate-image','prompt'),('generate-report','conversation')):
            for value in (None, [], {}, 1, '   '):
                with self.subTest(path=path,value=value):
                    self.assertEqual(self.client.post('/api/'+path,json={key:value}).status_code,400)
        self.assertEqual(self.client.post('/api/conversation', json={'question':[]}).status_code,400)

    def test_valid_chat_retains_context_and_response_shape(self):
        context={'class_id':'screw','specimen_id':'part-A','feature_id':'length'}
        with patch.object(self.server,'local_chat',return_value='fixture answer') as chat:
            response=self.client.post('/api/chat',json={'message':'hello','measurement_context':context})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json,{'response':'fixture answer'})
        self.assertEqual(chat.call_args.kwargs['measurement_context'],context)

    def test_trace_limit_is_bounded_and_invalid_limits_do_not_hit_db(self):
        with patch('observability.trace.recent',return_value=[]) as recent:
            for value in ('bad','1.5','0','-1'):
                self.assertEqual(self.client.get('/api/trace?n='+value).status_code,400)
            recent.assert_not_called()
            self.assertEqual(self.client.get('/api/trace?n=100000').status_code,200)
            recent.assert_called_once_with(100)

    def test_page_still_versions_all_local_script_and_style_refs(self):
        text=self.client.get('/').get_data(as_text=True)
        self.assertIn('/static/js/interaction.js?v=',text)
        self.assertIn('/static/css/motion.css?v=',text)

    def test_same_millisecond_uploads_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(self.server,'UPLOAD_DIR',directory),patch.object(self.server,'audit_log'), \
             patch.object(self.server.time,'time',return_value=1000):
            first=self.client.post('/api/upload',data={'file':(io.BytesIO(b'first fixture'),'same.png')})
            second=self.client.post('/api/upload',data={'file':(io.BytesIO(b'second fixture'),'same.png')})
            self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,200)
            self.assertNotEqual(first.json['name'],second.json['name'])
            self.assertEqual(Path(first.json['path']).read_bytes(),b'first fixture')
            self.assertEqual(Path(second.json['path']).read_bytes(),b'second fixture')

    def test_runtime_database_and_configuration_downloads_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.server,'DATA_DIR',directory):
            for name in ('trace.db','audit.db','memory.sqlite3','trace.db-wal','.env','config.json'):
                Path(directory,name).write_bytes(b'private runtime fixture')
                with self.subTest(name=name):
                    response=self.client.get('/api/data/'+name)
                    self.assertEqual(response.status_code,404)
                    self.assertNotIn(b'private runtime fixture',response.data)

    def test_image_report_and_audio_downloads_remain_available(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.server,'DATA_DIR',directory):
            for name in ('uploads/photo.png','report.pdf','tts_output.mp3'):
                path=Path(directory,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'public media fixture')
                response=self.client.get('/api/data/'+name)
                try:
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.data,b'public media fixture')
                finally:
                    response.close()


class AssetVersionTest(unittest.TestCase):
    def test_equal_size_same_second_edits_get_different_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'css/app.css';path.parent.mkdir()
            path.write_text('a{}')
            stamp=1700000000000000000
            os.utime(path,ns=(stamp,stamp))
            cache=AssetVersion(directory,ttl=0)
            first=cache.get()
            path.write_text('b{}');os.utime(path,ns=(stamp+1000000,stamp+1000000))
            self.assertNotEqual(cache.get(),first)

    def test_cache_reuses_version_within_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=AssetVersion(directory)
            with patch('http_support.time.monotonic',return_value=20):
                first=cache.get()
                self.assertEqual(cache.get(),first)
