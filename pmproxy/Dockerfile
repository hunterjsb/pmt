# Build stage
FROM rust:1.86-slim AS builder

WORKDIR /app

# Install protobuf compiler
RUN apt-get update && apt-get install -y protobuf-compiler && rm -rf /var/lib/apt/lists/*

# Copy manifests
COPY Cargo.toml Cargo.lock* ./

# Copy source
COPY build.rs ./
COPY proto ./proto
COPY src ./src

# Build release binary
RUN cargo build --release

# Runtime stage
FROM debian:bookworm-slim

# Install CA certificates for HTTPS
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy binary from builder
COPY --from=builder /app/target/release/pmproxy /app/pmproxy

# Default port
EXPOSE 50051

# Run
ENTRYPOINT ["/app/pmproxy"]
CMD ["--host", "0.0.0.0", "--port", "50051"]
