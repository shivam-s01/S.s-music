import os

port = int(os.environ.get('PORT', 7700))
bind = f'0.0.0.0:{port}'
backlog = 512

worker_class = 'gthread'
workers = int(os.environ.get('WEB_CONCURRENCY', 2))
threads = 4

timeout = 300
graceful_timeout = 30
keepalive = 5

preload_app = True
max_requests = 1000
max_requests_jitter = 150

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'warning')
access_log_format = '%(h)s %(r)s %(s)s %(b)sB %(T)ss'

limit_request_line = 4096
limit_request_fields = 50

proc_name = 'aurum'

def on_starting(server):
    print('[gunicorn] Aurum starting — {} workers ({})'.format(workers, worker_class))
