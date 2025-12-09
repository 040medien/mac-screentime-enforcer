# mac-screentime-enforcer
A lightweight, locally running agent that: 1. Tracks whether the child session is actively used. 2. Publishes usage statistics and heartbeat data to Home Assistant via MQTT. 3. Subscribes to a retained `screen/&lt;child>/allowed` topic and enforces locks/logouts when Home Assistant denies access (budget exceeded, schedule blocks, etc.).
