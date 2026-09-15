FROM docker.io/library/python:3.13-slim AS test
WORKDIR /src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
RUN python -m unittest discover -s tests

FROM docker.io/library/python:3.13-slim
ARG GIT_SHA=unknown
ENV APP_VERSION=${GIT_SHA}
ENV HTTP_PORT=8080
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=test /src/src ./src
RUN useradd --system --uid 65532 --gid 0 --home-dir /nonexistent --shell /usr/sbin/nologin app
USER 65532:0
EXPOSE 8080
CMD ["python", "-m", "src.app.server"]
