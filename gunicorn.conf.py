"""
gunicorn.conf.py — GODMODE v4.0 Production Server Config

Usage:
    gunicorn -c gunicorn.conf.py app:app

Tuned for:
  - I/O-bound audio streaming (gevent async workers)
  - Low RAM footprint (2 workers + COW via preload_app)
  - Long-lived stream connections (high timeout)
  - Automatic memory leak prevention (max_requests recycle)
"""

import os

# ── Binding ───────────────────────────────────────────────────────────
bind    = '0.0.0.0:{}'.format(os.environ.get('PORT', 7700))
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
# Set to 300s (5 min) as a practical ceiling; gevent handles idle keep-alives fine.
timeout          = 300
graceful_timeout = 30       # time to finish in-flight requests before hard kill
keepalive        = 5        # seconds to hold idle keep-alive connections

# ── Memory management ─────────────────────────────────────────────────
# preload_app=True → app loaded once in master, workers forked (Copy-On-Write).
# Saves ~30-50 MB RAM vs loading app fresh per worker.
preload_app         = True
max_requests        = 1000  # recycle worker after N requests (prevents slow leaks)
max_requests_jitter = 150   # stagger restarts to avoid thundering-herd

# ── Logging ───────────────────────────────────────────────────────────
accesslog = '-'             # stdout
errorlog  = '-'             # stdout
loglevel  = os.environ.get('LOG_LEVEL', 'warning')
access_log_format = '%(h)s %(r)s %(s)s %(b)sB %(T)ss'

# ── Safety limits ─────────────────────────────────────────────────────
limit_request_line   = 4096
limit_request_fields = 50

# ── Hooks ─────────────────────────────────────────────────────────────
def on_starting(server):
    print('[gunicorn] GODMODE v4.0 starting — {} workers ({})'.format(
        workers, worker_class))

def worker_exit(server, worker):
    print('[gunicorn] Worker {} exited'.format(worker.pid))
