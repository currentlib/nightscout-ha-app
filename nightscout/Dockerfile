FROM node:22.23.4-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS node

# 4.4.18 is the last MongoDB that runs on a Raspberry Pi 4. 4.4.19 and newer
# need ARMv8.2-A and die with SIGILL, so never move this to a floating 4.4 tag.
FROM mongo:4.4.18@sha256:d23ec07162ca06646a6329c452643f37494af644d045c002a7b41873981c160d

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY rootfs/ /

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && useradd --system --home-dir /data/nightscout --shell /usr/sbin/nologin nightscout \
    && chmod +x /usr/local/bin/run.sh /usr/local/bin/update.sh

ENTRYPOINT ["/usr/local/bin/run.sh"]
CMD []
