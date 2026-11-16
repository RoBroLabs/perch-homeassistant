"""Constants for the Perch for Home Assistant companion."""

from datetime import timedelta

DOMAIN = "perch"

# Domain-separation labels. Changing any of these breaks interop with the TV app.
BIND_LABEL = b"perch-bind-v1"
CRED_LABEL = b"perch-cred-v1"
FP_LABEL = b"perch-fp-v1"
AUTH_LABEL = b"perch-auth-v2"

PROTOCOL_VERSION = 1
COMPANION_VERSION = "0.1.0"
AUTH_TIMEOUT = 10
HEARTBEAT = 30
CLOSE_AUTH_INVALID = 4001
CLOSE_REVOKED = 4003

PAIRING_TTL = 300

# Subentry and its data keys.
SUBENTRY_TV = "tv"
CONF_TV_ID = "tv_id"
CONF_PUBLIC_KEY = "public_key"
CONF_CREDENTIAL_HASH = "credential_hash"

# Options.
CONF_PAIRING_ATTEMPTS = "pairing_attempts"
DEFAULT_PAIRING_ATTEMPTS = 5
MIN_PAIRING_ATTEMPTS = 3
MAX_PAIRING_ATTEMPTS = 10
CONF_RESTORE_TIMEOUT = "restore_timeout"
DEFAULT_RESTORE_TIMEOUT = 15
MIN_RESTORE_TIMEOUT = 5
MAX_RESTORE_TIMEOUT = 60
CONF_ALLOW_CLOUD = "allow_cloud"
DEFAULT_ALLOW_CLOUD = False

CONF_CAMERAS = "cameras"
CONF_DOORBELL = "doorbell"
CONF_SCENES = "scenes"
CONF_ADVANCED = "advanced"

# Per-TV subentry data (set by the reconfigure step).
CONF_ROOM_ID = "room_id"
CONF_ROOMS = "rooms"

# Options picked in the second options step, from the first step's choices.
CONF_DOORBELL_CAMERA = "doorbell_camera"
CONF_MOVIE_SCENE = "movie_night_scene"
CONF_PAUSED_SCENE = "paused_scene"

# Moods: default levels, as a percentage of full brightness.
MOOD_FILM_PCT = 10
MOOD_EPISODE_PCT = 35
MOOD_PAUSED_PCT = 50
# Hand-change detection: brightness slack (about 2 % of 255) and the seconds after
# a Perch service call in which changes from other contexts are not hand changes.
HAND_CHANGE_TOLERANCE = 5
HAND_CHANGE_GRACE = 10
STORE_KEY = "perch.moods"
STORE_VERSION = 1
# The longest a skip-tonight may reach ahead.
SKIP_MAX_HOURS = 24

# Frigate events, through the official Frigate integration.
FRIGATE_DOMAIN = "frigate"
FRIGATE_TESTED_VERSIONS = ("5.15.6",)
FRIGATE_SUPPORTED_MAJOR = 5
MEDIA_LIFETIME = timedelta(minutes=5)
