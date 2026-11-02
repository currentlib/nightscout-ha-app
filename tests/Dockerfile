# The app image plus a fake GitHub and fake npm, so the real scripts, mongod and
# Nightscout can be tested offline. Build context is tests/.cache.
FROM nightscout-ha-app:test
COPY . /test/
RUN mkdir -p /data && chmod +x /test/bin/*
ENV PATH=/test/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
