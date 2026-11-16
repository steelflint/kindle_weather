#!/usr/bin/env sh

# Export environment variables here
export WIFI_TEST_IP=${WIFI_TEST_IP:-1.1.1.1}

# Refresh on the hour and half hour, 06:00 to 23:30, every day.
export REFRESH_SCHEDULE=${REFRESH_SCHEDULE:-"0,30 6-23 * * *"}
export TIMEZONE=${TIMEZONE:-"Europe/London"}

# Partial e-ink updates avoid the black flash but leave ghosting; do a full
# refresh every 4th update to clean the screen.
export FULL_DISPLAY_REFRESH_RATE=${FULL_DISPLAY_REFRESH_RATE:-4}

# kindle-dash shows a "sleeping" screen when the next wakeup is at least this
# many seconds away. The overnight gap here is 6.5 hours, so this keeps the
# last forecast on screen overnight instead. Set it back to 3600 if you'd
# rather see the sleeping screen.
export SLEEP_SCREEN_INTERVAL=36000

export LOW_BATTERY_REPORTING=${LOW_BATTERY_REPORTING:-false}
export LOW_BATTERY_THRESHOLD_PERCENT=10
