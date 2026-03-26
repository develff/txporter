FROM debian:bookworm-slim

# Install AqBanking and dependencies
RUN apt-get update && apt-get install -y \
    aqbanking-tools \
    libaqbanking-dev \
    python3 \
    python3-pip \
    python3-flask \
    python3-requests \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash txporter
USER txporter
WORKDIR /home/txporter

# Copy application
COPY --chown=txporter:txporter src/ ./src/
COPY --chown=txporter:txporter scripts/ ./scripts/
RUN chmod +x scripts/*.sh

EXPOSE 8090

CMD ["python3", "src/server.py"]
