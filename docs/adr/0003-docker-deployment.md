# ADR-0003: Docker for deployment

## Status
Accepted

## Context
Bot needs to run on a dedicated always-on machine, separate from the MQTT broker. Development happens on macOS where the full MQTT stack is available for testing.

## Decision
Package the bot as a Docker container. Develop and test locally on macOS, then transfer the image to the production machine.

## Consequences
- Portable across machines without environment setup
- Easy local testing with the existing macOS MQTT stack
- Slight overhead vs bare systemd, acceptable for this workload
