"""
gunicorn.conf.py — Aurum Production Server Config

Usage:
    gunicorn -c gunicorn.conf.py server:app

Tuned for:
  - I/O-bound audio streaming (gthread workers — gevent conflict fix)
  - Low RAM footprint
  - Long-lived stream connections (high timeout)
  - Automatic memory leak prevention (max_requests recycle)
  - Railway + Cloudflare + PWABuilder APK safe
"""

import os

# ── Binding ───────────────────────────────────────────────────────────
port    = int(os.environ.get('PORT', 7700))
bind    = f'0.0.0.0:{port}'
backlog = 512

# ── Workers ───────────────────────────────────────────────────────────
# gthread: thread-based — gevent ki jagah.
# gevent monkey-patch requests/SSL ke saath C-level recursion karta tha Railway pe.
# gthread ke saath ye problem nahi hoti, streaming bhi same kaam karta hai.
worker_class = 'gthread'
workers      = int(os.environ.get('WEB_CONCURRENCY', 2))
threads      = 4   # har worker ke 4 threads — concurrent requests handle karte hain

# ── Timeouts ─────────────────────────────────────────────────────────
timeout          = 300
graceful_timeout = 30
keepalive        = 5

# ── Memory management ─────────────────────────────────────────────────
preload_app         = True
max_requests        = 1000
max_requests_jitter = 150

# ── Logging ───────────────────────────────────────────────────────────
accesslog         = '-'
errorlog          = '-'
loglevel          = os.environ.get('LOG_LEVEL', 'warning')
access_log_format = '%(h)s %(r)s %(s)s %(b)sB %(T)ss'

# ── Safety limits ─────────────────────────────────────────────────────
limit_request_line   = 4096
limit_request_fields = 50

# ── Process naming ────────────────────────────────────────────────────
proc_name = 'aurum'

# ── Hooks ─────────────────────────────────────────────────────────────
def on_starting(server):
    print('[gunicorn] Aurum starting — {} workers ({})'.format(
        workers, worker_class))

def on_exit(server):
    print('[gunicorn] Aurum shutting down — cleanup done.')

def worker_exit(server, worker):
    print('[gunicorn] Worker {} exited'.format(worker.pid))

def worker_abort(worker):
    print('[gunicorn] Worker {} aborted (timeout).'.format(worker.pid))
