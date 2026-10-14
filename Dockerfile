FROM docker.io/library/python:3.13-slim-trixie AS test
WORKDIR /src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests
RUN python -m pip install --no-cache-dir --target /opt/python . \
    && PYTHONPATH=/opt/python python -m unittest discover -s tests

FROM gcr.io/distroless/python3-debian13
ARG GIT_SHA=unknown
ENV APP_VERSION=${GIT_SHA}
ENV HTTP_PORT=8080
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=test /src/app ./app
COPY --from=test /opt/python ./python
ENV PYTHONPATH=/app/python
USER 65532:0
EXPOSE 8080
CMD ["-m", "app.main"]
