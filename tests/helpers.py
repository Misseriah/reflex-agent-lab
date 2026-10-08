import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reflex.config import Config, ProviderConfig


def config(mode="reflex", threshold=0.5, max_steps=10, endpoint="http://127.0.0.1:1"):
    return Config(mode=mode, threshold=threshold, max_steps=max_steps, max_attempts=1,
                  providers={role: ProviderConfig(endpoint=endpoint + "/" + role,
                     model="jev-1.13.0" if role == "jev" else "test-" + role,
                     api_key="test-secret-key") for role in ("jev", "strong", "small")})


def jev_response(choice, confidence=0.9):
    def reply(body):
        options = body["questions"]["next_action"]["criteria"]
        probabilities = {key: 0.01 / (len(options) - 1) for key in options}
        probabilities[choice] = 0.99
        return {"model": "jev-1.13.0", "answers": {"next_action": {
            "type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": probabilities}}, "usage": {"input_tokens": 123, "output_tokens": 17}}
    return reply


def chat_response(action, **arguments):
    return {"model": "test-strong", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"action": action, "arguments": arguments})}}],
        "usage": {"prompt_tokens": 321, "completion_tokens": 29}}


@contextmanager
def fixture_server(replies):
    """Protocol fixture only: it makes no semantic decisions and is never shipped as a model."""
    requests, errors = [], []
    queue = list(replies)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": request, "authorization": self.headers.get("Authorization")})
            try:
                path, status, response = queue.pop(0)
                if self.path != path:
                    raise AssertionError(f"Expected {path}, got {self.path}")
                response = response(request) if callable(response) else response
            except Exception as exc:
                errors.append(exc)
                status, response = 500, {"error": str(exc)}
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), requests
        if errors:
            raise errors[0]
        if queue:
            raise AssertionError(f"Unconsumed HTTP fixtures: {len(queue)}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
