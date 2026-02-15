FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=0

# Build deps (covers most native + crypto builds)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    build-essential pkg-config \
    libssl-dev \
 && rm -rf /var/lib/apt/lists/*

# Rust toolchain (Cargo needed for Rust-based Python wheels)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal \
 && rustc --version && cargo --version

WORKDIR /app

RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -U pip maturin

# Copy only dependency metadata first (enables Docker layer cache)
COPY pyproject.toml ./
COPY requirements.txt ./

# Install build tooling + deps with cache
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    pip install -U pip setuptools wheel maturin \
 && pip install -r requirements.txt

# Now copy the rest of the source (changes often)
COPY . .

# Build and install Rust extension, then install project in editable mode
RUN cd amm_sim_rs \
 && pip install maturin \
 && maturin develop --release \
 && cd .. \
 && pip install -e .

# If requirements.txt contains "-e .", this ensures your project is installed (editable)
# (If you DON'T want editable install, remove "-e ." from requirements and use pip install . instead.)
CMD ["python", "--version"]
