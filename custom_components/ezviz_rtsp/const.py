"""Constants for the ezviz integration."""

DOMAIN = "ezviz_rtsp"
MANUFACTURER = "EZVIZ"

# Configuration
ATTR_SERIAL = "serial"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"
ATTR_TYPE_CLOUD = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA = "CAMERA_ACCOUNT"
CONF_SESSION_ID = "session_id"
CONF_RFSESSION_ID = "rf_session_id"
CONF_EZVIZ_ACCOUNT = "ezviz_account"
CONF_MFA = "mfa_code"  # only kept long enough to finish a login

# Cloud streaming (cameras with no local RTSP: battery cameras and peepholes)
CONF_CODES = "codes"  # verification code per serial, for encrypted video
CONF_SERIAL = "serial"
CONF_CODE = "code"
CONF_TRANSCODE = "transcode"  # re-encode the live stream to H.264 for the browser
CONF_STREAM_TOKEN = "stream_token"  # guards the live address other apps read
DATA_LIVE_VIEW = f"{DOMAIN}_live_view_registered"
DATA_CARD = f"{DOMAIN}_card_registered"
DATA_WS = f"{DOMAIN}_ws_registered"

# The Lovelace card the integration ships and serves.
CARD_FILENAME = "ezviz-cloud-live-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"

# Services data
DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
ATTR_ENABLE = "enable"
ATTR_DIRECTION = "direction"
ATTR_SPEED = "speed"
ATTR_LEVEL = "level"
ATTR_TYPE = "type_value"

# Service names
SERVICE_WAKE_DEVICE = "wake_device"
SERVICE_DETECTION_SENSITIVITY = "set_alarm_detection_sensibility"

# Defaults
EU_URL = "apiieu.ezvizlife.com"
RUSSIA_URL = "apirus.ezvizru.com"
DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_TIMEOUT = 25
DEFAULT_FFMPEG_ARGUMENTS = "/Streaming/Channels/102"
