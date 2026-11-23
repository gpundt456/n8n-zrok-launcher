"""Windows-first self-hosted n8n launcher with zrok and webhook-only proxy.

This script intentionally DOES NOT publish the n8n editor or local files.
The public zrok tunnel points to a filtering proxy, not directly to n8n.
"""
from __future__ import annotations

import http.client
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=False)


def config_path(name: str, fallback: str = '') -> str:
    return os.environ.get(name, fallback).strip().strip('"')


def port_config(name: str, default: int) -> int:
    value = int(config_path(name, str(default)))
    if not 1 <= value <= 65535:
        raise ValueError(f'{name} must be a valid port')
    return value


@dataclass(frozen=True)
class Settings:
    n8n_port: int
    proxy_port: int
    n8n_command: str
    zrok_command: str
    bot_token: str
    telegram_webhook_id: str
    wait_n8n: int
    wait_zrok: int
    max_request_bytes: int

    @classmethod
    def from_environment(cls):
        return cls(
            n8n_port=port_config('N8N_PORT', 5678),
            proxy_port=port_config('WEBHOOK_PROXY_PORT', 8765),
            n8n_command=config_path('N8N_CMD', 'n8n.cmd' if os.name == 'nt' else 'n8n'),
            zrok_command=config_path('ZROK_EXE', 'zrok'),
            bot_token=config_path('TELEGRAM_BOT_TOKEN'),
            telegram_webhook_id=config_path('N8N_WEBHOOK_ID'),
            wait_n8n=int(config_path('N8N_START_TIMEOUT', '60')),
            wait_zrok=int(config_path('ZROK_URL_TIMEOUT', '90')),
            max_request_bytes=int(config_path('MAX_WEBHOOK_BODY_BYTES', '16777216')),
        )


def executable(name: str) -> str:
    specified = Path(name).expanduser()
    if specified.is_file():
        return str(specified.resolve())
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f'Executable not found: {name}. Set its full path in .env.')


def port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(('127.0.0.1', port)) == 0


class WebhookProxy(BaseHTTPRequestHandler):
    """Forward only production n8n webhooks, blocking editor/API routes.

    This is path filtering, NOT authentication. Individual webhook routes must
    still use n8n's supported authentication when appropriate.
    """
    upstream_port = 5678
    max_body_bytes = 16777216
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Avoid printing webhook identifiers or query strings into logs.
        print('[proxy]', self.address_string(), fmt.split('%')[0].strip())

    def _forward(self):
        parts = urlsplit(self.path)
        raw_path = parts.path
        decoded = unquote(raw_path)
        # Match the exact prefix, forbid dot segments and potentially ambiguous
        # encoded separators. Reject all non-webhook routes by default.
        segments = decoded.split('/')
        acceptable = (
            decoded.startswith('/webhook/')
            and len(decoded) > len('/webhook/')
            and '\\' not in decoded
            and not any(s in ('.', '..') for s in segments)
            and not re.search(r'%2f|%5c|%2e', raw_path, re.IGNORECASE)
            and not decoded.startswith('//')
        )
        if not acceptable:
            return self._error(404, 'Not found')

        transfer_encoding = self.headers.get('Transfer-Encoding', '')
        if transfer_encoding:
            return self._error(400, 'Chunked requests not supported')
        length_str = self.headers.get('Content-Length')
        if length_str is None:
            if self.command in ('POST', 'PUT', 'PATCH'):
                return self._error(411, 'Content-Length required')
            length = 0
        else:
            try:
                length = int(length_str)
            except ValueError:
                return self._error(400, 'Invalid Content-Length')
        if length < 0 or length > self.max_body_bytes:
            return self._error(413, 'Request too large')
        body = self.rfile.read(length) if length else b''

        # Strip hop-by-hop and forwarded headers supplied by untrusted callers.
        excluded = {
            'host', 'connection', 'keep-alive', 'proxy-authenticate',
            'proxy-authorization', 'te', 'trailers', 'transfer-encoding',
            'upgrade', 'content-length', 'x-forwarded-for',
            'x-forwarded-host', 'x-forwarded-proto', 'forwarded',
        }
        headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
        headers['Host'] = f'127.0.0.1:{self.upstream_port}'
        headers['Content-Length'] = str(len(body))
        headers['Connection'] = 'close'
        # The tunnel terminates HTTPS. Never trust the original caller's
        # forwarded headers; the proxy controls those metadata fields.
        headers['X-Forwarded-Proto'] = 'https'

        connection = http.client.HTTPConnection('127.0.0.1', self.upstream_port, timeout=45)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in {
                    'content-length', 'connection', 'transfer-encoding',
                    'keep-alive', 'upgrade', 'server', 'date'
                }:
                    continue
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self._error(502, 'Local n8n is unavailable')
        finally:
            connection.close()
            self.close_connection = True

    def _error(self, status: int, reason: str):
        data = (reason + '\n').encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)
        self.close_connection = True

    def do_GET(self): self._forward()
    def do_POST(self): self._forward()
    def do_PUT(self): self._forward()
    def do_PATCH(self): self._forward()
    def do_DELETE(self): self._forward()
    def do_HEAD(self): self._forward()


def start_proxy(settings: Settings):
    class ConfiguredProxy(WebhookProxy):
        upstream_port = settings.n8n_port
        max_body_bytes = settings.max_request_bytes

    server = ThreadingHTTPServer(('127.0.0.1', settings.proxy_port), ConfiguredProxy)
    threading.Thread(target=server.serve_forever, name='webhook-proxy', daemon=True).start()
    print(f'[proxy] Listening on 127.0.0.1:{settings.proxy_port}; /webhook/* only')
    return server


def stop_owned_process(process: subprocess.Popen | None, name: str):
    if process is None or process.poll() is not None:
        return
    print(f'[{name}] Stopping process tree started by this launcher')
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start_n8n(settings: Settings, public_url: str):
    env = os.environ.copy()
    # Configure the n8n subprocess in memory; don't overwrite any existing
    # ~/.n8n/.env file or weaken n8n's security-related configuration.
    env.update({
        'N8N_PORT': str(settings.n8n_port),
        'N8N_LISTEN_ADDRESS': '127.0.0.1',
        'N8N_PROTOCOL': 'http',
        'WEBHOOK_URL': public_url.rstrip('/') + '/',
        'N8N_PROXY_HOPS': '1',
    })
    command = executable(settings.n8n_command)
    if os.name == 'nt' and command.lower().endswith(('.cmd', '.bat')):
        args = ['cmd.exe', '/d', '/c', command, 'start']
    else:
        args = [command, 'start']
    options = {'env': env, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.STDOUT}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(args, **options)
    print(f'[n8n] Started local n8n (PID {process.pid})')
    return process


def wait_until_n8n(settings: Settings, process: subprocess.Popen):
    deadline = time.monotonic() + settings.wait_n8n
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('n8n process exited before becoming available.')
        try:
            response = requests.get(f'http://127.0.0.1:{settings.n8n_port}/', timeout=2)
            if response.status_code < 500:
                print('[n8n] Local n8n is responding')
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise TimeoutError('n8n did not start in time. Check your local n8n installation.')


def read_output(pipe, lines, stop_event):
    try:
        for raw in iter(pipe.readline, ''):
            if stop_event.is_set():
                break
            lines.append(raw.strip())
            if len(lines) > 150:
                del lines[:50]
            # Don't print raw zrok output, which may include configuration details.
    finally:
        pipe.close()


def start_zrok(settings: Settings):
    zrok = executable(settings.zrok_command)
    check = subprocess.run([zrok, 'status'], capture_output=True, text=True, timeout=12)
    if check.returncode != 0 or 'not enabled' in (check.stdout + check.stderr).lower():
        raise RuntimeError('zrok is not ready. Run "zrok enable" using your own account first.')
    args = [zrok, 'share', 'public', '--headless', f'http://127.0.0.1:{settings.proxy_port}']
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True, 'bufsize': 1}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    proc = subprocess.Popen(args, **options)
    lines = []
    stop = threading.Event()
    for pipe in (proc.stdout, proc.stderr):
        threading.Thread(target=read_output, args=(pipe, lines, stop), daemon=True).start()
    pattern = re.compile(r'https://[A-Za-z0-9-]+\.share\.zrok\.io\b', re.IGNORECASE)
    deadline = time.monotonic() + settings.wait_zrok
    while time.monotonic() < deadline:
        for line in list(lines):
            found = pattern.search(line)
            if found:
                return proc, found.group(0)
        if proc.poll() is not None:
            raise RuntimeError('zrok exited early. Check zrok configuration and limits.')
        time.sleep(1)
    stop.set()
    stop_owned_process(proc, 'zrok')
    raise TimeoutError('Could not detect zrok HTTPS URL in allotted time.')


def register_telegram(settings: Settings, public_url: str) -> bool:
    if not settings.bot_token or not settings.telegram_webhook_id:
        print('[telegram] Not configured; skipping manual webhook registration')
        return False
    webhook_url = public_url.rstrip('/') + '/webhook/' + settings.telegram_webhook_id.strip('/')
    # Registration is optional: n8n's Telegram Trigger may register webhooks itself.
    api = f'https://api.telegram.org/bot{settings.bot_token}/setWebhook'
    try:
        response = requests.post(api, data={'url': webhook_url}, timeout=15)
        if not response.ok:
            raise RuntimeError('Telegram webhook registration failed (HTTP error)')
        result = response.json()
    except requests.RequestException:
        # Don't include exception URLs: Telegram bot tokens are part of API URLs.
        raise RuntimeError('Telegram webhook registration failed (network error)') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected webhook registration')
    print('[telegram] Webhook registered (token and full webhook URL hidden)')
    return True


def unregister_telegram(settings: Settings):
    if not settings.bot_token:
        return
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{settings.bot_token}/deleteWebhook', timeout=8
        )
        if response.ok:
            print('[telegram] Webhook registration deleted')
    except requests.RequestException:
        print('[telegram] Could not clear webhook; check it manually')


def main() -> int:
    if os.name != 'nt':
        print('[note] Designed for Windows; other systems have not been verified.')
    settings = Settings.from_environment()
    if settings.n8n_port == settings.proxy_port:
        raise ValueError('N8N_PORT and WEBHOOK_PROXY_PORT must differ')
    if port_taken(settings.n8n_port):
        raise RuntimeError(
            f'Local port {settings.n8n_port} is already in use. '
            'Close the existing service before starting. No other processes were killed.'
        )
    if port_taken(settings.proxy_port):
        raise RuntimeError(f'Proxy port {settings.proxy_port} is already in use')

    n8n_process = None
    zrok_process = None
    proxy = None
    telegram_registered = False
    try:
        proxy = start_proxy(settings)
        # Start n8n initially with localhost URL, then restart with the zrok
        # public URL; n8n reads WEBHOOK_URL at process startup.
        n8n_process = start_n8n(settings, f'http://127.0.0.1:{settings.n8n_port}')
        wait_until_n8n(settings, n8n_process)
        zrok_process, url = start_zrok(settings)
        print('[zrok] HTTPS webhook tunnel ready')
        stop_owned_process(n8n_process, 'n8n')
        n8n_process = None
        deadline = time.monotonic() + 20
        while port_taken(settings.n8n_port) and time.monotonic() < deadline:
            time.sleep(0.5)
        if port_taken(settings.n8n_port):
            raise RuntimeError('n8n still owns its local port after restart. Stop it manually.')
        n8n_process = start_n8n(settings, url)
        wait_until_n8n(settings, n8n_process)
        print('[n8n] Public WEBHOOK_URL configured; editor remains local')

        # Test that the local proxy refuses editor access.
        r = requests.get(f'http://127.0.0.1:{settings.proxy_port}/', timeout=5)
        if r.status_code != 404:
            raise RuntimeError('Proxy safety check failed: root was not blocked')

        telegram_registered = register_telegram(settings, url)
        print('')
        print('READY — public URL:', url)
        print(f'LOCAL EDITOR — http://127.0.0.1:{settings.n8n_port}')
        print('Only /webhook/* paths are passed by the public tunnel.')
        print('Press Ctrl+C to stop this launcher.')
        while True:
            if n8n_process.poll() is not None:
                raise RuntimeError('n8n exited unexpectedly')
            if zrok_process.poll() is not None:
                raise RuntimeError('zrok tunnel exited unexpectedly')
            time.sleep(2)
    except KeyboardInterrupt:
        print('\n[main] Stopping')
        return 0
    finally:
        if telegram_registered:
            unregister_telegram(settings)
        stop_owned_process(n8n_process, 'n8n')
        stop_owned_process(zrok_process, 'zrok')
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'[error] {exc}', file=sys.stderr)
        sys.exit(1)
