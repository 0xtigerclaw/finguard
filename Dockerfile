FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY finguard ./finguard
RUN pip install --no-cache-dir -e .
COPY data ./data
COPY examples ./examples
COPY eval ./eval
COPY tests/recordings ./tests/recordings
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn finguard.web:app --host 0.0.0.0 --port ${PORT:-8080}"]
