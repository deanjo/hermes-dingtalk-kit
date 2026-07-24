ARG HERMES_BASE=hermes-agent:stable-daocloud
FROM ${HERMES_BASE}

COPY overlays/hermes/plugins/platforms/dingtalk /opt/hermes-dingtalk-kit/overlays/hermes/plugins/platforms/dingtalk
COPY overlays/hermes/plugins/product_confirmation /opt/hermes-dingtalk-kit/overlays/hermes/plugins/product_confirmation
COPY overlays/hermes/plugins/h1_task_write /opt/hermes-dingtalk-kit/overlays/hermes/plugins/h1_task_write
COPY scripts/compat_patcher.py scripts/install_dingtalk_kit.py scripts/post_install_verifier.py /opt/hermes-dingtalk-kit/scripts/

# H1 environment debt (B6): bake jq + pymysql into the image. Runtime skills
# (e.g. uat-mysql-query) execute inside this container on Debian 13; the
# sealed venv has no pip, so install pymysql via uv into /opt/hermes/.venv.
# Build-time baking is the sanctioned path — runtime lazy installs are
# disabled by HERMES_DISABLE_LAZY_INSTALLS=1 in the base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/* \
    && uv pip install --python /opt/hermes/.venv/bin/python --no-cache pymysql

RUN rm -rf /opt/hermes/plugins/platforms/dingtalk \
    && PYTHONDONTWRITEBYTECODE=1 python3 -B /opt/hermes-dingtalk-kit/scripts/install_dingtalk_kit.py --target /opt/hermes --plugins-only
