"""
gunicorn.conf.py — Aurum Production Server Config

Usage:
    gunicorn -c gunicorn.conf.py server:app

Tuned for:
  - I/O-bound audio streaming (gevent async workers)
  - Low RAM footprint (2 workers + COW via preload_app)
  - Long-lived stream connections (high timeout)
  - Automatic memory leak prevention (max_requests recycle)
  - Render.com + Cloudflare + PWABuilder APK safe
"""

import os
import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────
port    = int(os.environ.get('PORT', 7700))
bind    = f'0.0.0.0:{port}'
backlog = 512

# ── Workers ───────────────────────────────────────────────────────────
# gevent: async I/O — 1 worker handles many concurrent streams via greenlets.
# 2 workers is sweet spot: redundancy without doubling RAM.
# Raise to 3-4 only if CPU becomes the bottleneck (unlikely for streaming).
worker_class       = 'gevent'
workers            = int(os.environ.get('WEB_CONCURRENCY', 2))
worker_connections = 100     # max greenlets per worker (gevent)

# ── Timeouts ─────────────────────────────────────────────────────────
# timeout must exceed longest expected audio stream (~1 hr worst case).
# yt-dlp 25s + stream buffer ke liye safe margin.
timeout          = 300       # 5 min ceiling — gevent handles idle keep-alives fine
graceful_timeout = 30        # finish in-flight requests before hard kill
keepalive        = 5         # seconds to hold idle keep-alive connections

# ── Memory management ─────────────────────────────────────────────────
# preload_app=True → app loaded once in master, workers forked (Copy-On-Write).
# Saves ~30-50 MB RAM vs loading app fresh per worker.
preload_app         = True
max_requests        = 1000   # recycle worker after N requests (prevents slow leaks)
max_requests_jitter = 150    # stagger restarts — avoid thundering-herd

# ── Logging ───────────────────────────────────────────────────────────
accesslog         = '-'      # stdout — Render logs mein dikhega
errorlog          = '-'      # stderr
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

def post_fork(server, worker):
    # gevent monkey-patch — har worker mein karo
    from gevent import monkey
    monkey.patch_all()
    server.log.debug(f'Worker {worker.pid} forked — gevent patched.')
