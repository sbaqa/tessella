# Tessella: the same image runs on a laptop, as a CI step and on a team server.
# Base image pinned by digest; every Python dependency pinned by SHA-256 (requirements.lock);
# runs as an unprivileged user; a CycloneDX SBOM of the installed packages is at /opt/sbom.cdx.json.
#   docker build -t tessella:0.1.0 .
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps . \
    && python -m venv /opt/sbom \
    && /opt/sbom/bin/pip install cyclonedx-bom==7.6.0 \
    && /opt/sbom/bin/cyclonedx-py environment --output-format JSON \
       --output-file /opt/sbom.cdx.json /opt/venv/bin/python

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# git: the diff-since-last-green evidence. Nothing else is added to the base image.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/tessella tessella \
    && git config --system --add safe.directory /work
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/sbom.cdx.json /opt/sbom.cdx.json
USER 10001:10001
WORKDIR /work
EXPOSE 8484
ENTRYPOINT ["tessella"]
CMD ["--help"]
