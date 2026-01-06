# TAIPOWER HVCS Grabber

This script logs into the TAIPOWER HVCS site, reads the dashboard, and publishes
two fields to MQTT:

- Daily max demand time
- Monthly max demand time

The MQTT message format is:

```
當日最高需量時間:YYYY/MM/DD HH:MM@當月最高需量時間:YYYY/MM/DD HH:MM
```

Retention is enabled (`retain=True`) on publish.

## Configuration

All settings are stored in `config.json` in the same directory as `main.py`.
Fill in at least the login and MQTT values.

Key fields:

- `TAIPOWER_USERNAME`
- `TAIPOWER_PASSWORD`
- `TAIPOWER_METER_NO` (optional)
- `MQTT_HOST`
- `MQTT_PORT`
- `MQTT_TOPIC`
- `MQTT_USERNAME` / `MQTT_PASSWORD` (optional)

## Run

```
python main.py
```

## Windows Task Scheduler

Schedule the script to run daily and set the working directory to the project
folder so it can find `config.json`.

