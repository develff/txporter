FROM opensuse/tumbleweed:latest

# Install AqBanking, Python, and dependencies
RUN zypper --non-interactive refresh && \
    zypper --non-interactive install -y \
        aqbanking \
        libgwenhywfar79-plugins \
        jq \
        python313 \
        python313-Flask \
        python313-requests \
        python313-gunicorn \
        curl \
    && zypper clean --all

# Create non-root user and pre-create the AqBanking data directory so that
# a freshly mounted (empty) Docker volume is already owned by txporter.
RUN useradd -m -s /bin/bash txporter && \
    mkdir -p /home/txporter/.aqbanking && \
    chown -R txporter:txporter /home/txporter/.aqbanking
USER txporter
WORKDIR /home/txporter

# Copy application
COPY --chown=txporter:txporter src/ ./src/
COPY --chown=txporter:txporter scripts/ ./scripts/
COPY --chown=txporter:txporter config/bank_profiles.json ./config/bank_profiles.json
RUN chmod +x scripts/*.sh

EXPOSE 8090

# workers=1: global state (_pending_syncs, _running_proc) must not be shared across workers.
# timeout=300: bank syncs can take up to ~210 s (90 s drain + 120 s complete_fetch).
CMD ["gunicorn", "--chdir", "src", "--bind", "0.0.0.0:8090", "--workers", "1", "--timeout", "300", "server:app"]
