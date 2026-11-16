# Builds for whatever platform the host uses. AgentCore Runtime needs ARM64:
#   docker buildx build --platform linux/arm64 .
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY assistant.py auth.py realtime.py tools.py ./
COPY static ./static
# Notes live under /data; mount a persistent disk there to keep them across deploys.
ENV DOCKER_CONTAINER=1 ASSISTANT_NOTES_PATH=/data/notes.json PORT=8080
EXPOSE 8080
CMD ["python", "assistant.py"]
