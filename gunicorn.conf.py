# Copyright (c) 2024, 2025, 2026, Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v1.0 as shown at
# https://oss.oracle.com/licenses/upl/
"""Gunicorn production configuration for ExpressLane."""

# Bind to localhost only — nginx fronts this
bind = "127.0.0.1:5000"

# Workers & threads: app uses threading.Lock for migrations,
# so keep worker count low and use gthread for concurrency
workers = 2
worker_class = "gthread"
threads = 2

# OCI/OCM API calls can be very slow; RMS deploy/cleanup jobs can take 15+ min
# Move to separate worker later — for now, raise timeout to avoid killing mid-job
timeout = 1800
graceful_timeout = 30

# Preload for faster worker fork + shared memory
preload_app = True

# Recycle workers periodically to prevent memory leaks
max_requests = 1000
max_requests_jitter = 50

# Logging — systemd journal captures stdout/stderr
accesslog = "-"
errorlog = "-"
loglevel = "info"
