"""Constants for the Visionect integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform
from homeassistant.util.signal_type import SignalTypeFormat

from pyvisionect.imaging.constants import MAX_NO_FULL_UPDATE

DOMAIN: Final = "visionect"

DEFAULT_PORT: Final = 11113
DEFAULT_HOST: Final = "0.0.0.0"

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

# --- config entry data / options -------------------------------------------

CONF_PERSIST_PARAMS: Final = "persist_params"

DEFAULT_PERSIST_PARAMS: Final = True

CONF_TLS_CERTFILE: Final = "tls_certfile"
CONF_TLS_KEYFILE: Final = "tls_keyfile"

# --- partial (screen-space) updates ----------------------------------------

CONF_PARTIAL_UPDATES: Final = "partial_updates"
"""Option key: ``{uuid: bool}``. Per sign, and absent means off."""

DEFAULT_PARTIAL_UPDATES: Final = False

CONF_PARTIAL_MAX_CONSECUTIVE: Final = "partial_max_consecutive"
"""Option key: how many partials may go out before a full-screen push."""

DEFAULT_PARTIAL_MAX_CONSECUTIVE: Final = MAX_NO_FULL_UPDATE
"""The vendor's ``noFullUpdateMax``, which is 10 (``client.go:217``).

This is the **ghosting budget**, and it is not a precaution. Measured on this
hardware across eight accepted partial pushes and twelve rectangles, the
firmware initiated no clearing refresh of its own: every partial logged
``wfn: 2, inv: 0`` into exactly one ``UPD_FULL_AREA`` and nothing else
(pyvisionect ``OPEN-QUESTIONS.md`` A10/A12). A full-screen push is the only
thing that asks for the inverse clearing waveform, so a forced full push every
N partials is the only thing that clears the panel. A device left to run
partials forever turns to mush.

Note *asks*: the same measurements caught the firmware granting the requested
inverse refresh on some full pushes and silently declining it on others, with
byte-identical headers. That is a reason to keep this conservative, not to
raise it.
"""

PARTIAL_MAX_CONSECUTIVE_LIMIT: Final = 60
"""The largest budget the options flow will accept.

Deliberately finite. ``PartialPolicy`` takes a negative number to mean "never
force a refresh", and offering that through the UI would be offering a setting
whose consequence is permanent ghosting on a panel nobody can replace.
"""

# Changing one of these needs the entry reloaded, because it decides how the
# socket is bound. Everything else -- which signs use partial updates, the
# ghosting budget, whether parameter writes persist -- is read live, and
# reloading for it would drop the listener. That matters more than it sounds:
# a sign whose socket is closed re-dials on its own schedule, which on this
# firmware can be an hour away.
OPTIONS_NEEDING_RELOAD: Final = frozenset({CONF_TLS_CERTFILE, CONF_TLS_KEYFILE})

# Both default to unset, and that is not timidity. The device side of TLS is
# TCLV parameter 145, and on the firmware this integration was developed
# against (7.4.4407) the device answers a read of 145 with a read error -- the
# same answer it gives for a parameter id that does not exist. So turning TLS
# on here buys nothing on that firmware, while a certificate configured by
# accident costs nothing either: the listener only wraps a connection that
# actually opens with a ClientHello.
#
# The hazard runs the other way. A sign whose 145 *is* set to 1, pointed at a
# listener with no certificate, cannot be reached over the network at all and
# has to be recovered over USB serial. So the order is always certificate
# first, parameter second -- and the config flow validates the certificate
# before accepting it, rather than finding out at bind time.

# There is deliberately no "only allow known signs" option. The protocol has no
# authentication of any kind -- the UUID in a device's first packet is the
# entire identity claim -- so a toggle with that name would imply a control the
# integration cannot actually provide. Network isolation is the real control,
# and the README says so rather than dressing it up as a setting.

# --- dispatcher signals -----------------------------------------------------

# Suffixed with the entry id so two entries could never cross-talk, even
# though single_config_entry forbids a second one today.
SIGNAL_DEVICE_ADDED: Final = SignalTypeFormat[str]("visionect_device_added_{}")
SIGNAL_DEVICE_REMOVED: Final = SignalTypeFormat[str]("visionect_device_removed_{}")
SIGNAL_LISTENER_STATE: Final = SignalTypeFormat[str]("visionect_listener_{}")
SIGNAL_SCREEN_UPDATED: Final = SignalTypeFormat[str]("visionect_screen_{}")
SIGNAL_DEVICE_FILE_READ: Final = SignalTypeFormat[str]("visionect_device_file_{}")

# --- events -----------------------------------------------------------------

EVENT_DEVICE_CONNECTED: Final = f"{DOMAIN}_device_connected"
EVENT_PUSH_COMPLETED: Final = f"{DOMAIN}_push_completed"
EVENT_PUSH_FAILED: Final = f"{DOMAIN}_push_failed"
EVENT_COMMAND_NACKED: Final = f"{DOMAIN}_command_nacked"
EVENT_FILES_LISTED: Final = f"{DOMAIN}_files_listed"
EVENT_FILE_READ: Final = f"{DOMAIN}_file_read"
EVENT_TOUCH: Final = f"{DOMAIN}_touch"
EVENT_BUTTON: Final = f"{DOMAIN}_button"

# --- storage ----------------------------------------------------------------

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.runtime"
FRAME_DIR: Final = DOMAIN

# --- protocol ---------------------------------------------------------------

TCLV_HEARTBEAT: Final = 29

# --- timing -----------------------------------------------------------------

# Staleness only; never an availability input (see README / design section 1.4).
OVERDUE_GRACE: Final = timedelta(minutes=5)

# How long the listener may sit bound with nothing connecting before we say so.
NO_DEVICE_GRACE: Final = timedelta(minutes=10)

ISSUE_NO_DEVICE: Final = "no_device_connected"

# --- device files -----------------------------------------------------------

# The device answers a file read 1024 bytes at a time at about 2.3 KiB/s and
# offers no seek, so its stored frames take one to nine minutes and a lost
# reply restarts the transfer. These are the defaults the service exposes.
DEVICE_FILE_TIMEOUT: Final = 30.0
DEVICE_FILE_ATTEMPTS: Final = 3

# --- imaging ----------------------------------------------------------------

DITHER_MODES: Final = {
    "none": 1,
    "bayer": 2,
    "floyd_steinberg": 3,
    "blue_noise": 4,
}
ENCODINGS: Final = {"1_bit": 1, "4_bit": 4}
FIT_MODES: Final = ["fit", "stretch", "crop"]

DEFAULT_DITHER: Final = "blue_noise"
DEFAULT_ENCODING: Final = "4_bit"
DEFAULT_FIT: Final = "fit"
DEFAULT_BACKGROUND: Final = "#FFFFFF"
