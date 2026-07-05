ARG HERMES_BASE=hermes-agent:stable-daocloud
FROM ${HERMES_BASE}

COPY overlays/hermes/gateway/session_context.py /opt/hermes/gateway/session_context.py
COPY overlays/hermes/gateway/run.py /opt/hermes/gateway/run.py
COPY overlays/hermes/gateway/session.py /opt/hermes/gateway/session.py
COPY overlays/hermes/plugins/platforms/dingtalk/adapter.py /opt/hermes/plugins/platforms/dingtalk/adapter.py

