FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config

WORKDIR /app

RUN groupadd --gid 10001 graphrag \
    && useradd --uid 10001 --gid graphrag --no-create-home --shell /usr/sbin/nologin graphrag

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
COPY app.py ./app.py

RUN python -m pip install --no-cache-dir ".[app]"

USER 10001:10001

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
