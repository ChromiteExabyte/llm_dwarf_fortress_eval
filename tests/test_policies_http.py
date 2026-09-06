"""Exercise the real HTTP client on loopback; no model service is contacted."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading

import pytest

from dfeval.policies import ChatCompletionsPolicy, PolicyError


@pytest.mark.parametrize("behavior", ["completion", "redirect", "http_error"])
def test_compatible_client_over_http_keeps_credentials_out_of_public_record(monkeypatch, behavior):
    received = []
    credential = "local-test-credential-only"
    monkeypatch.setenv("DFEVAL_LOCAL_TEST_KEY", credential)
    # A misconfigured host proxy must not receive even a local request.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if behavior == "redirect":
                self.send_response(307)
                self.send_header("Location", "/credential-sink")
                body = b""
            elif behavior == "http_error":
                self.send_response(401)
                body = credential.encode()
            else:
                self.send_response(200)
                body = json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"action": "wait", "reason": "Observe next interval", "notebook": ""})}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25}}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        policy = ChatCompletionsPolicy(mode="local", model="test-fixture-only", endpoint=endpoint,
                                       api_key_env="DFEVAL_LOCAL_TEST_KEY", timeout=3)
        if behavior == "completion":
            assert policy.choose({"citizens": [{"name": "Dwarf 矮人"}]}, [])["action"] == "wait"
            assert policy.last_exchange["usage"]["completion_tokens"] == 15
        else:
            with pytest.raises(PolicyError, match="redirects|HTTP") as failure:
                policy.choose({}, [])
            assert credential not in str(failure.value)
        assert len(received) == 1  # No redirect or hidden retry.
        assert received[0]["path"] == "/v1/chat/completions"
        assert received[0]["authorization"] == "Bearer " + credential
        assert received[0]["body"]["model"] == "test-fixture-only"
        assert received[0]["body"]["stream"] is False
        assert credential not in json.dumps(policy.last_exchange)
        assert credential not in json.dumps(policy.public_config())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
