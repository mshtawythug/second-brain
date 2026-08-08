# Custom Postgres image for second-brain GraphRAG (task G0-1).
# PostgreSQL 16 + pgvector (vector) + pgcrypto (contrib) + Apache AGE (openCypher graph).
#
# Canonical, packaged source of truth for the AGE image. The repo-root
# docker-compose.yml does NOT build this — prod stays on stock
# `pgvector/pgvector:pg16` until the gated AGE cut-over. This image is built only
# by docker-compose.age-test.yml (the port-5434 test instance, build context
# ./src/brain/templates/docker/age) and by the rendered $BRAIN_HOME compose:
# `brain setup` materializes a copy into $BRAIN_HOME/docker/age/Dockerfile so the
# generated compose can build it there.
#
# Why a custom image:
#   - stock `pgvector/pgvector:pg16` has pgvector + pgcrypto but NOT Apache AGE
#   - stock `apache/age:PG16` has AGE but NOT our exact pgvector setup
#   so we start from the pgvector image (keeps the running DB's pgvector ABI/data
#   layout identical) and compile AGE from a pinned release tag against the SAME
#   PG16 server headers, so the resulting .so/.control/.sql match the server exactly.
#
# Pins (immutable):
#   - Base: pgvector/pgvector:0.8.2-pg16   (PG16 + pgvector 0.8.2; matches running DB)
#   - AGE : release tag PG16/v1.5.0-rc0    (commit 0048900f372d4dfd5c236f78b8a8453bcdbb458e)
#           NOTE: upstream Apache AGE has NO GA PG16 tag. `PG16/v1.5.0-rc0` is the
#           latest released PG16 tag (the only newer one is `PG16/v1.6.0-rc0`). It is
#           a release candidate, labelled honestly as "1.5.0-rc0" everywhere — not GA.
#
# Apache AGE does NOT require `shared_preload_libraries=age` for the per-session
# `LOAD 'age';` bootstrap used by `brain init` / `connect`. It can be enabled
# optionally for performance (see docs/specs/2026-05-20-graphrag-age-image.md).

FROM pgvector/pgvector:0.8.2-pg16

# Pinned Apache AGE source. AGE_COMMIT is the immutable checkout; AGE_REF is the
# release tag it must resolve to — verified at build time so the build FAILS if the
# tag ever moves off the pinned commit (drift guard). Override via --build-arg to
# upgrade (bump BOTH the ref and the commit together).
ARG AGE_REPO=https://github.com/apache/age.git
ARG AGE_REF=PG16/v1.5.0-rc0
ARG AGE_COMMIT=0048900f372d4dfd5c236f78b8a8453bcdbb458e

# Build AGE from source against the image's PG16, then drop all build deps so the
# final image only carries the compiled extension artifacts (lean image).
RUN set -eux; \
    apt-mark hold locales; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        git \
        ca-certificates \
        postgresql-server-dev-16 \
        bison \
        flex \
        libreadline-dev \
        zlib1g-dev; \
    git clone "$AGE_REPO" /tmp/age; \
    # drift guard: the pinned tag MUST resolve to the pinned commit
    test "$(git -C /tmp/age rev-list -n 1 "$AGE_REF")" = "$AGE_COMMIT"; \
    git -C /tmp/age checkout "$AGE_COMMIT"; \
    make -C /tmp/age PG_CONFIG="$(command -v pg_config)"; \
    make -C /tmp/age PG_CONFIG="$(command -v pg_config)" install; \
    # sanity: AGE control file must land where PG16 expects extensions
    test -f "$(pg_config --sharedir)/extension/age.control"; \
    rm -rf /tmp/age; \
    apt-get purge -y --auto-remove \
        build-essential \
        git \
        postgresql-server-dev-16 \
        bison \
        flex \
        libreadline-dev \
        zlib1g-dev; \
    apt-mark unhold locales; \
    rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="second-brain-pg16-age-pgvector" \
      org.opencontainers.image.description="PostgreSQL 16 + pgvector 0.8.2 + pgcrypto + Apache AGE 1.5.0-rc0 for second-brain GraphRAG" \
      org.opencontainers.image.base.name="pgvector/pgvector:0.8.2-pg16" \
      brain.age.ref="PG16/v1.5.0-rc0" \
      brain.age.commit="0048900f372d4dfd5c236f78b8a8453bcdbb458e" \
      brain.pgvector.version="0.8.2"
