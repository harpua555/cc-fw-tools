FROM ubuntu:24.04

# Core env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FW_VER=1.1.40

# Enable Universe repo (contains squashfs-tools) and install runtime/build deps
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common && \
    add-apt-repository -y universe && \
    apt-get update

# Install runtime and build dependencies for cc-fw-tools and the web app
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip \
    python3-flask gunicorn \
    git git-lfs \
    bsdiff squashfs-tools cpio unzip zip \
    curl wget ca-certificates openssl xz-utils sudo dos2unix \
    && git lfs install \
    && rm -rf /var/lib/apt/lists/*

# Ensure tools are on sudo's PATH and available without /usr/sbin in secure_path
RUN set -eux; \
    # Only create symlinks if targets exist and destination is missing
    if [ -x /usr/sbin/mksquashfs ] && [ ! -x /usr/bin/mksquashfs ]; then ln -s /usr/sbin/mksquashfs /usr/bin/mksquashfs; fi; \
    if [ -x /usr/sbin/unsquashfs ] && [ ! -x /usr/bin/unsquashfs ]; then ln -s /usr/sbin/unsquashfs /usr/bin/unsquashfs; fi; \
    printf 'Defaults secure_path="/usr/sbin:/usr/bin:/sbin:/bin"\n' > /etc/sudoers.d/99-path

# Verify required binaries exist (fail fast if missing)
RUN set -eux; \
    ( [ -x /usr/bin/mksquashfs ] || [ -x /usr/sbin/mksquashfs ] ) && \
    ( [ -x /usr/bin/unsquashfs ] || [ -x /usr/sbin/unsquashfs ] )

WORKDIR /app

# Copy source
COPY . .

# Normalize line endings and ensure scripts are executable (our scripts and cc-fw-tools)
RUN set -eux; \
    # Normalize our scripts
    find scripts -type f -name "*.sh" -exec dos2unix {} + 2>/dev/null || true; \
    find scripts -type f -name "*.sh" -exec chmod +x {} + 2>/dev/null || true; \
    # Normalize and make executable all cc-fw-tools shell and python entry scripts
    find cc-fw-tools -type f -name "*.sh" -exec dos2unix {} + 2>/dev/null || true; \
    find cc-fw-tools -type f -name "*.sh" -exec chmod +x {} + 2>/dev/null || true; \
    find cc-fw-tools/TOOLS -maxdepth 1 -type f -name "*.py" -exec dos2unix {} + 2>/dev/null || true; \
    find cc-fw-tools/TOOLS -maxdepth 1 -type f -name "*.py" -exec chmod +x {} + 2>/dev/null || true; \
    find cc-fw-tools/oc-patches -type f -name "patch.py" -exec dos2unix {} + 2>/dev/null || true; \
    find cc-fw-tools/oc-patches -type f -name "patch.py" -exec chmod +x {} + 2>/dev/null || true; \
    # Fallback
    if [ -f build-and-run.sh ]; then dos2unix build-and-run.sh 2>/dev/null || true; chmod +x build-and-run.sh || true; fi

# Ensure scripts are executable
RUN chmod +x scripts/start.sh || true && \
    chmod +x scripts/build_artifact.sh || true

# Expose web port
EXPOSE 8080

# Start the web server (which updates cc-fw-tools and serves UI)
ENTRYPOINT ["scripts/start.sh"]