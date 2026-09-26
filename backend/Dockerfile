# syntax=docker/dockerfile:1
# Build context: repository root (needs config/ as well as backend/).
#
# Behind a TLS-inspecting corporate proxy, pass its CA bundle as a build secret:
#   docker build --secret id=extra_ca,src=path/to/ca-bundle.crt ...
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

# Dependencies first, so code changes don't reinstall them.
COPY backend/pyproject.toml ./
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
    && mkdir dataplat && touch dataplat/__init__.py \
    && pip install . \
    && pip uninstall -y dataplat && rm -rf dataplat

COPY backend/ ./
COPY config/ /app/config/
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
    && pip install --no-deps -e . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin dataplat

USER dataplat
ENV DATAPLAT_CONFIG=/app/config/platform.yaml
ENTRYPOINT ["dataplat"]
CMD ["api"]
