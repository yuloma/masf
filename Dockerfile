FROM docker:27-cli AS engine
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-yaml ca-certificates git ripgrep && rm -rf /var/lib/apt/lists/*
FROM runtime AS controller
ARG MASF_REVISION=unknown
LABEL org.opencontainers.image.revision=$MASF_REVISION
COPY --from=engine /usr/local/bin/docker /usr/local/bin/docker
COPY --from=engine /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
RUN apt-get update && apt-get install -y --no-install-recommends gh python3-venv python3-setuptools python3-wheel python3-yaml && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/masf
COPY pyproject.toml .
COPY masf masf
RUN python3 -m venv --system-site-packages /opt/venv && /opt/venv/bin/pip install --no-build-isolation . && ln -s /opt/venv/bin/masf /usr/local/bin/masf
ENTRYPOINT ["masf"]
