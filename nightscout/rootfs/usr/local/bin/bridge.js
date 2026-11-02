// Publishes Nightscout as a native Home Assistant device through MQTT discovery.
// Uses only Node's standard library: the app image installs nothing at build time.
'use strict';
const crypto = require('crypto');
const net = require('net');
const tls = require('tls');

const API = 'http://127.0.0.1:1337';
const BASE = 'nightscout';
const AVAILABILITY = `${BASE}/availability`;
const STATE = `${BASE}/state`;
const STATS = `${BASE}/stats`;
const POLL_MS = 60 * 1000;
const STATS_MS = 15 * 60 * 1000;
const RECONNECT_MS = 15 * 1000;
const NO_BROKER_MS = 60 * 1000;
const STALE_MS = 15 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const TREATMENT_DAYS = 7;
const MG_PER_MMOL = 18.01559; // Nightscout's MMOL_TO_MGDL
const LEVELS = ['Urgent low', 'Low', 'In range', 'High', 'Urgent high'];

const mmol = (process.env.DISPLAY_UNITS || '').includes('mmol');
const unit = mmol ? 'mmol/L' : 'mg/dL';
const headers = {'api-secret': crypto.createHash('sha1').update(process.env.API_SECRET).digest('hex')};

const log = (message) => console.log(`[nightscout-mqtt] ${message}`);
const round = (value, digits = 1) => (value == null ? null : Number(value.toFixed(digits)));
const scale = (mgdl) => (mgdl == null ? null : mmol ? round(mgdl / MG_PER_MMOL) : Math.round(mgdl));
const iso = (ms) => (ms ? new Date(ms).toISOString() : null);

// --- Reading Nightscout ---------------------------------------------------

async function api(path) {
  const response = await fetch(API + path, {headers, signal: AbortSignal.timeout(30000)});
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function level(mgdl, t) {
  if (mgdl == null) return null;
  if (mgdl < t.bgLow) return LEVELS[0];
  if (mgdl < t.bgTargetBottom) return LEVELS[1];
  if (mgdl > t.bgHigh) return LEVELS[4];
  if (mgdl > t.bgTargetTop) return LEVELS[3];
  return LEVELS[2];
}

const latest = (treatments, field) => treatments.find((t) => t[field] > 0);
const sum = (treatments, field) => round(treatments.reduce((total, t) => total + (t[field] || 0), 0));

function current(props, status, treatments, now = Date.now()) {
  const mgdl = props.bgnow?.last;
  const read = props.bgnow?.mills;
  const midnight = new Date(now).setHours(0, 0, 0, 0);
  const today = treatments.filter((t) => Date.parse(t.created_at) >= midnight);
  const carbs = latest(treatments, 'carbs');
  const insulin = latest(treatments, 'insulin');
  const sensor = props.sage?.[props.sage?.min];
  const forecast = props.ar2?.forecast?.predicted?.[5]?.mgdl;
  return {
    glucose: scale(mgdl),
    glucose_mgdl: mgdl ?? null,
    delta: mmol ? round(props.delta?.scaled ?? null) : props.delta?.mgdl ?? null,
    trend: props.direction?.label ?? null,
    direction: props.direction?.value ?? null,
    device: props.bgnow?.sgvs?.[0]?.device ?? null,
    last_reading: iso(read),
    level: level(mgdl, status.settings.thresholds),
    stale: !read || now - read > STALE_MS ? 'ON' : 'OFF',
    forecast_30m: scale(forecast),
    iob: round(props.iob?.iob ?? null, 2),
    cob: round(props.cob?.cob ?? null),
    last_carbs: carbs?.carbs ?? null,
    last_carbs_time: carbs?.created_at ?? null,
    last_insulin: insulin?.insulin ?? null,
    last_insulin_time: insulin?.created_at ?? null,
    carbs_today: sum(today, 'carbs'),
    insulin_today: sum(today, 'insulin'),
    sensor_started: sensor?.found ? iso(sensor.treatmentDate) : null,
    site_changed: props.cage?.found ? iso(props.cage.treatmentDate) : null,
    insulin_changed: props.iage?.found ? iso(props.iage.treatmentDate) : null,
    pump_battery_changed: props.bage?.found ? iso(props.bage.treatmentDate) : null,
    uploader_battery: Number.parseInt(props.upbat?.display, 10) || null,
    database_mb: round(props.dbsize?.details?.dataSize ?? null),
    version: status.version,
  };
}

function windowStats(readings, t) {
  if (!readings.length) return {in_range: null, below: null, above: null, average: null, cv: null, gmi: null};
  const values = readings.map((r) => r.sgv);
  const share = (count) => round((count * 100) / values.length);
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length;
  const below = values.filter((v) => v < t.bgTargetBottom).length;
  const above = values.filter((v) => v > t.bgTargetTop).length;
  return {
    in_range: share(values.length - below - above),
    below: share(below),
    above: share(above),
    average: scale(mean),
    cv: round((Math.sqrt(variance) * 100) / mean),
    gmi: round(3.31 + 0.02392 * mean), // Bergenstal et al., Diabetes Care 2018
  };
}

async function statistics(status, now = Date.now()) {
  const since = now - 14 * DAY_MS;
  const readings = await api(`/api/v1/entries/sgv.json?count=30000&find[date][$gte]=${since}`);
  const t = status.settings.thresholds;
  return {
    day: windowStats(readings.filter((r) => r.date >= now - DAY_MS), t),
    fortnight: {...windowStats(readings, t), readings: readings.length},
  };
}

// --- Home Assistant entities ----------------------------------------------

function sensor(key, name, field, extra = {}) {
  return ['sensor', key, {name, state_topic: STATE, value_template: `{{ value_json.${field} }}`, ...extra}];
}

function statSensor(key, name, field, extra = {}) {
  return ['sensor', key, {name, state_topic: STATS, value_template: `{{ value_json.${field} }}`, ...extra}];
}

function entities() {
  const glucose = {device_class: 'blood_glucose_concentration', state_class: 'measurement',
    unit_of_measurement: unit, suggested_display_precision: mmol ? 1 : 0};
  const percent = {state_class: 'measurement', unit_of_measurement: '%', suggested_display_precision: 1};
  const timestamp = {device_class: 'timestamp'};
  const diagnostic = {entity_category: 'diagnostic'};
  const windows = [['24h', 'day', '24h'], ['14d', 'fortnight', '14 days']];
  return [
    sensor('glucose', 'Glucose', 'glucose', {...glucose, icon: 'mdi:water', json_attributes_topic: STATE,
      json_attributes_template: '{{ {"mgdl": value_json.glucose_mgdl, "direction": value_json.direction, "device": value_json.device} | tojson }}'}),
    sensor('delta', 'Delta', 'delta', {...glucose, icon: 'mdi:plus-minus-variant'}),
    sensor('trend', 'Trend', 'trend', {icon: 'mdi:trending-up'}),
    sensor('level', 'Level', 'level', {device_class: 'enum', options: LEVELS, icon: 'mdi:gauge'}),
    sensor('last_reading', 'Last reading', 'last_reading', timestamp),
    sensor('forecast_30m', 'Forecast 30 min', 'forecast_30m', {...glucose, icon: 'mdi:crystal-ball'}),
    ['binary_sensor', 'no_data', {name: 'No recent data', state_topic: STATE, device_class: 'problem',
      value_template: '{{ value_json.stale }}'}],
    sensor('iob', 'Insulin on board', 'iob', {state_class: 'measurement', unit_of_measurement: 'U',
      icon: 'mdi:needle', suggested_display_precision: 2}),
    sensor('cob', 'Carbs on board', 'cob', {state_class: 'measurement', unit_of_measurement: 'g', icon: 'mdi:food-apple'}),
    sensor('last_carbs', 'Last carbs', 'last_carbs', {unit_of_measurement: 'g', icon: 'mdi:food-apple-outline'}),
    sensor('last_carbs_time', 'Last carbs time', 'last_carbs_time', {...timestamp, icon: 'mdi:clock-outline'}),
    sensor('last_insulin', 'Last insulin', 'last_insulin', {unit_of_measurement: 'U', icon: 'mdi:needle'}),
    sensor('last_insulin_time', 'Last insulin time', 'last_insulin_time', {...timestamp, icon: 'mdi:clock-outline'}),
    sensor('carbs_today', 'Carbs today', 'carbs_today', {unit_of_measurement: 'g', icon: 'mdi:food'}),
    sensor('insulin_today', 'Insulin today', 'insulin_today', {unit_of_measurement: 'U', icon: 'mdi:needle'}),
    sensor('sensor_started', 'Sensor started', 'sensor_started', {...timestamp, icon: 'mdi:radio-tower'}),
    sensor('site_changed', 'Site changed', 'site_changed', {...timestamp, icon: 'mdi:bandage'}),
    sensor('insulin_changed', 'Insulin changed', 'insulin_changed', {...timestamp, icon: 'mdi:beaker-outline'}),
    sensor('pump_battery_changed', 'Pump battery changed', 'pump_battery_changed', {...timestamp, icon: 'mdi:battery-sync'}),
    sensor('uploader_battery', 'Uploader battery', 'uploader_battery', {device_class: 'battery',
      state_class: 'measurement', unit_of_measurement: '%'}),
    sensor('database_size', 'Database size', 'database_mb', {...diagnostic, device_class: 'data_size',
      unit_of_measurement: 'MB', state_class: 'measurement'}),
    sensor('version', 'Nightscout version', 'version', {...diagnostic, icon: 'mdi:tag'}),
    ...windows.flatMap(([suffix, field, label]) => [
      statSensor(`time_in_range_${suffix}`, `Time in range ${label}`, `${field}.in_range`, {...percent, icon: 'mdi:target'}),
      statSensor(`time_below_range_${suffix}`, `Time below range ${label}`, `${field}.below`, {...percent, icon: 'mdi:arrow-down-bold'}),
      statSensor(`time_above_range_${suffix}`, `Time above range ${label}`, `${field}.above`, {...percent, icon: 'mdi:arrow-up-bold'}),
      statSensor(`average_glucose_${suffix}`, `Average glucose ${label}`, `${field}.average`, {...glucose, icon: 'mdi:chart-bell-curve'}),
      statSensor(`variability_${suffix}`, `Glucose variability ${label}`, `${field}.cv`, {...percent, icon: 'mdi:sine-wave'}),
    ]),
    statSensor('gmi_14d', 'GMI 14 days', 'fortnight.gmi', {...percent, icon: 'mdi:water-percent'}),
  ];
}

function discovery(version) {
  const device = {identifiers: [BASE], name: 'Nightscout', manufacturer: 'Nightscout',
    model: 'Home Assistant app', sw_version: version};
  if (process.env.BASE_URL) device.configuration_url = process.env.BASE_URL;
  return entities().map(([component, key, payload]) => {
    const uniqueId = `${BASE}_${key}`;
    return [`homeassistant/${component}/${uniqueId}/config`, {
      unique_id: uniqueId, default_entity_id: `${component}.${uniqueId}`, device,
      origin: {name: 'nightscout-ha-app'}, availability_topic: AVAILABILITY, ...payload,
    }];
  });
}

// --- Minimal MQTT 3.1.1 client: QoS 0 publish, subscribe, keepalive, will --

const string = (text) => {
  const body = Buffer.from(text);
  const length = Buffer.alloc(2);
  length.writeUInt16BE(body.length);
  return Buffer.concat([length, body]);
};

function packet(type, ...parts) {
  const body = Buffer.concat(parts);
  const length = [];
  let rest = body.length;
  do {
    const digit = rest % 128;
    rest = Math.floor(rest / 128);
    length.push(rest > 0 ? digit | 128 : digit);
  } while (rest > 0);
  return Buffer.concat([Buffer.from([type, ...length]), body]);
}

function connectPacket({username, password}) {
  const flags = 0x02 | 0x04 | 0x20 | (username ? 0x80 : 0) | (password ? 0x40 : 0); // clean, retained will
  return packet(0x10, string('MQTT'), Buffer.from([4, flags, 0, 60]), string('nightscout-ha-app'),
    string(AVAILABILITY), string('offline'),
    ...(username ? [string(username)] : []), ...(password ? [string(password)] : []));
}

// Splits a byte stream into [type, body] packets, returning the unread remainder.
function parse(buffer, onPacket) {
  while (buffer.length >= 2) {
    let length = 0;
    let multiplier = 1;
    let offset = 1;
    let done = false;
    while (offset < buffer.length && offset <= 4) {
      const byte = buffer[offset++];
      length += (byte & 127) * multiplier;
      multiplier *= 128;
      if (!(byte & 128)) { done = true; break; }
    }
    if (!done || buffer.length < offset + length) return buffer;
    onPacket(buffer[0] >> 4, buffer.subarray(offset, offset + length));
    buffer = buffer.subarray(offset + length);
  }
  return buffer;
}

// --- Main loop ------------------------------------------------------------

let socket = null;
let lastPublished = {};
let online = null;
let announcedVersion = null;
let cachedStats = null;
let statsAt = 0;

function publish(topic, payload) {
  if (socket) socket.write(packet(0x31, string(topic), Buffer.from(payload)));
}

function publishChanged(topic, payload) {
  const text = JSON.stringify(payload);
  if (lastPublished[topic] !== text) publish(topic, text);
  lastPublished[topic] = text;
}

function setOnline(value) {
  if (online !== value) log(value ? 'Nightscout is reachable' : 'Nightscout is not reachable, entities are unavailable');
  online = value;
  publish(AVAILABILITY, value ? 'online' : 'offline');
}

function announce(version) {
  for (const [topic, payload] of discovery(version)) publish(topic, JSON.stringify(payload));
  announcedVersion = version;
}

async function poll() {
  if (!socket) return;
  try {
    const now = Date.now();
    const [props, status, treatments] = await Promise.all([
      api('/api/v2/properties'),
      api('/api/v1/status.json'),
      api(`/api/v1/treatments.json?count=1000&find[created_at][$gte]=${iso(now - TREATMENT_DAYS * DAY_MS)}`),
    ]);
    if (now - statsAt >= STATS_MS) {
      cachedStats = await statistics(status, now);
      statsAt = now;
    }
    if (status.version !== announcedVersion) announce(status.version);
    publishChanged(STATE, current(props, status, treatments, now));
    publishChanged(STATS, cachedStats);
    setOnline(true);
  } catch (error) {
    if (online !== false) log(`Poll failed: ${error.message}`);
    setOnline(false);
  }
}

async function broker() {
  if (process.env.MQTT_HOST) {
    return {host: process.env.MQTT_HOST, port: Number(process.env.MQTT_PORT) || 1883,
      username: process.env.MQTT_USERNAME, password: process.env.MQTT_PASSWORD};
  }
  if (!process.env.SUPERVISOR_TOKEN) return null;
  try {
    const response = await fetch('http://supervisor/services/mqtt', {
      headers: {Authorization: `Bearer ${process.env.SUPERVISOR_TOKEN}`}, signal: AbortSignal.timeout(10000)});
    return response.ok ? (await response.json()).data : null;
  } catch {
    return null;
  }
}

async function connect() {
  const config = await broker();
  if (!config) {
    log('No MQTT broker found. Install the Mosquitto broker app to get Home Assistant entities.');
    setTimeout(connect, NO_BROKER_MS);
    return;
  }

  const options = {host: config.host, port: config.port};
  const connection = config.ssl ? tls.connect({...options, rejectUnauthorized: false}) : net.connect(options);
  let unread = Buffer.alloc(0);
  let lastHeard = Date.now();
  let keepalive = null;

  connection.on(config.ssl ? 'secureConnect' : 'connect', () => connection.write(connectPacket(config)));
  connection.on('data', (chunk) => {
    lastHeard = Date.now();
    unread = parse(Buffer.concat([unread, chunk]), (type, body) => {
      if (type === 2) { // CONNACK
        if (body[1] !== 0) {
          log(`MQTT broker refused the connection (code ${body[1]})`);
          connection.destroy();
          return;
        }
        log(`Connected to MQTT broker ${config.host}:${config.port}`);
        socket = connection;
        lastPublished = {};
        announcedVersion = null;
        connection.write(packet(0x82, Buffer.from([0, 1]), string('homeassistant/status'), Buffer.from([0])));
        poll();
      } else if (type === 3 && body.subarray(2 + body.readUInt16BE(0)).toString() === 'online') {
        // Home Assistant restarted: resend everything.
        lastPublished = {};
        announcedVersion = null;
        poll();
      }
    });
  });
  keepalive = setInterval(() => {
    if (Date.now() - lastHeard > 90 * 1000) connection.destroy();
    else connection.write(Buffer.from([0xc0, 0]));
  }, 30 * 1000);
  connection.on('error', (error) => log(`MQTT connection error: ${error.message}`));
  connection.on('close', () => {
    clearInterval(keepalive);
    if (socket === connection) socket = null;
    online = null;
    setTimeout(connect, RECONNECT_MS);
  });
}

if (require.main === module) {
  connect();
  setInterval(poll, POLL_MS);
}

module.exports = {connectPacket, current, discovery, level, packet, parse, string, windowStats};
