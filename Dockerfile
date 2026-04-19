FROM docker.io/library/python:3.9-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN groupadd -r expresslane && \
    useradd -r -g expresslane -d /home/expresslane -m expresslane && \
    mkdir -p /app/instance /app/cache /home/expresslane/.oci && \
    chown -R expresslane:expresslane /app /home/expresslane

# Copy application files explicitly (not COPY . .)
COPY app.py config.py models.py gunicorn.conf.py oci_clients.py version.py upgrade_check.py ./
COPY ocm_migration.py migration_sizer.py inventory_dashboard.py inventory_cache.py ./
COPY asset_specs_extractor.py aws_oci_mapping.json ./
COPY templates/ templates/
COPY static/ static/

RUN chown -R expresslane:expresslane /app

USER expresslane
ENV HOME=/home/expresslane

EXPOSE 5000

# Healthcheck is defined at the compose level in docker-compose.yml so that
# it is honored by both `docker compose` (OCI image format) and `podman-compose`
# (which silently drops Dockerfile HEALTHCHECK when building in OCI format).

CMD ["gunicorn", "-c", "gunicorn.conf.py", "--bind", "0.0.0.0:5000", "app:app"]
