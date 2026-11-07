"""测试用的 OpenAI 兼容接口模拟服务。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockOcrServer:
    """mode: ok | fail_first（前 fail_times 次返回 500）| error（总是 500）| auth（401）| slow | plain（回复无置信度行）
    fail_marker：请求的提示词里含该字符串时总是返回 500（用于让某一类题目失败）。"""

    def __init__(self, mode="ok", fail_times=1, delay=0.0, fail_marker=None):
        self.mode, self.fail_times, self.delay, self.fail_marker = mode, fail_times, delay, fail_marker
        self.calls = 0
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.calls += 1
                    n = outer.calls
                    outer.requests.append({"auth": self.headers.get("Authorization"), "body": body})
                user = body["messages"][-1]["content"]
                text = user if isinstance(user, str) else user[0]["text"]
                images = 0 if isinstance(user, str) else len(user) - 1
                if outer.mode == "slow":
                    time.sleep(outer.delay)
                if outer.mode == "auth":
                    return self._send(401, {"error": "bad key"})
                if outer.mode == "error" or (outer.mode == "fail_first" and n <= outer.fail_times) \
                        or (outer.fail_marker and outer.fail_marker in text):
                    return self._send(500, {"error": "boom"})
                content = f"转写了 {images} 张图" + ("" if outer.mode == "plain" else "\n置信度: 0.9")
                self._send(200, {"model": body["model"], "choices": [{"message": {"content": content}}]})

            def _send(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
