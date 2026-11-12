FROM python:3.12-slim
WORKDIR /app
RUN useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot /app/data
COPY --chown=bot:bot core.py domain.py main.py settings.py transports.py maintenance.py PROJECT_ID VERSION ./
USER bot
CMD ["python", "main.py"]
