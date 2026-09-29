# gcloud-workspace-mcp on a distroless Hummingbird Python image, with the
# Google Cloud SDK bundled so the server can mint tokens inside the container.
#
#   podman build -t gcloud-workspace-mcp .
#
# See the README ("Container") for how to run it with your gcloud config.

ARG PYTHON_TAG=3.14

FROM registry.access.redhat.com/hi/python:${PYTHON_TAG}-builder AS build

ARG GCLOUD_VERSION=583.0.0
# SHA-256 of the two release tarballs. Change them together with GCLOUD_VERSION.
ARG GCLOUD_SHA256_X86_64=84c5e4798836bda13aa82c3e84fa1acd0c4e4ca5318f7141052e3a5a26a7cc97
ARG GCLOUD_SHA256_ARM=8ce6287e01e54b53d2e9618d124b62ac85efe5a093904ae027b17f2057030662

# The build stage runs as root to install packages; the runtime image runs as 65532.
# hadolint ignore=DL3002
USER 0

# The Google Cloud SDK, without its bundled Python and the extra CLIs. The
# build tools come from the pinned builder image's repositories. The tarball
# goes to a file first so its hash is checked before anything is unpacked.
# hadolint ignore=DL3041
RUN dnf install -y tar gzip findutils && dnf clean all \
    && case "$(uname -m)" in \
        x86_64) arch=x86_64 sha256="${GCLOUD_SHA256_X86_64}" ;; \
        aarch64) arch=arm sha256="${GCLOUD_SHA256_ARM}" ;; \
        *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; \
    esac \
    && curl -fsSLo /tmp/gcloud.tar.gz "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${arch}.tar.gz" \
    && printf '%s  /tmp/gcloud.tar.gz\n' "${sha256}" > /tmp/gcloud.sha256 \
    && sha256sum -c /tmp/gcloud.sha256 \
    && tar -xzf /tmp/gcloud.tar.gz -C /opt \
    && rm /tmp/gcloud.tar.gz /tmp/gcloud.sha256 \
    && rm -rf /opt/google-cloud-sdk/platform/bundledpythonunix \
              /opt/google-cloud-sdk/platform/bq \
              /opt/google-cloud-sdk/platform/gsutil \
              /opt/google-cloud-sdk/bin/anthoscli \
    && find /opt/google-cloud-sdk -name __pycache__ -type d -prune -exec rm -rf {} + \
    && mkdir /gcloud

# Dependencies come from uv.lock with its hashes, so every build of a commit
# ships the same versions. `--locked` fails the build when uv.lock is stale.
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
COPY . /src
RUN UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never \
    uv sync --project /src --locked --no-dev --no-editable --no-cache

# `bin/gcloud` is a shell script and the runtime image has no shell, so run
# gcloud's Python entry point directly.
RUN printf '%s\n' \
        '#!/opt/venv/bin/python3' \
        'import runpy' \
        'runpy.run_path("/opt/google-cloud-sdk/lib/gcloud.py", run_name="__main__")' \
        > /opt/venv/bin/gcloud \
    && chmod 0755 /opt/venv/bin/gcloud

FROM registry.access.redhat.com/hi/python:${PYTHON_TAG}

COPY --from=build /opt /opt
# Mount point for the host's gcloud config; writable so gcloud works without one.
COPY --from=build --chown=65532:65532 /gcloud /gcloud

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    CLOUDSDK_CONFIG=/gcloud \
    CLOUDSDK_PYTHON=/opt/venv/bin/python3 \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 65532

ENTRYPOINT ["/opt/venv/bin/gcloud-workspace-mcp"]
