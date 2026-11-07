# A Dockerfile that builds, runs, and has five problems nobody notices.

FROM python

WORKDIR /app

# The package index is cached in this layer and never refreshed.
RUN apt-get install -y curl

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build tooling installed into the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libpq-dev

ADD . /app

ENV API_KEY=sk_live_9f8a7b6c5d4e3f2a1b

RUN curl -fsSL https://get.example.com/install.sh | sh

CMD ["python", "-m", "app"]
