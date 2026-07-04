"""Constants for the HomeTo64 integration."""

DOMAIN = "hometo64"

# Config / options keys
CONF_HOST          = "host"
CONF_PASSWORD      = "password"
CONF_FILENAME      = "filename"
CONF_HEADING       = "heading"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_STOP_MIN = "auto_stop_minutes"   # stop writemem after N minutes (0 = never)

# Sensor list — stored as a list of dicts:
#   [{"entity_id": "sensor.living_room_temp", "display_name": "Living Room"}, ...]
# display_name defaults to the entity's friendly name but can be overridden by the user.
CONF_SENSORS      = "sensors"
CONF_ENTITY_ID    = "entity_id"
CONF_DISPLAY_NAME = "display_name"

# Ultimate REST API — firmware 3.11+
ULTIMATE_API_BASE        = "http://{host}/v1"
ULTIMATE_HEADER_PASSWORD = "X-Password"

API_INFO     = "/info"
API_RUN_PRG  = "/runners:run_prg"
API_LOAD_PRG = "/runners:load_prg"

# Defaults
DEFAULT_PASSWORD      = ""
DEFAULT_FILENAME      = "HOMETO64.PRG"
DEFAULT_HEADING       = "-- HOME ASSISTANT DASHBOARD --"
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_AUTO_STOP_MIN = 120   # stop RAM writes after 2 hours by default (0 = never)
DEFAULT_SENSORS: list = []

# www/ subdirectory for static file serving
WWW_SUBDIR = "hometo64"

# C64 BASIC tokens
C64_TOKEN_PRINT  = 0x99
C64_TOKEN_GOTO   = 0x89
C64_TOKEN_GOSUB  = 0x8D
C64_TOKEN_RETURN = 0x8E
C64_TOKEN_FOR    = 0x81
C64_TOKEN_TO     = 0xA4
C64_TOKEN_NEXT   = 0x82
C64_TOKEN_REM    = 0x8F
C64_TOKEN_END    = 0x80
C64_TOKEN_POKE   = 0x97
C64_TOKEN_PEEK   = 0xC2
C64_TOKEN_GET    = 0xA1
C64_TOKEN_IF     = 0x8B
C64_TOKEN_THEN   = 0xA7
C64_TOKEN_INT    = 0xB5

# PETSCII control codes
PETSCII_CLEAR       = 0x93
PETSCII_REVERSE_ON  = 0x12
PETSCII_REVERSE_OFF = 0x92

C64_BASIC_LOAD_ADDRESS = 0x0801
C64_COLS               = 40
CONF_PAGES         = "pages"           # list of page configs
CONF_PAGE_HEADING  = "page_heading"    # heading for a single page
CONF_NUM_PAGES     = "num_pages"       # how many pages (1-3)
DEFAULT_NUM_PAGES  = 1
MAX_PAGES          = 10

# Scratch RAM control addresses
SCRATCH_ACTION_ADDR = 0xCFFC   # C64 writes slot+1 here; HA reads, calls service, writes 0
SCRATCH_BITMASK_ADDR = 0xCFFD  # HA writes controllable bitmask for current page
SCRATCH_CURSOR_ADDR  = 0xCFFE  # C64 tracks selected slot index here
# NAV_ADDR = 0xCFFF (defined in c64_prg.py)

# Entity domains that support toggle/control from the C64 dashboard
TOGGLEABLE_DOMAINS = frozenset({
    "light", "switch", "input_boolean", "fan",
    "cover", "lock", "scene", "automation", "group",
})

# Optimistic display values shown immediately on toggle (before HA confirms)
OPTIMISTIC_TOGGLE = {
    "on":       "Off",
    "off":      "On",
    "open":     "Closing",
    "closed":   "Opening",
    "locked":   "Unlockd",
    "unlocked": "Locked",
    "active":   "Idle",
    "idle":     "Active",
}

# Auto-cycle pages feature
CONF_AUTO_CYCLE    = "auto_cycle_seconds"   # 0 = off
DEFAULT_AUTO_CYCLE = 0                       # off by default
AUTO_CYCLE_ADDR    = 53243                   # $CFCB — HA writes jiffy interval, BASIC reads
