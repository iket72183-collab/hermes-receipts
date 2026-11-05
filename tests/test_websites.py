import http.server
import threading
import time
import unittest
from unittest.mock import patch
from support import plugin
import importlib

checks=importlib.import_module(plugin.__name__+'.checks')


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/short', '/short-wrong', '/close', '/body-deadline', '/chunked', '/chunk-trailer', '/chunk-short', '/chunk-no-end', '/chunk-no-trailer', '/chunk-bad-delimiter', '/chunk-negative'):
            self.send_response(200)
            if self.path.startswith('/chunk'):
                self.send_header('Transfer-Encoding', 'chunked')
            elif self.path.startswith('/short'):
                self.send_header('Content-Length', '500')
            self.end_headers()
            body = {
                '/chunked': b'5\r\nReady\r\n0\r\n\r\n',
                '/chunk-trailer': b'5;fixture=yes\r\nReady\r\n0\r\nX-Fixture: complete\r\n\r\n',
                '/chunk-short': b'A\r\nReady',
                '/chunk-no-end': b'5\r\nReady\r\n',
                '/chunk-no-trailer': b'5\r\nReady\r\n0\r\n',
                '/chunk-bad-delimiter': b'5\r\nReadyxx0\r\n\r\n',
                '/chunk-negative': b'-1\r\nReady\r\n0\r\n\r\n',
                '/short-wrong': b'Wrong',
            }.get(self.path, b'Ready')
            self.wfile.write(body); self.wfile.flush()
            if self.path == '/body-deadline': time.sleep(.4)
            return
        if self.path=='/drip-headers':
            try:
                for fragment in [b'HTTP/1.0 200 OK\r\n', *([b'X-Slow: a\r\n']*15), b'\r\nReady']:
                    self.wfile.write(fragment);self.wfile.flush();time.sleep(.03)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        if self.path=='/slow':time.sleep(.4)
        if self.path=='/redirect':
            self.send_response(302);self.send_header('Location','http://169.254.169.254/');self.end_headers();return
        body=b'Ready' if self.path=='/correct' else b'Wrong'
        self.send_response(200)
        if self.path=='/correct':self.send_header('Content-Length',str(len(body)))
        if self.path=='/large':self.send_header('Content-Length',str(2*1024*1024))
        if self.path=='/compressed':self.send_header('Content-Encoding','gzip')
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def log_message(self,*args):pass


class WebsiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def run_check(self,path,**kwargs):
        return checks.check_website({'url':self.base+path,'contains':'Ready','expected_status':200,**kwargs},allow_loopback_http=True)
    def test_200_wrong_content_fails(self):
        result=self.run_check('/wrong');self.assertEqual(result['state'],'failed');self.assertEqual(result['evidence']['status'],200)
    def test_correct_content_passes(self):self.assertEqual(self.run_check('/correct')['state'],'passed')
    def test_short_declared_body_unverified_with_or_without_required_text(self):
        for path in ('/short', '/short-wrong'):
            with self.subTest(path=path):
                self.assertEqual(self.run_check(path)['state'], 'unverified')
    def test_complete_close_delimited_body_passes(self):
        self.assertEqual(self.run_check('/close')['state'], 'passed')
    def test_complete_chunked_body_passes(self):
        for path in ('/chunked', '/chunk-trailer'):
            with self.subTest(path=path):
                self.assertEqual(self.run_check(path)['state'], 'passed')
    def test_incomplete_chunked_body_unverified(self):
        for path in ('/chunk-short', '/chunk-no-end', '/chunk-no-trailer', '/chunk-bad-delimiter', '/chunk-negative'):
            with self.subTest(path=path):
                self.assertEqual(self.run_check(path)['state'], 'unverified')
    def test_deadline_eof_cannot_pass_matching_prefix(self):
        with patch.object(checks, 'WEBSITE_DEADLINE_SECONDS', .1):
            start = time.monotonic(); result = self.run_check('/body-deadline')
        self.assertEqual(result['state'], 'unverified')
        self.assertLess(time.monotonic() - start, .35)
    def test_wrong_status_fails(self):self.assertEqual(self.run_check('/correct',expected_status=201)['state'],'failed')
    def test_redirect_not_followed(self):self.assertEqual(self.run_check('/redirect')['evidence']['status'],302)
    def test_large_response_unverified(self):self.assertEqual(self.run_check('/large')['state'],'unverified')
    def test_compressed_response_unverified(self):self.assertEqual(self.run_check('/compressed')['state'],'unverified')
    def test_total_timeout(self):
        with patch.object(checks,'WEBSITE_DEADLINE_SECONDS',.1):
            start=time.monotonic();result=self.run_check('/slow')
        self.assertEqual(result['state'],'unverified');self.assertLess(time.monotonic()-start,.35)
    def test_trickled_headers_cannot_extend_deadline(self):
        with patch.object(checks,'WEBSITE_DEADLINE_SECONDS',.1):
            start=time.monotonic();result=self.run_check('/drip-headers')
        self.assertEqual(result['state'],'unverified');self.assertLess(time.monotonic()-start,.35)
    def test_explicit_loopback_opt_in_required(self):
        self.assertIsNotNone(checks.url_refusal(self.base,allow_loopback_http=False))
    def test_private_metadata_credentials_query_and_schemes_refused(self):
        for url in ('http://169.254.169.254/','https://10.0.0.1/','https://[::1]/','file:///etc/passwd','https://user:password@example.com/','https://example.com/?token=secret','https://example.com:garbage/'):
            self.assertIsNotNone(checks.url_refusal(url,allow_loopback_http=True),url)
    def test_pinned_resolution_rejects_private_address(self):
        with patch.object(checks,'_resolve_addresses',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            result=checks.check_website({'url':'https://example.com/','expected_status':200,'contains':'Ready'},allow_loopback_http=False)
        self.assertEqual(result['state'],'unverified');self.assertIn('refused',result['reason'])
    def test_dns_wait_bounded(self):
        def slow(*args,**kwargs):time.sleep(.3);return []
        with patch.object(checks.socket,'getaddrinfo',side_effect=slow):
            start=time.monotonic();self.assertEqual(checks._resolve_addresses('example.com',443,.05),[])
            self.assertLess(time.monotonic()-start,.2)


if __name__=='__main__':unittest.main()
