"""Explicit, budgeted judge-only access to the fixed official DeepSeek endpoint."""
from contextlib import contextmanager
import os
import socket
import threading
import time
import tomllib
import urllib.error
import urllib.request

from reflex.billing import cny_usage, request_ceiling
from reflex.config import ProviderConfig
from reflex.providers import NoRedirect
from .core import ROOT, Problem, encode, parse, require

HOST = 'api.deepseek.com'
ENDPOINT = 'https://api.deepseek.com/chat/completions'
MAX_TOKENS = 2048
PRICE = {'scheme': 'deepseek_cache_cny_v1', 'model': 'deepseek-flash', 'currency': 'CNY',
         'date': '2026-10-08', 'source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'input_per_million': 2.0, 'cached_input_per_million': 0.04, 'output_per_million': 8.0,
         'offpeak_multiplier': 0.5}
_scope = threading.local()


def network_allowed(event, args):
    if not getattr(_scope, 'active', False):
        return False
    if event == 'socket.getaddrinfo':
        return args[0] == HOST and args[1] == 443
    if event == 'socket.connect':
        address = args[1]
        return isinstance(address, tuple) and address[0] in getattr(_scope, 'ips', ()) and address[1] == 443
    return False


@contextmanager
def judge_network():
    require(not getattr(_scope, 'active', False), 'INVALID_STATE', '评审网络上下文不能嵌套')
    _scope.active = True
    try:
        _scope.ips = {x[4][0] for x in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)}
        yield
    finally:
        _scope.active, _scope.ips = False, set()


def provider():
    # Only the requested credential reference is loaded, never returned to the browser.
    with (ROOT / 'config.toml').open('rb') as stream:
        values = tomllib.load(stream).get('small', {})
    key = os.getenv(values.get('api_key_env', ''), '') or values.get('api_key', '')
    require(bool(key), 'JUDGE_NOT_CONFIGURED', 'DeepSeek 凭证未配置；没有发起模型请求')
    require(values.get('endpoint') in {ENDPOINT, 'https://api.deepseek.com/v1/chat/completions'},
            'JUDGE_NOT_CONFIGURED', '初审只允许已配置的 DeepSeek 官方 HTTPS 接口')
    require(values.get('model') == 'deepseek-flash', 'JUDGE_NOT_CONFIGURED', '本轮冻结使用 deepseek-flash，配置已变化')
    return ProviderConfig(endpoint=ENDPOINT, model='deepseek-flash', api_key=key, temperature=0,
                          max_tokens=MAX_TOKENS, extra_body={'thinking': {'type': 'disabled'}}, pricing=PRICE)


def public_config():
    return {'endpoint': ENDPOINT, 'model': 'deepseek-flash', 'max_tokens': MAX_TOKENS,
            'temperature': 0, 'thinking': 'disabled', 'pricing': PRICE, 'automatic_retries': 0}


def ceiling():
    p = ProviderConfig(endpoint=ENDPOINT, model='deepseek-flash', max_tokens=MAX_TOKENS,
                       extra_body={'thinking': {'type': 'disabled'}}, pricing=PRICE)
    return request_ceiling(p)


def call(messages):
    p = provider()
    body = {'model': p.model, 'temperature': 0, 'max_tokens': MAX_TOKENS, 'stream': False,
            'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'}, 'messages': messages}
    require(len(encode(body).encode()) <= 500000, 'JUDGE_INPUT_TOO_LARGE', '初审输入过大，未发送')
    started = time.monotonic()
    status, response, error = None, None, None
    try:
        request = urllib.request.Request(ENDPOINT, data=encode(body).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + p.api_key})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with judge_network(), opener.open(request, timeout=90) as stream:
            status = stream.status
            data = stream.read(2000001)
            require(len(data) <= 2000000, 'JUDGE_RESPONSE_TOO_LARGE', '评审响应超限')
            response = parse(data)
    except urllib.error.HTTPError as exc:
        status, error = exc.code, 'HTTP_' + str(exc.code)
    except Exception as exc:
        error = exc.code if isinstance(exc, Problem) else type(exc).__name__
    response = response if isinstance(response, dict) else {}
    usage = response.get('usage', {})
    usage = usage if isinstance(usage, dict) else {}
    cost = cny_usage(PRICE, usage)
    if type(usage.get('completion_tokens')) is int and usage['completion_tokens'] > MAX_TOKENS:
        cost.update(cost_cny_lower=None, cost_cny_upper=None, usage_error='Output usage exceeds request limit')
    # Do not serialize provider error bodies or request headers, which may contain credentials.
    safe_response = parse(encode(response).replace(p.api_key, '[REDACTED]')) if response else {}
    return {'request': body, 'response': safe_response, 'usage': usage, 'http_status': status,
            'error': error, 'latency_ms': round((time.monotonic()-started)*1000), **cost}
