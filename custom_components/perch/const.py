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

# Tell my phone (options section `phone`).
CONF_PHONE = "phone"
CONF_NOTIFY_SERVICES = "notify_services"
CONF_NOTIFY_REQUEST_READY = "notify_request_ready"
CONF_NOTIFY_INCLUDE_TITLE = "notify_include_title"
# notify.send_message needs a notify entity; Perch sends only to legacy services.
NOTIFY_EXCLUDED = ("send_message",)
NOTIFY_TIMEOUT = 10  # seconds per service call
NOTIFY_PER_TV_HOUR = 10  # request notices a TV may trigger in an hour
NOTIFY_OVERALL_HOUR = 30  # request notices in an hour, all TVs
NOTIFY_REMEMBER = 500  # titles remembered for "once per title"
NOTIFY_STORE_KEY = "perch.notify"

# Let someone in (options section `let_in`; the TV allow flag is per subentry).
CONF_LET_IN = "let_in"
CONF_LET_IN_ENABLED = "let_in_enabled"
CONF_LET_IN_SCRIPT = "let_in_script"
CONF_LET_IN_PIN = "let_in_pin"  # form field only, never stored
CONF_LET_IN_PIN_CONFIRM = "let_in_pin_confirm"  # form field only, never stored
CONF_LET_IN_PIN_HASH = "let_in_pin_hash"
CONF_LET_IN_ALLOWED = "let_in_allowed"
LET_IN_LABEL = b"perch-letin-v1"
LET_IN_PIN_MIN = 6
LET_IN_PIN_MAX = 12
LET_IN_PLAIN_LEN = 16  # the PIN is padded to this many bytes before sealing
LET_IN_SEALED_LEN = 32 + 12 + LET_IN_PLAIN_LEN + 16
RING_WINDOW = timedelta(minutes=2)
LET_IN_TRIES = 3  # wrong PINs before a lockout
LET_IN_LOCKOUTS = (timedelta(minutes=15), timedelta(hours=1))  # then disabled
LET_IN_SCRIPT_TIMEOUT = 10  # seconds to start the script
LET_IN_STORE_KEY = "perch.let_in"
