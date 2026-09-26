FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --system --home /data --uid 10001 blastradius
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Add ".[ntc]" to include circuit-maintenance-parser (~40 carrier formats; pulls numpy/lxml).
ARG EXTRAS=""
RUN pip install ".${EXTRAS}"

RUN mkdir -p /data && chown blastradius:blastradius /data
USER blastradius
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8470
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8470/healthz').status == 200 else 1)"
ENTRYPOINT ["blastradius"]
CMD ["-c", "/etc/blastradius/config.yaml", "run", "--host", "0.0.0.0"]
