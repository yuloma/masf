FROM masf-runtime:pilot
COPY . /opt/providers/
ENV PATH="/opt/providers/codex:/opt/providers/bin:/usr/local/bin:/usr/bin:/bin"
RUN mkdir -p /opt/providers/bin && if [ -e /opt/providers/cursor/cursor-agent ]; then ln -s ../cursor/cursor-agent /opt/providers/bin/agent; fi
