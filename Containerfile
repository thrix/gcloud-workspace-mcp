# gcloud-workspace-mcp on a distroless Hummingbird Python image, with the
# Google Cloud SDK bundled so the server can mint tokens inside the container.
#
#   podman build -t gcloud-workspace-mcp .
#
# See the README ("Container") for how to run it with your gcloud config.

ARG PYTHON_TAG=3.14

FROM registry.access.redhat.com/hi/python:${PYTHON_TAG}-builder AS build

ARG GCLOUD_VERSION=583.0.0

USER 0

# The Google Cloud SDK, without its bundled Python and the extra CLIs.
RUN dnf install -y tar gzip findutils && dnf clean all \
    && case "$(uname -m)" in \
        x86_64) arch=x86_64 ;; \
        aarch64) arch=arm ;; \
        *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${arch}.tar.gz" \
        | tar -xz -C /opt \
    && rm -rf /opt/google-cloud-sdk/platform/bundledpythonunix \
              /opt/google-cloud-sdk/platform/bq \
              /opt/google-cloud-sdk/platform/gsutil \
              /opt/google-cloud-sdk/bin/anthoscli \
    && find /opt/google-cloud-sdk -name __pycache__ -type d -prune -exec rm -rf {} +

COPY . /src
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check /src \
    && /opt/venv/bin/pip uninstall -y pip

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

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    CLOUDSDK_CONFIG=/gcloud \
    CLOUDSDK_PYTHON=/opt/venv/bin/python3 \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 65532

ENTRYPOINT ["/opt/venv/bin/gcloud-workspace-mcp"]
