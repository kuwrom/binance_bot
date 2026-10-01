FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install .
RUN useradd -m bot && mkdir -p /app/data && chown bot /app/data
USER bot
CMD ["shortbot-guardian"]
