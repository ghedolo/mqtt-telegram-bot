#!/usr/bin/env python3
"""Watch the raw MQTT traffic for configured Fields, and say what the bot would
make of each message.

The gap this closes: `MqttClient._on_message` drops a Field silently when the
payload parses but the Field is absent from it (`KeyError`/`TypeError` →
`continue`, deliberately quiet because intermittent Fields are normal). The
symptom is a meter that publishes on the broker while the bot stores nothing,
raises no OFFLINE (the topic itself was received, so the device looks alive) and
holds any blackout in silence. Nothing in the logs says so.

This subscribes to the same topics with the same credentials, prints every
message as it arrives, and for each Field on that topic shows the value the bot
would extract — or exactly why it would extract none.

Subscribe-only: it publishes nothing and writes nothing. It does open a second
MQTT session with the deploy's credentials, so use a client id the broker will
not confuse with the bot's (the default below is unique per run).

Run inside the deploy (cwd = /app, so sensors.d/ and credentials.yaml are there):

  docker compose exec -T bot python3 - --seconds 60 < tools/mqtt_probe.py
  docker compose exec -T bot python3 - --seconds 60 'SM1_CDZ*' < tools/mqtt_probe.py
  docker compose exec -T bot python3 - --all --seconds 30 < tools/mqtt_probe.py

Default scope is every Field a blackout group watches, plus the stored Fields of
their Devices — the set involved in a missing blackout message.
"""
import argparse
import fnmatch
import json
import os
import ssl
import sys
import time

import paho.mqtt.client as mqtt

from bot.config import load
from bot.mqtt_client import _coerce


def explain(sc, payload: str) -> str:
    """What the bot's dispatch loop would do with this payload for this Field —
    the same order of tests as `MqttClient._on_message`, with the silent branch
    spelled out."""
    if sc.json_path:
        try:
            node = json.loads(payload)
        except Exception:
            return "SKIPPED — payload is not JSON, but the field declares a json_path"
        for key in sc.json_path.split("."):
            if not isinstance(node, dict):
                return (f"SKIPPED SILENTLY — json_path {sc.json_path!r} expects an object, "
                        f"but the payload is a bare {type(node).__name__} ({node!r})")
            if key not in node:
                return (f"SKIPPED SILENTLY — json_path {sc.json_path!r}: no {key!r} here "
                        f"(keys present: {list(node)})")
            node = node[key]
    else:
        node = payload
    try:
        return f"value {_coerce(sc, node)}"
    except Exception:
        return f"SKIPPED — {node!r} is not a number and matches no `states` label"


def pick(cfg, patterns, take_all) -> dict[str, list]:
    """topic → [field config], for the Fields to watch."""
    chosen = []
    if take_all:
        chosen = [*cfg.sensors.values(), *cfg.signals.values()]
    elif patterns:
        for sc in (*cfg.sensors.values(), *cfg.signals.values()):
            if any(fnmatch.fnmatch(sc.name, p) for p in patterns):
                chosen.append(sc)
    else:
        names = set()
        for grp in cfg.blackouts.values():
            for name in grp.fields:
                names.add(name)
                dev = cfg.devices.get(cfg.device_of(name))
                if dev is not None:
                    names |= {osc.name for osc in dev.fields.values()}
        chosen = [sc for sc in (*cfg.sensors.values(), *cfg.signals.values())
                  if sc.name in names]
    topics: dict[str, list] = {}
    for sc in chosen:
        topics.setdefault(sc.topic, []).append(sc)
    return topics


def main():
    ap = argparse.ArgumentParser(
        description="Watch raw MQTT payloads and show what the bot would parse (subscribe-only)")
    ap.add_argument("patterns", nargs="*", help="field names or globs. Quote them: 'SM1_CDZ*'")
    ap.add_argument("--seconds", type=int, default=60, help="how long to listen (default 60)")
    ap.add_argument("--all", action="store_true", help="watch every configured field")
    args = ap.parse_args()

    cfg = load("sensors.d", "credentials.yaml")
    topics = pick(cfg, args.patterns, args.all)
    if not topics:
        sys.exit("No field matched — nothing to subscribe to")

    print(f"Fields watched ({sum(len(v) for v in topics.values())} on {len(topics)} topic(s)):")
    for topic, scs in sorted(topics.items()):
        for sc in scs:
            kind = "signal" if sc.name in cfg.signals else "sensor"
            print(f"  {sc.name:24s} {kind:6s} topic={topic}  json_path={sc.json_path or '-'}")
    print(f"\nListening {args.seconds}s on {cfg.mqtt_host}:{cfg.mqtt_port} …")
    print("(silence below means the bot is not receiving these topics either)\n")

    seen: dict[str, int] = {}

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"!! connect failed: {reason_code}")
            return
        for topic in topics:
            res, _mid = client.subscribe(topic)
            if res != mqtt.MQTT_ERR_SUCCESS:
                print(f"!! subscribe to {topic} refused: {res}")

    def on_message(client, userdata, msg):
        try:
            payload = msg.payload.decode()
        except Exception:
            print(f"{time.strftime('%H:%M:%S')}  {msg.topic}  <undecodable {len(msg.payload)} bytes>")
            return
        seen[msg.topic] = seen.get(msg.topic, 0) + 1
        print(f"{time.strftime('%H:%M:%S')}  {msg.topic}")
        print(f"    payload: {payload[:400]}")
        for sc in topics[msg.topic]:
            print(f"    -> {sc.name}: {explain(sc, payload)}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"mqtt_probe_{os.getpid()}")
    if cfg.mqtt_username:
        client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
    if cfg.mqtt_tls:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
    client.loop_start()
    try:
        time.sleep(args.seconds)
    finally:
        client.loop_stop()
        client.disconnect()

    print("\nMessages per topic:")
    for topic in sorted(topics):
        n = seen.get(topic, 0)
        print(f"  {topic:40s} {n:5d}" + ("   <- NOTHING ARRIVED" if n == 0 else ""))


if __name__ == "__main__":
    main()
