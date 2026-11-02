# Nightscout

Runs Nightscout and its own MongoDB inside Home Assistant OS. The Nightscout code
is downloaded and built on your Pi, not shipped in the image.

**Do not rely on this as your only low-glucose alarm.** Backups, restarts and updates
all cause short gaps.

## Requirements

- Home Assistant OS, 64-bit. Run `ha info` in the Terminal & SSH app: `arch` must be `aarch64`.
- Raspberry Pi 4 with 4 GB of RAM. It should also work on a Pi 5, but that is untested.
- About 1 GB of free storage.

## First start

1. Set `api_secret` to a long random passphrase, at least 12 characters, then start the app.
2. Watch the **Log** tab. The first build takes about 20 minutes on an SD card. Until it
   finishes, the web port shows an "is being installed" page.
3. When the log says `Nightscout <version> is running`, open the web UI and log in with
   your `api_secret`.

Turn on the **Watchdog** on the app's page after the first start.

## Options

| Option | Default | Meaning |
|---|---|---|
| `api_secret` | required | Admin password for Nightscout (`API_SECRET`). |
| `nightscout_version` | `latest` | `latest` for the newest GitHub release, or a version such as `15.0.9`. |
| `auto_update` | `true` | Look for a new release on each start. A pinned version is always applied. |
| `display_units` | `mmol` | `mmol` or `mg/dl`. |
| `enable` | see app | Space-separated Nightscout plugins (`ENABLE`). |
| `base_url` | not set | Your public address, for example from Cloudflare Tunnel (`BASE_URL`). |
| `time_format` | `24` | `12` or `24`. |
| `language` | `en` | Nightscout language code. |
| `extra_env` | empty | Any other Nightscout setting, as `KEY=VALUE`. |

`MONGODB_URI`, `PORT`, `NODE_ENV` and `INSECURE_USE_HTTP` are set by the app and
can't be overridden. Set `NS_PROBE_SECONDS=180` in `extra_env` if a healthy new version
ever gets rolled back because the Pi was too slow to start it within 90 seconds.

The host port is changed in the app's **Network** section.

## Security: read at home, upload from anywhere

`AUTH_DEFAULT_ROLES` defaults to `denied` here, unlike upstream Nightscout. Nobody can read
or write anything without your `api_secret` or an access token, on your network or from the
internet. This is what makes it safe to publish through a tunnel.

Don't change it to `readable` while the app is published. Anyone who finds the address could
then read the glucose history. Port 1337 is plain HTTP, so keep it off the internet and use
the tunnel for outside access.

1. **Publish it:** point Cloudflare Tunnel at `http://<hostname>:1337` (hostname below).
   Set `base_url` to the public `https://` address.
2. **Phones and browsers at home:** create a token (below) with role `readable`, then open
   `http://<pi-ip>:1337/?token=<token>` once.

### Access tokens

In Nightscout, open the menu, then **Admin Tools**, and add a subject with the role you want.
Copy the generated token. It's safe to delete a subject to revoke its token.

### Linx CGM

In the Linx Nightscout connector, enter the public `https://` address from `base_url`, and
your `api_secret`. Don't use the `http://` address, and don't add a slash at the end. These
steps come from how Nightscout uploaders work in general and have not been tried with Linx.
Confirm with a reading on the Nightscout graph.

If the connector accepts an access token instead, prefer that: Admin Tools can create a role
with only the `api:entries:create` permission. A `readable` token can't upload.

## Home Assistant integration

Add the **Nightscout** integration with:

- **URL:** `http://<hostname>:1337`. It keeps working if the Pi's IP changes. The hostname looks
  like `a1b2c3d4-nightscout`, and the first part is the first 8 characters of the SHA-1 hash of
  this repository's lowercase URL. Run `ha apps` to see the app's ID, then replace `_` with `-`.
- **API Key:** a token with role `readable`, not your `api_secret`.

The sensor reports mg/dL. To see mmol/L, open the entity's settings and change its unit
of measurement. Or use a template sensor, after checking the entity ID in
**Developer Tools > States**:

```yaml
template:
  - sensor:
      - name: Glucose mmol
        unit_of_measurement: mmol/L
        state_class: measurement
        state: "{{ (states('sensor.blood_sugar') | float / 18.016) | round(1) }}"
        availability: "{{ has_value('sensor.blood_sugar') }}"
```

## Updates

On each start the app looks for a new release. The old version keeps serving while the new
one builds, then Nightscout restarts for about a minute. A failed download, build or Node
version check leaves the old version running and logs why.

If the new version doesn't come up, the app rolls back by itself and won't try that
version again. To retry it, set `nightscout_version` to that version.

Nightly backups restart the app, so they also check for updates. Without them, use:

```yaml
automation:
  - alias: Restart Nightscout nightly
    triggers:
      - trigger: time
        at: "03:30:00"
    actions:
      - action: hassio.app_restart
        data:
          app: <repository-hash>_nightscout
```

Update builds run at low priority, and are the first thing to stop if RAM runs out.

## Backups

Backups stop the app, copy the database, and start it again. Expect a gap of a few minutes.
Only MongoDB data and app state are backed up, not the Nightscout code.

To restore, use **Settings > System > Backups**. The database comes back straight away, and
the app rebuilds the Nightscout version that was running, so it needs internet access once.

## MongoDB version

This app stays on MongoDB 4.4.18 because it's the last version that runs on a Pi 4. 4.4.19
and later crash on its CPU. MongoDB 4.4 is end-of-life, so it only listens inside the app.

Nightscout has marked 4.4 as deprecated. If a future release drops it, the app rolls back to
the last working version and stays there. A Pi 5 can use newer MongoDB versions, but moving
needs a `mongodump` and `mongorestore` and a new app version.
