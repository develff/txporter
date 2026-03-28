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

CMD ["python3", "src/server.py"]
