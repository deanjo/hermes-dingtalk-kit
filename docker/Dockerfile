ARG HERMES_BASE=hermes-agent:stable-daocloud
FROM ${HERMES_BASE}

COPY overlays/hermes/plugins/platforms/dingtalk /opt/hermes-dingtalk-kit/overlays/hermes/plugins/platforms/dingtalk
COPY scripts/compat_patcher.py scripts/install_dingtalk_kit.py scripts/post_install_verifier.py /opt/hermes-dingtalk-kit/scripts/

RUN PYTHONDONTWRITEBYTECODE=1 python3 -B /opt/hermes-dingtalk-kit/scripts/install_dingtalk_kit.py --target /opt/hermes
