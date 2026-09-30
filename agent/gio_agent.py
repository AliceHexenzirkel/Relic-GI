#!/usr/bin/env python3
"""
GIO agent -- a small, dependency-free control service for the game-server box.

Runs the same file on Linux (RHEL/Debian families; stdlib Python 3.9+, bash and the docker
compose v2 plugin are the only dependencies) and on Windows (Docker Desktop; Python 3.10+).
It is the ONLY thing the Relic launcher talks to on a server box. It:
  * reports, per game version, whether the stack EXISTS, is BOOTSTRAPPED, is PROVISIONED and is UP,
  * bootstraps a stack the first time (the vendor bootstrap.sh on Linux; the same eight compose
    steps run from Python on Windows, where the vendor bootstrap.bat ends with `pause`),
  * provisions it: drops in the pre-GAA player save, makes the events last practically forever,
    keeps the Spiral Abyss calendar covering today, re-points the advertised IP at the WAN address
    and imports the progress-template databases players can start from,
  * starts/stops a version's stack (only one may run at a time -- ports + subnet conflict),
  * proxies GM commands to muipserver, signing them the same way the MUIP tool does,
  * copies account progress between players (guid rewrite, see copy_save),
  * creates player accounts for the admin (job `accountcreate`: any template imported on the
    stack, no signup policy and no quota -- the bearer token is the authorisation),
  * serves the token-less /public/* endpoints the player mode uses (status, self-service account
    creation from a template, GM commands) -- rate-limited and governed by the admin policy (signup
    and player commands are on by default, the admin may turn them off; every progress template is
    offered by default, and one launcher may create up to maxPerPlayer accounts per version, 5 by
    default),
  * rotates the stack's secrets and toggles in-game password verification,
  * mirrors the official 2021 in-version hotfixes (the "hotpatch" outputs of miHoYo's CDN) into a
    folder on the box -- first as one ready-made archive.org bundle (md5-pinned, unpacked in a
    staging folder, only manifest-verified files enter the mirror), then file by file from the
    official CDN for whatever is left -- serves them token-less under /hotpatch/<path> in the CDN
    layout and makes the dispatch advertise them (server/res/PC_version.txt +
    server/data/version.txt + the four t_region_config URL columns), so every client hotpatches
    itself at login exactly like in 2021 -- plus, per voice language the admin picks, the
    BASE-build voice packs a client that lacks them downloads at that same login (a Japanese /
    Korean / Chinese Windows selects that voice),
  * downloads the ready-made vendor server stack itself from the Internet Archive when the box has
    none yet (job `fetch`: resumable, size + md5 verified, extracted with 7zz/7z/bsdtar or 7-Zip /
    tar.exe, the top folder renamed onto GIO_DIR_xx, the 2.8 nested server/data layout healed),
  * verifies every HTTPS download fully (chain + host name) but without the RFC 5280 pedantry Python
    3.13 turned on (VERIFY_X509_STRICT rejects the legacy root a Windows store may end archive.org's
    chain at); GIO_CA_FILE adds roots, and a download whose md5 + size are pinned beforehand may
    repeat over an unverified connection when its certificate cannot be verified
    (GIO_TLS_PINNED_FALLBACK) -- the md5 is what proves the file then,
  * lets the admin edit an allowlist of its own settings from the launcher (GET/POST /agent/config:
    written back into its KEY=VALUE config file -- comments, unknown keys and order kept --, applied
    at once where the code reads them live, the rest flagged for a restart) and restart itself
    (POST /agent/restart: under systemd it exits and Restart=always brings it back with the re-read
    EnvironmentFile; elsewhere it starts a fresh copy of itself with its boot environment),
  * changes a version's stack folder (job `relocate`, POST /server/relocate): the stack is brought
    down under its current project name, then MOVED (a rename, or a verified copy to another drive)
    or the version only RE-POINTED at a folder that holds a stack / none yet, GIO_DIR_xx written
    into the config file and the stack started again when it was running.

Anything slower than a couple of seconds runs as a JOB: the POST returns {"job": "<id>"} at once
and the client polls GET /jobs/<id>?since=N for the streamed log. One job at a time (only the
download phases of `hotpatch-voice` and `fetch` let the watchdogs' jobs and player signups start
beside them).

Security: every admin request needs the bearer token (GIO_AGENT_TOKEN); /health, /public/* and
the static /hotpatch/<path> mirror never do. Two deployment shapes:
  * direct (default for the Relic app): GIO_AGENT_LISTEN=0.0.0.0:18080 -- the app talks straight
    to the agent over the network; the token (baked into the app build) is the only credential.
    Open the port in whichever firewall the box runs (install_agent.sh does this automatically).
  * tunnel (advanced): GIO_AGENT_LISTEN=127.0.0.1:18080 -- reachable only via an SSH local-forward.
The MUIP sign key is read live from muipserver.xml and never leaves the server.

Configuration: environment variables (systemd EnvironmentFile) and/or `--config FILE` (KEY=VALUE
lines; the environment wins). The agent writes that file (--config, else /etc/gio-agent/config) only
through POST /agent/config. `--log FILE` appends stdout/stderr to a size-rotated file (needed
under pythonw, where stdout is None). See docs/AGENT-API.md for the full contract.

Run:      GIO_AGENT_TOKEN=... python3 gio_agent.py [--config FILE] [--log FILE]
Selftest: python3 gio_agent.py --selftest      (no docker / network needed)
Fetch:    python3 gio_agent.py --config FILE --fetch 1.6 [2.8]   (asks the RUNNING agent to download
          the server package(s) and streams the job log; exit 0 done / 1 a job failed / 2 usage /
          3 agent busy / 4 agent unreachable)
"""
import base64
import email.utils
import errno
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import ssl
except ImportError:  # a Python built without OpenSSL: https fails as it always did, no TLS fallback
    ssl = None

AGENT_VERSION = "3.7"
IS_WINDOWS = os.name == "nt"


# -- CLI options that must be known BEFORE the module-level configuration is evaluated --

def _cli_option(name, argv=None):
    """Value of `--name VALUE` or `--name=VALUE` from argv, else None."""
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def parse_config_text(text):
    """KEY=VALUE lines -> [(key, value)]. '#' comments and blank lines are ignored, a leading BOM
    is tolerated, surrounding quotes are stripped. Pure, so the selftest can feed it strings.
    Lines end at "\\n" only (a trailing "\\r" is stripped with the rest of the whitespace): the way the
    writer below, systemd's EnvironmentFile, install_agent.sh and the launcher split the file.
    str.splitlines() would also break at U+2028 / U+2029 / U+0085 / \\v / \\f and read one physical
    line as two assignments the other readers never see."""
    out = []
    for raw in text.lstrip("\ufeff").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            out.append((k, v))
    return out


def apply_config_file(path, environ=None):
    """Load a KEY=VALUE file into the environment WITHOUT overriding keys already set there (an
    explicit export and systemd's EnvironmentFile keep precedence). Returns the keys applied. A byte
    that is not UTF-8 (a hand edit in another code page) degrades that one value to U+FFFD, and a value
    the environment cannot hold (over 32767 characters on Windows, a NUL) is skipped with a WARNING:
    neither may stop the agent from starting (systemd would crash-loop it, the launcher lose it)."""
    environ = os.environ if environ is None else environ
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        pairs = parse_config_text(f.read())
    applied = []
    for k, v in pairs:
        if k not in environ:
            try:
                environ[k] = v
            except ValueError as e:
                log_line("gio-agent: WARNING: %s in %s cannot be used (%s) -- ignored." % (k, path, e))
                continue
            applied.append(k)
    return applied


class _RotatingLog:
    """A minimal append-only log file with one-step size rotation (file > max -> file.1).
    Installed as sys.stdout/sys.stderr by --log, so every print() lands in it; thread-safe."""

    encoding = "utf-8"

    def __init__(self, path, max_bytes=5 * 1024 * 1024):
        self.path, self.max_bytes = path, max_bytes
        self._lock = threading.Lock()
        self._f = None
        self._open()

    def _open(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8", errors="replace")

    def _rotate(self):
        try:
            self._f.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        self._open()

    def write(self, s):
        with self._lock:
            try:
                self._f.write(s)
                self._f.flush()  # a flush per line is what print(flush=True) did anyway
                if os.fstat(self._f.fileno()).st_size > self.max_bytes:
                    self._rotate()
            except Exception:  # noqa: BLE001 -- logging must never take the agent down
                pass
        return len(s)

    def flush(self):
        try:
            self._f.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        try:
            self._f.close()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        return False


def open_log_file(path):
    log = _RotatingLog(path)
    sys.stdout = log
    sys.stderr = log
    return log


def log_line(msg):
    """print() that tolerates a missing stdout (pythonw) and a closed console pipe."""
    out = sys.stdout
    if out is None:
        return
    try:
        print(msg, file=out, flush=True)
    except (OSError, ValueError):
        pass


def read_config_pairs(path):
    """The KEY=VALUE pairs of a config file (parse_config_text; OSError is the caller's). A byte that is
    not UTF-8 reads as U+FFFD: a stray byte of a hand edit must not stop the start (this runs at import)
    nor turn GET /agent/config into a 500."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return parse_config_text(f.read())


def config_dict(pairs, first_wins):
    """Pairs -> dict. A repeated key: the FIRST line wins for `--config` (apply_config_file never
    overrides a key it already set), the LAST for systemd's EnvironmentFile. Pure."""
    out = {}
    for k, v in pairs:
        if first_wins and k in out:
            continue
        out[k] = v
    return out


LOG_FILE = _cli_option("--log")
if LOG_FILE:
    open_log_file(LOG_FILE)
CONFIG_FILE = _cli_option("--config")
_STARTED = time.time()  # GET /agent/config `started`: the launcher sees a finished restart by a new value
# The environment exactly as this process received it, BEFORE apply_config_file copies the file's values
# into os.environ: (1) a key set here with a value the file does not hold is an explicit override (an
# exported variable; under systemd only another EnvironmentFile= of a drop-in, since the file beats
# Environment= -- env_overridden) that editing the file cannot change, and (2) a self-restart hands THIS,
# minus what the file owned (respawn_env), to the new process -- os.environ would pin every old file value
# over the rewritten file.
_BOOT_ENV = dict(os.environ)
# The file the agent may rewrite (POST /agent/config): --config, else -- not on Windows -- the systemd
# EnvironmentFile the installer writes when it exists (the unit runs the agent without --config).
SYSTEMD_CONFIG = "/etc/gio-agent/config"
if CONFIG_FILE:
    CONFIG_PATH = os.path.abspath(CONFIG_FILE)
elif not IS_WINDOWS and os.path.isfile(SYSTEMD_CONFIG):
    CONFIG_PATH = SYSTEMD_CONFIG
else:
    CONFIG_PATH = None
try:
    _BOOT_FILE = config_dict(read_config_pairs(CONFIG_PATH), first_wins=bool(CONFIG_FILE)) if CONFIG_PATH else {}
except (OSError, ValueError):
    _BOOT_FILE = {}  # --config: apply_config_file below reports it and exits; systemd: nothing is editable
if CONFIG_FILE:
    try:
        apply_config_file(CONFIG_FILE)
    except (OSError, ValueError) as _e:
        log_line("gio-agent: cannot read --config %s: %s" % (CONFIG_FILE, _e))
        sys.exit(2)


def _project_name(path):
    """What `docker compose` will call the project: the directory basename, lowercased, characters
    outside [a-z0-9_-] dropped, leading '_'/'-' trimmed -- compose's own normalization, the one
    that turns "1.6_live" into the "16_live" that `docker compose ls` reports. DERIVED instead of
    hardcoded: the agent compares it against ls output in every up-check and both watchdogs, so a
    stack living under another basename would otherwise silently desync them all."""
    name = re.sub(r"[^a-z0-9_-]", "", os.path.basename(os.path.normpath(path)).lower()).lstrip("_-")
    return name or "default"


def _dir_setting(key, linux_default):
    # Windows has no conventional stack location: an unset dir means "this version is absent".
    return os.environ.get(key, "" if IS_WINDOWS else linux_default).strip()


# Version -> compose project dir; the project name is derived from the dir the same way compose
# derives it. An empty dir means the version is not configured on this box.
VERSIONS = {
    "1.6": {"dir": _dir_setting("GIO_DIR_16", "/home/1.6_live")},
    "2.8": {"dir": _dir_setting("GIO_DIR_28", "/home/2.8_live")},
}
for _meta in VERSIONS.values():
    _meta["project"] = _project_name(_meta["dir"]) if _meta["dir"] else ""
del _meta
REGION = os.environ.get("GIO_AGENT_REGION", "dev_docker")
# Empty = derive it per version from that stack's .env OUTER_IP. muipserver publishes its port on
# OUTER_IP only (docker-compose.yml.tmpl: "%OUTER_IP%:21051:21051"), so 127.0.0.1 does NOT work.
MUIP_HOST = os.environ.get("GIO_MUIP_HOST", "").strip()
# The sign key the agent WRITES into a stack it prepares while that stack still carries the vendor's
# published key (agent 3.4). Empty = a random one, so only this agent (and an admin reading
# <stack>/creds.txt on the box) can sign GM commands. Validated where SECRET_VALUE_RE lives (a WARNING,
# never a refusal to boot); never logged, never returned -- only a fingerprint ever leaves the box.
MUIP_KEY = os.environ.get("GIO_MUIP_KEY", "").strip()
# The sign_key literals the 1.6_live / 2.8_live archives ship in server/muipserver/conf/muipserver.xml.tmpl
# (public knowledge: anyone with the archive can sign commands against a stack that still has one).
VENDOR_MUIP_KEYS = frozenset(("8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ", "9H2UrJ5J4yZJf95FqMkqi628snEmzvyV9oAp"))
TOKEN = os.environ.get("GIO_AGENT_TOKEN", "")
def _advertised_config(env):
    """(host, ip, both_set) from the environment. GIO_ADVERTISED_HOST (a DDNS name the agent follows)
    wins over a static GIO_ADVERTISED_IP when both are set -- main() logs a WARNING, never refuses to
    boot (systemd Restart=always would crash-loop). With a host, the IP starts empty and is filled by
    resolve_advertised_at_boot() / the advertise watchdog."""
    host = (env.get("GIO_ADVERTISED_HOST") or "").strip().rstrip(".")
    ip = (env.get("GIO_ADVERTISED_IP") or "").strip()
    return host, ("" if host else ip), bool(host and ip)


def _parse_check_every(text, default=120, floor=30):
    try:
        return max(floor, int((text or "").strip() or default))
    except ValueError:
        return default


# The two boolean spellings of the GIO_* switches (raw None = the key is absent). A default-ON switch
# is on unless it says 0/false/no/off; a default-OFF one is off unless it says 1/true/yes/on. Shared by
# the import-time globals and the live apply of POST /agent/config, so both read a value alike.
_FALSY = ("0", "false", "no", "off")
_TRUTHY = ("1", "true", "yes", "on")


def _flag_on(raw):
    return (raw if raw is not None else "1").strip().lower() not in _FALSY


def _flag_off(raw):
    return (raw if raw is not None else "0").strip().lower() in _TRUTHY


# IP handed to the CLIENT (dispatch/gateserver/gacha URLs). Decoupled from .env OUTER_IP, which must
# stay a local IP because docker binds to it. Empty = leave the stack advertising its LAN IP.
# With GIO_ADVERTISED_HOST set it is the host's last confirmed IPv4 address, and it changes ONLY
# under _op_lock (a job holding it reads the global a dozen times and must see one value).
ADVERTISED_HOST, ADVERTISED_IP, _ADVERTISED_BOTH_SET = _advertised_config(os.environ)
# GIO_ADVERTISED_IP as this process was started with it (ADVERTISED_IP itself follows a DDNS host):
# what GET /agent/config reports as the running value of that restart-only setting.
_BOOT_ADVERTISED_IP = (os.environ.get("GIO_ADVERTISED_IP") or "").strip()
# Split-horizon / LAN-only setups: also accept a private (RFC 1918) answer for the host.
ADVERTISED_ALLOW_PRIVATE = _flag_off(os.environ.get("GIO_ADVERTISED_ALLOW_PRIVATE"))
ADVERTISED_CHECK = _parse_check_every(os.environ.get("GIO_ADVERTISED_CHECK"))  # seconds, >= 30
# The LOCAL IP docker binds the published ports on -- what .env OUTER_IP must hold. The vendor
# archive ships OUTER_IP=127.0.0.1, so a stack re-extracted over a bootstrapped install renders a
# compose file bound to loopback: every port unreachable from the LAN/WAN and the client sees only
# Fiddler's 502 "server busy". ensure_bind_ip() repairs .env before bootstrap renders anything from
# it. Empty = auto-detect the box's primary IP when needed.
BIND_IP = os.environ.get("GIO_BIND_IP", "").strip()
# Payloads: next to this script by default; on Linux the installed copy under /opt wins when it
# exists. The DEV-machine selftest therefore validates the shipped manifests.
_DEFAULT_PAYLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payloads")
if not IS_WINDOWS and os.path.isdir("/opt/gio-agent/payloads"):
    _DEFAULT_PAYLOADS = "/opt/gio-agent/payloads"
PAYLOAD_DIR = os.environ.get("GIO_PAYLOAD_DIR", "").strip() or _DEFAULT_PAYLOADS


def _default_state_path():
    if not IS_WINDOWS:
        return "/var/lib/gio-agent/state.json"
    if CONFIG_FILE:
        return os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), "state.json")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Relic", "agent", "state.json")


STATE_PATH = os.environ.get("GIO_STATE_PATH", "").strip() or _default_state_path()


def parse_listen(text, default_port=18080):
    """GIO_AGENT_LISTEN -> (host, port). Tolerates a malformed value ("18080", "0.0.0.0", trailing
    junk) instead of crash-looping under systemd Restart=always: falls back to a sane host/port
    rather than raising on rsplit/int. Pure, so the selftest can feed it strings."""
    listen = (text or "").strip()
    host, sep, port_s = listen.rpartition(":")
    if not sep or not host:
        host, port_s = "127.0.0.1", (listen if listen.isdigit() else str(default_port))
    host = host.strip("[]")  # "[::]:18080" -> "::"
    try:
        port = int(port_s)
    except ValueError:
        log_line("gio-agent: GIO_AGENT_LISTEN='%s' invalid port, using %d" % (listen, default_port))
        port = default_port
    return host, port


# Parsed ONCE here (main() binds to it, the hotpatch mirror derives its public URL from the port).
LISTEN_HOST, LISTEN_PORT = parse_listen(os.environ.get("GIO_AGENT_LISTEN", "127.0.0.1:18080"))
# The hotpatch mirror: where the official 2021 hotfix outputs are cached (CDN layout underneath)
# and the base URL players fetch them from. Default folder: beside the state file
# (/var/lib/gio-agent/hotpatch on Linux). GIO_HOTPATCH_URL overrides the derived
# "http://<advertised or OUTER_IP>:<agent port>/hotpatch" (a reverse proxy, another port, https).


def _hotpatch_dir_from(raw, state_path):
    """GIO_HOTPATCH_DIR (raw None = unset) -> the mirror folder, before _served_folder_problem's
    refusals (_hotpatch_dirs_from applies them). Pure."""
    return (raw or "").strip() or os.path.join(os.path.dirname(os.path.abspath(state_path)), "hotpatch")


def _real_norm(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _path_within(child, parent):
    """child is parent or lies under it -- by realpath + normcase, so neither a symbolic link nor the
    letter case hides a nesting."""
    c, p = _real_norm(child), _real_norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _agent_owned_folders(config_path, state_path, payload_dir):
    """(label, folder) of what the agent keeps for itself: the configuration folder (the admin token),
    the state folder (state.json, its .bak / .corrupt-* copies, backups), the payloads and the script's
    own folder. Pure given its arguments."""
    out = [("the agent's state folder", os.path.dirname(os.path.abspath(state_path)))]
    if config_path:
        out.append(("the agent's configuration folder", os.path.dirname(os.path.abspath(config_path))))
    if payload_dir:
        out.append(("the agent's payloads", payload_dir))
    out.append(("the agent's own folder", os.path.dirname(os.path.abspath(__file__))))
    return tuple(out)


def _served_folder_problem(folder, owned, stacks):
    """Why folder may not be the hotpatch mirror or its bundle folder -- None when it may. The mirror is
    served WITHOUT a token (GET /hotpatch/<path>) and the agent writes and empties the bundle folder, so
    neither may be the root of a drive or of the filesystem, be or contain one of the agent's own folders
    (`owned`, _agent_owned_folders: the config file holds the admin token), nor lie inside or contain a
    server folder (`stacks`: (version, dir) -- creds.txt, .env, the database). A folder INSIDE the state
    folder is fine: <state dir>/hotpatch is the default. The stack relocation refuses the same nesting from
    the other side (_agent_folders). Pure given its arguments."""
    real = os.path.realpath(os.path.abspath(folder))
    if os.path.dirname(real) == real:
        return "%s is the root of a drive or of the filesystem" % folder
    for label, d in owned:
        if d and _path_within(d, folder):
            return "%s is or contains %s %s" % (folder, label, d)
    for v, d in stacks:
        if d and (_path_within(folder, d) or _path_within(d, folder)):
            return "%s and version %s's server folder %s would lie inside one another" % (folder, v, d)
    return None


HOTPATCH_URL = os.environ.get("GIO_HOTPATCH_URL", "").strip().rstrip("/")
# A file a client asks for that is not cached is fetched once from the official CDN (only under
# a known manifest's branch prefixes) unless disabled here or by the policy's hotpatchUpstream.
HOTPATCH_UPSTREAM = _flag_on(os.environ.get("GIO_HOTPATCH_UPSTREAM"))
# Byte budget (MiB) for the files fetched on demand that the manifest does NOT list -- the official
# index files enumerate the whole game (28 GB for 1.6) under the branch, and the route is token-less,
# so an anonymous caller must never be able to fill the disk from the CDN. 0 = listed files only.


def _parse_mib(text, default):
    try:
        return max(0, int((text or "").strip() or default))
    except ValueError:
        return default


HOTPATCH_ONDEMAND_MAX = _parse_mib(os.environ.get("GIO_HOTPATCH_ONDEMAND_MAX"), 2048) << 20
# Free space (MiB) a voice-pack download must leave on the mirror's volume: one voice language is
# 3-4 GB on 1.6 and 7-9 GB on 2.8, and a full disk takes MySQL down with it.
HOTPATCH_DISK_RESERVE = _parse_mib(os.environ.get("GIO_HOTPATCH_DISK_RESERVE"), 2048) << 20
# Ready-made bundles of the mirror's files on archive.org (hotpatch.json `bundles`): downloaded and
# unpacked FIRST, the official CDN per file only for what is still missing. 0 = never use them.
HOTPATCH_BUNDLES = _flag_on(os.environ.get("GIO_HOTPATCH_BUNDLES"))
# Which mirror the hotpatch content comes FROM, as a choice the admin can make and change later:
# "archive" (default) = the ready-made archive.org bundles first, the official CDN only for what is
# still missing; "cdn" = the official CDN alone, file by file. This seeds the policy field of the
# same name, which is what the launcher edits and what the code reads at run time -- the env value
# only decides what a server starts out with. GIO_HOTPATCH_BUNDLES=0 stays the hard switch (it
# refuses bundles whatever the policy says).
HOTPATCH_SOURCES = ("archive", "cdn")
HOTPATCH_SOURCE = os.environ.get("GIO_HOTPATCH_SOURCE", "archive").strip().lower()
if HOTPATCH_SOURCE not in HOTPATCH_SOURCES:
    log_line("gio-agent: WARNING: GIO_HOTPATCH_SOURCE=%r is not one of %s -- using 'archive'."
             % (HOTPATCH_SOURCE, ", ".join(HOTPATCH_SOURCES)))
    HOTPATCH_SOURCE = "archive"


def _bundle_dir_inside_mirror(bundle_dir, mirror_dir):
    """True when bundle_dir is the mirror or lies under it (by realpath). Pure, selftest-locked."""
    b = os.path.realpath(os.path.abspath(bundle_dir))
    m = os.path.realpath(os.path.abspath(mirror_dir))
    return b == m or b.startswith(m.rstrip(os.sep) + os.sep)


# Where the bundles land and are unpacked. It MUST lie outside the served tree: safe_hotpatch_path
# confines serving to HOTPATCH_DIR and ondemand_bytes budgets every unlisted file under it, so an
# archive or a staging folder inside the mirror would be served and counted. A configured value
# inside it is refused here (a WARNING, never a crash-loop under systemd) and the default applies.


def _bundle_dir_from(raw, hotpatch_dir):
    """GIO_HOTPATCH_BUNDLE_DIR (raw None = unset) -> (bundle dir, its default, the refused value or
    None). Pure, shared by this import and the next-start view of /agent/config."""
    default = os.path.join(os.path.dirname(os.path.abspath(hotpatch_dir)), "hotpatch-bundles")
    want = (raw or "").strip() or default
    if _bundle_dir_inside_mirror(want, hotpatch_dir):
        return default, default, want
    return want, default, None


def _hotpatch_dirs_from(raw_mirror, raw_bundle, state_path, owned, stacks):
    """(mirror, bundle folder, refusals) exactly as a start derives them -- this import, and the next-start
    view of /agent/config. A CONFIGURED folder _served_folder_problem refuses (a hand edit: POST /agent/config
    refuses it with 400) falls back to the default -- a WARNING per refusal, never a refusal to boot (systemd
    would crash-loop) --, and so does a bundle folder inside the mirror. refusals = [(key, value, why)]. Pure."""
    refusals = []
    mirror = _hotpatch_dir_from(raw_mirror, state_path)
    if (raw_mirror or "").strip():
        why = _served_folder_problem(mirror, owned, stacks)
        if why:
            refusals.append(("GIO_HOTPATCH_DIR", mirror, why))
            mirror = _hotpatch_dir_from(None, state_path)
    bundle, default, inside = _bundle_dir_from(raw_bundle, mirror)
    if inside:
        refusals.append(("GIO_HOTPATCH_BUNDLE_DIR", inside, "it lies inside the hotpatch mirror %s, which is served "
                                                            "without a token" % mirror))
    elif (raw_bundle or "").strip():
        why = _served_folder_problem(bundle, owned, stacks)
        if why:
            refusals.append(("GIO_HOTPATCH_BUNDLE_DIR", bundle, why))
            bundle = default
    return mirror, bundle, refusals


def _log_hotpatch_refusals(refusals, mirror, bundle):
    for key, value, why in refusals:
        log_line("gio-agent: WARNING: %s=%s is refused (%s) -- using %s." % (
            key, value, why, mirror if key == "GIO_HOTPATCH_DIR" else bundle))


HOTPATCH_DIR, HOTPATCH_BUNDLE_DIR, _hp_refusals = _hotpatch_dirs_from(
    os.environ.get("GIO_HOTPATCH_DIR"), os.environ.get("GIO_HOTPATCH_BUNDLE_DIR"), STATE_PATH,
    _agent_owned_folders(CONFIG_PATH, STATE_PATH, PAYLOAD_DIR), [(v, m["dir"]) for v, m in VERSIONS.items()])
_log_hotpatch_refusals(_hp_refusals, HOTPATCH_DIR, HOTPATCH_BUNDLE_DIR)
del _hp_refusals
# An explicit extractor (7zz/7z/7za/7zr/bsdtar, 7z.exe/tar.exe/bz.exe); empty = discovered per call.
EXTRACTOR_PATH = os.environ.get("GIO_7Z", "").strip()
# A bundle is worth its download only when at least this share of the mirror's bytes is missing;
# below it (one damaged file, a small purge) the per-file CDN path is cheaper.
BUNDLE_MIN_FRACTION = 0.5
EXTRACT_TIMEOUT = 3600  # seconds one extraction may take (26k files of the 2.8 stack, a 9 GB voice bundle)
# Free space (MiB) the volume holding a stack's parent must keep after the archive + the unpacked
# tree of a server package download: the docker images and MariaDB's data come right after it.
FETCH_DISK_RESERVE = _parse_mib(os.environ.get("GIO_FETCH_DISK_RESERVE"), 4096) << 20
FETCH_RETRIES = 30      # network cuts in a row (the counter resets whenever bytes advanced)
# TLS (agent 3.6). An extra PEM bundle of trusted roots, loaded ON TOP of the system store (a corporate
# TLS inspection, an antivirus' web shield); unreadable / invalid = a WARNING and ignored.
CA_FILE = os.environ.get("GIO_CA_FILE", "").strip()
# When the certificate of a download whose md5 AND size are pinned beforehand (a server package, a
# bundle, a manifest-listed hotpatch file, a voice pack) cannot be verified, repeat it over an
# UNVERIFIED connection: the md5 + size check then proves the file. Never for an unpinned request.
TLS_PINNED_FALLBACK = _flag_on(os.environ.get("GIO_TLS_PINNED_FALLBACK"))
# The name players see in /public/status (the policy's `name` overrides it).
SERVER_NAME = os.environ.get("GIO_SERVER_NAME", "").strip() or socket.gethostname()
# Honour X-Forwarded-For only when explicitly told to: behind nothing, the header is attacker-set.
TRUST_PROXY = _flag_off(os.environ.get("GIO_TRUST_PROXY"))
# once  -- provision a version the first time it is set up (default)
# switch-- re-apply the pre-GAA save every time you switch to a different version
# never -- never provision automatically; only the explicit /server/provision call does it
PROVISION_MODE = os.environ.get("GIO_PROVISION_MODE", "once").strip().lower()
# now   -- install the "fixed" GAA txt files together with the save
# later -- install the save now and leave the txt fixes for /server/txtfixes after GAA chapter 1
TXT_FIXES_MODE = os.environ.get("GIO_TXT_FIXES_MODE", "now").strip().lower()
# The progress choice of Prepare server / Provision (agent 3.4) -- what the payload puts into the stack:
#   default -- the shipped pre-GAA save (sdk.db + redis dump + the hk4e_db_user dump): every player
#              account on that version is wiped and the manifest account exists afterwards
#   keep    -- bring / keep your own progress: no save, the database is not touched; config files,
#              events, the abyss calendar, the advertised IP, hotpatch and the template databases still happen
#   fixes   -- configuration files only (the reversible txt fixes included), no templates either
# A stack recorded as keep/fixes is never imported automatically (needs_provision), whatever
# GIO_PROVISION_MODE says: only an explicit {progress: default} run does that.
PROGRESS_MODES = ("default", "keep", "fixes")
GM_CMD_ID = "1116"  # gmTalk

EVER = "2050-01-01 00:00:00"  # "practically forever" -- far out, but nowhere near datetime overflow

# Timeouts (seconds). Bootstrap pulls images and initialises MariaDB, so it gets a very long leash.
T_COMPOSE = 900
T_BOOTSTRAP = 5400
T_MYSQL_READY = 300

# Post-start stabilisation. `docker compose up -d` answers 0 even when half the stack is about to
# die: the game services exit with code 0 on a fatal error (a cold MySQL, a torn TSV) and their
# on-failure restart policy ignores a clean exit -- docker will never bring them back on its own.
# Measured live: nodeserver died <1s after a clean `up -d` (lost the race with a still-initialising
# mysqld), dbgate/muipserver/pathfindingserver ~5s, gameserver and multiserver ~60s -- six of
# thirteen services dead, `up` reported success, the player saw a white screen. A service therefore
# has to survive T_STABLE seconds of quiet before a start is believed.
T_STABLE = 75
STABLE_PROBES = (0, 5, 20, 45, T_STABLE)  # early probes catch the 1-5s deaths without waiting 75s
REVIVE_ROUNDS = 3          # resurrection attempts before a start is declared failed
SERVICE_WATCH_EVERY = 120  # seconds between service-watchdog sweeps of a running stack
REVIVE_BACKOFF = 600       # per version, the watchdog attempts a revive at most this often
NONCRITICAL_SERVICES = {"adminer", "phpmyadmin"}  # DB web UIs -- the game runs fine without them
# The pathfinding server (monster / NPC navigation) is optional and hungry: ~4 GB of RSS of its own on
# 1.6, OOM-killed and restarted in a loop on a box under ~12 GB (measured on a 7.5 GiB VPS: 8 kills an
# hour, ~6 min reload each). Logging in and playing work without it. It is switched off the way the
# vendor switches oaserver off: the compose profile `donotstart` on the service, in BOTH compose files
# (the .tmpl every re-render reads and the rendered file `up -d` reads); `docker compose config
# --services` then omits it, so the watchdogs never count it as dead. GIO_PATHFINDING is only the
# setup-time default of a stack prepared without an explicit choice; the compose files are the truth
# afterwards and the admin's decision is kept in state and re-asserted at every start.
PATHFINDING_SERVICE = "pathfindingserver"
DONOTSTART_PROFILE = "donotstart"
PATHFINDING_DEFAULT = _flag_on(os.environ.get("GIO_PATHFINDING"))


class AgentError(Exception):
    """A failure with an HTTP status and a message meant for the user's screen. `code` is the
    stable machine-readable reason of a PUBLIC refusal (PUBLIC_ERROR_CODES); the launcher renders
    its own translated text from it and falls back to `message` for an unknown or missing code."""

    def __init__(self, status, message, retry_after=None, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.code = code


# The public error vocabulary (docs/AGENT-API.md). Part of the contract: add codes, never rename one.
PUBLIC_ERROR_CODES = (
    "server_busy", "quota_ip_hour", "quota_ip_day", "quota_server_hour", "quota_server_day",
    "rate_limited", "rate_limited_server", "signup_disabled", "signup_disabled_version",
    "name_invalid", "name_reserved", "name_taken", "template_unavailable", "template_not_ready",
    "password_required", "password_invalid", "version_down", "unknown_version", "commands_disabled",
    "command_invalid", "bad_request", "not_found", "server_error", "state_unreadable",
    "progress_failed", "account_limit",
)


# -- platform helpers --

def _popen_kwargs():
    """Extra subprocess kwargs: no console window flashing for every docker call on Windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def replace_file(src, dst, attempts=5, delay=0.2):
    """os.replace with a short retry: on Windows an antivirus/indexer holding the destination for a
    moment answers PermissionError, and a rename is exactly the atomic step we must not give up on."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def protect_path(path, mode):
    """Keep backups/credentials private. POSIX: chmod. Windows: best-effort icacls restricting the
    ACL to the current user (never fatal -- a refused ACL must not fail the operation)."""
    try:
        if IS_WINDOWS:
            user = os.environ.get("USERNAME", "")
            if user:
                rights = "(OI)(CI)F" if os.path.isdir(path) else "F"
                subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:%s" % (user, rights)],
                               capture_output=True, timeout=30, **_popen_kwargs())
        else:
            os.chmod(path, mode)
    except Exception:  # noqa: BLE001
        pass


def read_text(path):
    """Read a text file keeping every byte and line ending (surrogateescape round-trips bytes that
    are not valid UTF-8, newline='' keeps CRLF as CRLF)."""
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def write_text_atomic(path, text):
    """tmp + rename, keeping the original's mode/owner (a rename swaps the inode)."""
    tmp = path + ".tmp"
    write_text(tmp, text)
    try:
        st = os.stat(path)
        os.chmod(tmp, st.st_mode & 0o7777)
        if hasattr(os, "chown"):
            os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    replace_file(tmp, path)


MAX_BODY = 1 << 20       # every real admin request is a few hundred bytes of JSON
PUBLIC_MAX_BODY = 4096   # /public/* bodies are tiny; anything bigger is abuse


def read_http_body(rfile, headers, max_body=MAX_BODY):
    """The request body, framed by Content-Length *or* Transfer-Encoding: chunked.

    http.server understands neither chunked request bodies nor the trailer that follows one, and
    .NET's HttpClient sends a JSON POST chunked by default (JsonContent streams, so it cannot
    report a length up front). Reading only Content-Length therefore saw an EMPTY body and made
    the agent answer every command -- start, stop, setup, provision, GM -- with
    "400 missing/invalid field: 'version'". The Relic client sends a counted body now, but keep
    this: it is what makes the agent answer any correct HTTP/1.1 client, including an older Relic
    build already installed on someone's machine.
    """
    if "chunked" in headers.get("Transfer-Encoding", "").lower():
        buf = bytearray()
        while True:
            # "<hex size>[;ext]" -- anything else is a framing error we must not guess our way past.
            head = rfile.readline(64).split(b";", 1)[0].strip()
            try:
                size = int(head or b"0", 16)
            except ValueError:
                raise AgentError(400, "invalid request body (malformed chunk)")
            if size <= 0:
                break
            if len(buf) + size > max_body:
                raise AgentError(413, "request body too large")
            while size > 0:
                part = rfile.read(size)
                if not part:
                    raise AgentError(400, "incomplete request body")
                buf += part
                size -= len(part)
            rfile.readline(8)  # the CRLF that closes the chunk data
        # Trailers, then the blank line that ends the message. Bounded: a client that never sends
        # it must not park a handler thread forever.
        for _ in range(32):
            if rfile.readline(512).strip() == b"":
                break
        return bytes(buf)

    try:
        n = int(headers.get("Content-Length", "0") or 0)
    except ValueError:
        raise AgentError(400, "invalid request body (Content-Length)")
    if n > max_body:
        raise AgentError(413, "request body too large")
    return rfile.read(n) if n > 0 else b""


# -- MUIP signing (must match the MUIP tool's crypto exactly) --

def read_sign_key(version):
    d = stack_dir(version)
    xml = os.path.join(d, "server", "muipserver", "conf", "muipserver.xml")
    if not os.path.isfile(xml):
        raise AgentError(409, "muipserver.xml does not exist yet -- run the server setup (Prepare "
                              "server) for version %s first." % version)
    with open(xml, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    m = re.search(r'sign_key\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("sign_key not found in " + xml)
    return m.group(1)


def muip_host(version):
    if MUIP_HOST:
        return MUIP_HOST
    ip = read_env(version).get("OUTER_IP", "").strip()
    if not ip:
        raise AgentError(500, "Cannot locate muipserver: GIO_MUIP_HOST is not set and OUTER_IP is "
                              "missing from the %s stack's .env." % version)
    return "http://%s:21051" % ip


def sign_query(payload, secret, host):
    """Return (url, qstr, sign) for a MUIP /api call. qstr is the RAW (unescaped) signed string."""
    ticket = "".join(random.choice(string.ascii_letters) for _ in range(32))
    kvs = [f"cmd={GM_CMD_ID}", f"ticket={ticket}", f"region={REGION}"]
    for k, v in payload.items():
        kvs.append(f"{k}={v}")
    kvs.sort()
    qstr = "&".join(kvs)
    sign = hashlib.sha256((qstr + secret).encode()).hexdigest()
    url = f"{host}/api?{urllib.parse.quote_plus(qstr, safe='=&')}&sign={sign}"
    return url, qstr, sign


def gm_command(version, uid, msg):
    check_known(version)
    secret = read_sign_key(version)
    url, _qstr, _sign = sign_query({"uid": uid, "msg": msg}, secret, muip_host(version))
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


# -- agent state (what we have already done to each stack) --
# state.json is never silently reset. A MISSING file is a fresh agent ({}); a file that exists but
# cannot be read or parsed raises StateUnreadable, and then:
#   * writers refuse (503 state_unreadable) instead of saving an almost empty dict over it -- the
#     only exception are the fail-safe stop-path writes (best_effort=True), which log and skip;
#   * needs_provision refuses too: "no provisionedAt" read off a broken file used to turn the next
#     routine Start into a full re-import of the pre-GAA save over every player;
#   * readers degrade (status: facts unknown, policy fail-closed, watchdogs skip).
# Every save fsyncs the new file and first copies the current parsable one to state.json.bak; a
# file found unreadable at startup is copied to state.json.corrupt-<UTC> and the .bak restored
# (recover_state_at_startup). Recovery markers whose loss would hide a half-done destructive step
# are saved strict=True: a failed write stops the job (507) BEFORE that step.

_state_lock = threading.Lock()
# In-process view of the state file's health: `degraded` = the reason the file was last found
# unreadable (None once it reads again), `corruptCopy` = basename of the last .corrupt-<ts> copy.
_STATE_RECOVERY = {"degraded": None, "corruptCopy": None}
_state_diag_lock = threading.Lock()
_state_skip_logged = set()  # background loops that already logged "skipped: state unreadable"


class StateUnreadable(Exception):
    """state.json exists but cannot be read, is not JSON, or is not a state object."""

    def __init__(self, path, reason):
        super().__init__("%s: %s" % (path, reason))
        self.path = path
        self.reason = reason


def state_unreadable_error():
    # No path or parser detail in the message: it can reach a token-less caller through a race
    # (/public/account/create). The reason is in /status.state and the agent log.
    return AgentError(503, "The agent state file is unreadable -- the agent refuses changes until it "
                           "is repaired or reset (see /status state).", code="state_unreadable")


def _read_state_bytes(path, attempts=5, delay=0.2):
    """The raw bytes, or None when the file does not exist. PermissionError is retried like
    replace_file (antivirus/indexer holding a healthy file on Windows); anything still failing
    raises StateUnreadable."""
    for i in range(attempts):
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except PermissionError as e:
            if i == attempts - 1:
                raise StateUnreadable(path, "permission denied (%s)" % e)
            time.sleep(delay)
        except OSError as e:
            raise StateUnreadable(path, "cannot be read (%s)" % e)
    return None


def _parse_state(raw, path):
    """bytes -> the state dict, or StateUnreadable. A BOM (a Windows editor round trip) is fine."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise StateUnreadable(path, "not valid JSON (%s)" % e)
    if not isinstance(data, dict):
        raise StateUnreadable(path, "not a JSON object")
    versions = data.get("versions")
    if versions is not None and not (isinstance(versions, dict)
                                     and all(isinstance(x, dict) for x in versions.values())):
        raise StateUnreadable(path, "\"versions\" is not an object of objects")
    return data


def _note_state_health(reason):
    """Log the unreadable -> readable transitions once each (load_state runs on every status poll)."""
    with _state_diag_lock:
        before = _STATE_RECOVERY["degraded"]
        _STATE_RECOVERY["degraded"] = reason
        if reason is None:
            _state_skip_logged.clear()
    if reason is not None and reason != before:
        log_line("gio-agent: ERROR: the state file %s is unreadable (%s) -- the agent refuses every "
                 "state change and every automatic provisioning until it is repaired by hand or reset "
                 "(POST /agent/state/reset)." % (STATE_PATH, reason))
    elif reason is None and before is not None:
        log_line("gio-agent: the state file %s is readable again." % STATE_PATH)


def load_state():
    """The persisted state: {} when the file does not exist, StateUnreadable when it exists but
    cannot be used. Never {} for a broken file -- see the section comment."""
    try:
        raw = _read_state_bytes(STATE_PATH)
        data = {} if raw is None else _parse_state(raw, STATE_PATH)
    except StateUnreadable as e:
        _note_state_health(e.reason)
        raise
    if _STATE_RECOVERY["degraded"] is not None:
        _note_state_health(None)
    return data


def require_state_readable():
    """Up-front refusal for a job that records what it does: fail BEFORE touching the stack, not
    after the files are changed and the state write refuses."""
    try:
        load_state()
    except StateUnreadable:
        raise state_unreadable_error()


def state_readable_for(loop_name):
    """Background loops: True when the state can be read; otherwise log once and skip the sweep."""
    try:
        load_state()
        return True
    except StateUnreadable:
        with _state_diag_lock:
            first = loop_name not in _state_skip_logged
            _state_skip_logged.add(loop_name)
        if first:
            log_line("gio-agent: %s: skipped while the state file is unreadable." % loop_name)
        return False


def _fsync_dir(path):
    """POSIX: make a rename durable (XFS/ext4 can lose it on a power cut otherwise). Best effort."""
    if IS_WINDOWS:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_state_atomic(data):
    """tmp + flush + fsync, the current parsable file copied to .bak, rename, directory fsync."""
    d = os.path.dirname(STATE_PATH) or "."
    os.makedirs(d, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _backup_state()
        replace_file(tmp, STATE_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def _backup_state():
    """Copy (never rename -- readers must never find state.json missing) the CURRENT file to
    state.json.bak when it parses: the last good copy recover_state_at_startup falls back on. Tmp +
    rename so a torn .bak cannot replace a good one. Never fatal."""
    bak = STATE_PATH + ".bak"
    try:
        raw = _read_state_bytes(STATE_PATH)
        if raw is None:
            return
        _parse_state(raw, STATE_PATH)
    except StateUnreadable:
        return  # a broken current file is not a backup -- keep the older .bak
    try:
        with open(bak + ".tmp", "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(bak + ".tmp", os.stat(STATE_PATH).st_mode & 0o777)  # no wider than state.json
        except OSError:
            pass
        replace_file(bak + ".tmp", bak)
    except OSError as e:
        log_line("gio-agent: WARNING: could not refresh %s (%s) -- the save goes on." % (bak, e))
        try:
            os.remove(bak + ".tmp")
        except OSError:
            pass


def save_state(data, strict=False):
    """Persist `data`. Returns True when written. strict=False (convenience facts): a failure is
    logged and the operation goes on. strict=True (recovery markers written BEFORE a destructive
    step): a failure raises 507 state_write_failed, so that step never runs unrecorded."""
    try:
        _write_state_atomic(data)
        return True
    except Exception as e:  # noqa: BLE001
        if strict:
            raise AgentError(507, "Could not save the agent state (%s) -- stopped before the step it "
                                  "had to record. Free disk space / fix the permissions of %s and retry."
                             % (e, os.path.dirname(STATE_PATH) or "."), code="state_write_failed")
        log_line("gio-agent: could not save state: %s" % e)
        return False


def state_set(strict=False, best_effort=False, **kv):
    """Merge top-level keys. Refuses (503) over an unreadable file; best_effort=True (only the
    fail-safe stop-path writes) logs and skips instead."""
    with _state_lock:
        try:
            data = load_state()
        except StateUnreadable as e:
            if best_effort:
                log_line("gio-agent: state unreadable (%s) -- not recording %s." % (e.reason, sorted(kv)))
                return False
            raise state_unreadable_error()
        data.update(kv)
        return save_state(data, strict=strict)


def version_state(version):
    """One version's persisted facts ({} when none). Unreadable state -> 503 state_unreadable."""
    try:
        return (load_state().get("versions") or {}).get(version) or {}
    except StateUnreadable:
        raise state_unreadable_error()


def version_state_set(version, strict=False, best_effort=False, **kv):
    """Merge one version's keys; same refusal / strict / best_effort rules as state_set."""
    with _state_lock:
        try:
            data = load_state()
        except StateUnreadable as e:
            if best_effort:
                log_line("gio-agent: state unreadable (%s) -- not recording %s for %s."
                         % (e.reason, sorted(kv), version))
                return False
            raise state_unreadable_error()
        versions = data.setdefault("versions", {})
        versions.setdefault(version, {}).update(kv)
        return save_state(data, strict=strict)


def _corrupt_copy_path(now=None):
    """state.json.corrupt-<YYYYmmddTHHMMSSZ>, suffixed when that second is already taken."""
    base = STATE_PATH + ".corrupt-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    path, n = base, 1
    while os.path.exists(path):
        path, n = "%s-%d" % (base, n), n + 1
    return path


def _keep_corrupt_copy(raw, now=None):
    """Write the unreadable bytes beside the state file (tmp + fsync + rename). An identical copy
    from an earlier restart is reused, so a degraded agent restarting in a loop leaves one copy.
    Returns the basename; raises OSError."""
    d = os.path.dirname(STATE_PATH) or "."
    prefix = os.path.basename(STATE_PATH) + ".corrupt-"
    for name in sorted(os.listdir(d), reverse=True):
        if name.startswith(prefix):
            try:
                with open(os.path.join(d, name), "rb") as f:
                    if f.read() == raw:
                        return name
            except OSError:
                continue
    path = _corrupt_copy_path(now)
    with open(path + ".tmp", "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(path + ".tmp", os.stat(STATE_PATH).st_mode & 0o777)
    except OSError:
        pass
    replace_file(path + ".tmp", path)
    return os.path.basename(path)


def recover_state_at_startup(now=None):
    """main(), before the HTTP server and the watchdogs. A state file that is unreadable is copied
    to .corrupt-<ts>; when state.json.bak parses it is restored into place with runningOk cleared
    for every version (the next start re-verifies; a .bak rolls back at most one write) and a
    top-level `restoredFromBackup` record that keeps automatic provisioning off (needs_provision).
    With no usable .bak -- or when the copy cannot be made -- the unreadable file stays exactly
    where it is: a MISSING state.json would read as {} and re-enable auto-provisioning.
    Returns "ok" | "restored" | "degraded"."""
    try:
        load_state()
        return "ok"
    except StateUnreadable as e:
        reason = e.reason
    try:
        raw = _read_state_bytes(STATE_PATH)
        if raw is not None:
            try:
                _parse_state(raw, STATE_PATH)
            except StateUnreadable:
                pass
            else:
                # The first failure was transient (an antivirus lock held past the retries, one EIO):
                # the file is healthy -- never roll it back to .bak or copy it aside as "corrupt".
                log_line("gio-agent: the state file %s read fine on a second attempt (%s) -- kept as it is."
                         % (STATE_PATH, reason))
                try:
                    load_state()  # clears the degraded note
                except StateUnreadable:
                    return "degraded"
                return "ok"
        copy = _keep_corrupt_copy(raw if raw is not None else b"", now)
    except (OSError, StateUnreadable) as e:
        log_line("gio-agent: ERROR: could not copy the unreadable state file aside (%s) -- leaving it "
                 "in place; the agent runs degraded." % getattr(e, "reason", e))
        return "degraded"
    with _state_diag_lock:
        _STATE_RECOVERY["corruptCopy"] = copy
    bak = STATE_PATH + ".bak"
    try:
        braw = _read_state_bytes(bak)
        data = None if braw is None else _parse_state(braw, bak)
    except StateUnreadable as e:
        log_line("gio-agent: ERROR: %s is unusable too (%s)." % (bak, e.reason))
        data = None
    if data is None:
        log_line("gio-agent: ERROR: state file unreadable (%s), no usable %s -- left in place (copy: %s). "
                 "The agent refuses state changes until the file is repaired or reset." % (reason, bak, copy))
        return "degraded"
    for vs in (data.get("versions") or {}).values():
        vs["runningOk"] = False
    # The .bak may predate a verification toggle: the sdk config is what the sdk enforces.
    pv = reconcile_password_verify(data)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    data["restoredFromBackup"] = {"at": stamp, "corruptCopy": copy, "reason": reason}
    with _state_lock:
        if not save_state(data):
            log_line("gio-agent: ERROR: could not write the restored state -- the unreadable file stays "
                     "in place; the agent runs degraded.")
            return "degraded"
    try:
        load_state()  # clears the degraded note
    except StateUnreadable:
        pass  # a transient failure right after the write: the next read clears it
    if pv:
        log_line("gio-agent: passwordVerify re-read from the sdk config after the restore: %s." % ", ".join(pv))
    log_line("gio-agent: ERROR: state file unreadable (%s) -- restored %s (copy of the broken file: %s). "
             "Top-level keys: %s; versions: %s. The most recent change may be missing; runningOk cleared; "
             "automatic provisioning is off until an explicit provision."
             % (reason, os.path.basename(bak), copy, ", ".join(sorted(data)) or "-",
                ", ".join("%s (provisionedAt %s)" % (v, (vs or {}).get("provisionedAt") or "none")
                          for v, vs in sorted((data.get("versions") or {}).items())) or "-"))
    return "restored"


def reset_state(confirm, force=False):
    """POST /agent/state/reset {"confirm": true}: set the current file aside as .corrupt-<ts> and
    start from an empty state. A `stateReset` record keeps automatic provisioning off for versions
    with no provisioning marker (same guard as a backup restore). Refused while a job runs, and --
    unless `force` -- when the file parses again (a hand repair, a button clicked on a stale screen):
    that would throw a healthy policy and every per-version record away."""
    if confirm is not True:
        raise AgentError(400, 'Send {"confirm": true} to reset the agent state.')
    busy = current_job()
    if busy is not None:
        raise AgentError(409, "Another operation is running (%s, version %s) -- reset the state when it "
                              "has finished." % (busy.kind, busy.version))
    with _state_lock:
        try:
            raw = _read_state_bytes(STATE_PATH)
        except StateUnreadable as e:
            raise AgentError(500, "Cannot read the state file to keep a copy of it (%s) -- fix its "
                                  "permissions or move it aside by hand." % e.reason)
        if raw is not None and not force:
            try:
                _parse_state(raw, STATE_PATH)
            except StateUnreadable:
                pass
            else:
                try:
                    load_state()  # clears the degraded note the stale screen was showing
                except StateUnreadable:
                    pass
                raise AgentError(409, "The state file is readable again -- nothing was reset. Refresh the "
                                      "server status.", code="state_readable")
        copy = None
        if raw is not None:
            try:
                copy = _keep_corrupt_copy(raw)
            except OSError as e:
                raise AgentError(500, "Could not keep a copy of the state file (%s) -- nothing was reset." % e)
        reason = _STATE_RECOVERY["degraded"]
        data = {"stateReset": {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "corruptCopy": copy,
                               "reason": reason}}
        # The sdk keeps enforcing a verification switched on earlier: forgetting it would make signups
        # register with a generated password nobody knows.
        reconcile_password_verify(data)
        save_state(data, strict=True)
    with _state_diag_lock:
        _STATE_RECOVERY["corruptCopy"] = copy or _STATE_RECOVERY["corruptCopy"]
    try:
        now_data = load_state()  # just written: readable, and this clears the degraded note
    except StateUnreadable:
        now_data = data  # a transient failure right after the write must not 500 a finished reset
    _invalidate_public_status()
    log_line("gio-agent: agent state reset by the admin (previous file kept as %s)." % (copy or "-"))
    return {"ok": True, "corruptCopy": copy, "state": state_report(data=now_data)}


def state_report(error=None, data=None):
    """The /status "state" block."""
    data = data if isinstance(data, dict) else {}
    return {"ok": error is None, "error": error, "degraded": _STATE_RECOVERY["degraded"] is not None,
            "path": STATE_PATH,
            "restoredFromBackup": data.get("restoredFromBackup") if error is None else None,
            "reset": data.get("stateReset") if error is None else None,
            "corruptCopy": _STATE_RECOVERY["corruptCopy"]}


# -- the provisioned marker (<stack>/.relic-provisioned) --
# The one provisioning fact that must survive a lost state file: written with provisionedAt, beside
# the database it describes (mysql/ lives in the stack dir too), the same way `bootstrapped` comes
# from .bootstrap.lock. A fresh bootstrap -- which wipes mysql/ -- removes it first.

PROVISIONED_MARKER = ".relic-provisioned"
_MARKER_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def provisioned_marker_path(version):
    return os.path.join(stack_dir(version), PROVISIONED_MARKER)


def _marker_text(version):
    """The marker file's text, "" when it exists but cannot be read, None when there is none."""
    if not is_configured(version):
        return None
    try:
        return read_text(provisioned_marker_path(version))
    except FileNotFoundError:
        return None
    except OSError:
        return ""


def _parse_provisioned_marker(text, mtime_fallback=None):
    """Marker v2 (agent 3.4): line 1 the timestamp, line 2 `progress=<mode>`, line 3
    `defaultAccount=0|1`. A one-line marker (3.2 / 3.3) reads as default/1 -- importing the shipped
    save was the only thing those agents could have done. Pure."""
    lines = [l.strip() for l in (text or "").splitlines()]
    first = lines[0] if lines else ""
    at = first if _MARKER_STAMP_RE.match(first) else mtime_fallback
    progress, default_account = "default", True
    for line in lines[1:]:
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip().lower()
        if k == "progress" and v in PROGRESS_MODES:
            progress = v
        elif k == "defaultAccount":
            default_account = v not in ("0", "false", "no", "")
    return {"at": at, "progress": progress, "defaultAccount": default_account}


def read_provisioned_marker(version):
    """None when there is no marker, else {"at", "progress", "defaultAccount"}: the provisionedAt
    the marker holds (its mtime when line 1 is no timestamp), the progress mode of the last
    provision and whether the shipped save is in this database. The file's EXISTENCE is the fact;
    the content keeps the public generation stable and the keep/fixes decision alive when state.json
    has to be re-seeded from it (a lost record must never turn a keep stack into an auto-import)."""
    return marker_at(stack_dir(version)) if is_configured(version) else None


def marker_at(folder):
    """read_provisioned_marker for any folder (the stack relocation reads the one a version is about to
    be pointed at): None when it holds no marker, an unreadable marker reads as the bare fact."""
    path = os.path.join(folder, PROVISIONED_MARKER)
    try:
        text = read_text(path)
    except FileNotFoundError:
        return None
    except OSError:
        text = ""
    st = _stat_or_none(path)
    fallback = None if st is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
    return _parse_provisioned_marker(text, fallback)


def provision_record(version):
    """The provisioning record the way status() reports it: the state's, filled in from the durable
    <stack>/.relic-provisioned marker for whatever the state does not hold.

    Every decision that keys off "what was this stack provisioned with" MUST read this, never
    version_state() alone. The state file is the losable half (a restored .bak, a reset, a wipe);
    the marker is the half that lives with the stack, and needs_provision re-seeds the state FROM it.
    Reading only the state made two destructive mistakes possible: a heal ("default" over a stack
    whose record was only in the marker) re-imported the save over a live player database, and a
    keep/fixes run rewrote defaultAccount to 0 and bumped the public generation, which hides the
    in-game login and marks every remembered player account stale. An unreadable state still raises
    (503) before anything is touched."""
    vs = dict(version_state(version))
    marker = read_provisioned_marker(version)
    if marker is None:
        return vs
    if not vs.get("provisionedAt"):
        vs["provisionedAt"] = marker["at"]
    if vs.get("progress") not in PROGRESS_MODES:
        vs["progress"] = marker["progress"]
    if "defaultAccount" not in vs:
        vs["defaultAccount"] = marker["defaultAccount"]
    return vs


def write_provisioned_marker(version, provisioned_at, progress="default", default_account=True):
    _write_stack_text(provisioned_marker_path(version),
                      "%s\nprogress=%s\ndefaultAccount=%d\n" % (provisioned_at, progress, 1 if default_account else 0))


def forget_provisioned(version, job):
    """Before a FRESH bootstrap (it wipes mysql/ + redis/): drop provisionedAt and the marker, or a
    setup whose provision then fails would leave a stale record and the next Start would skip the
    import the empty database still needs. Unreadable state -> 503 before anything is touched.
    The progress record goes with it and defaultAccount turns False: the pre-made save is gone with
    mysql/ (sdk.db may still hold the name, but no player stands behind it)."""
    had = bool(version_state(version).get("provisionedAt"))
    # autoProvision goes too: the database it protected is about to be wiped, and an empty one needs the import
    version_state_set(version, strict=True, provisionedAt=None, progress=None, defaultAccount=False,
                      autoProvision=None)
    path = provisioned_marker_path(version)
    try:
        os.remove(path)
        had = True
    except FileNotFoundError:
        pass
    except OSError as e:
        raise AgentError(500, "Could not remove %s (%s) -- not reinstalling: the stale marker would stop "
                              "the save from being imported into the new database." % (path, e))
    if had:
        job.log("The bootstrap wipes the database: the earlier provisioning record is forgotten.")


def migrate_provisioned_markers():
    """Agent start: a stack provisioned before the marker existed gets one from the readable state,
    and (agent 3.4) a record without a `progress` key is back-filled as default/True -- the only
    thing an older agent could have done was import the shipped save -- while a one-line marker is
    rewritten in the v2 form. Returns how many markers were written or rewritten."""
    try:
        data = load_state()
    except StateUnreadable:
        return 0
    n = 0
    for v in configured_versions():
        vs = (data.get("versions") or {}).get(v) or {}
        at = vs.get("provisionedAt")
        if not at:
            continue
        progress = vs.get("progress")
        if progress not in PROGRESS_MODES:
            progress = "default"
            version_state_set(v, progress="default", defaultAccount=True)
            log_line("gio-agent: migrated the provisioning record of %s: default save." % v)
        default_account = bool(vs.get("defaultAccount", True)) if "progress" in vs else True
        if not is_bootstrapped(v):
            continue
        text = _marker_text(v)
        if text is not None and "progress=" in text:
            continue  # already v2
        try:
            write_provisioned_marker(v, str(at), progress, default_account)
            n += 1
            log_line("gio-agent: %s %s for %s (provisioned %s, progress %s)."
                     % ("rewrote" if text is not None else "wrote", PROVISIONED_MARKER, v, at, progress))
        except OSError as e:
            log_line("gio-agent: WARNING: could not write %s for %s: %s" % (PROVISIONED_MARKER, v, e))
    return n


def _without_provision_record(version, vs):
    return is_configured(version) and is_bootstrapped(version) and not vs.get("provisionedAt") \
        and read_provisioned_marker(version) is None


def release_provision_guard(job):
    """After a provision or start that succeeded: drop `restoredFromBackup` / `stateReset` once no
    installed version is left without a provisioning record -- the guard then protects nothing. A
    version still without one keeps it (and the admin banner) until it is provisioned explicitly."""
    with _state_lock:
        try:
            data = load_state()
        except StateUnreadable:
            return
        if not (data.get("restoredFromBackup") or data.get("stateReset")):
            return
        versions = data.get("versions") or {}
        waiting = [v for v in configured_versions() if _without_provision_record(v, versions.get(v) or {})]
        if waiting:
            job.log("Automatic provisioning stays off for %s (no provisioning record since the state was "
                    "%s) -- run Provision there if that stack really needs the save."
                    % (", ".join(waiting), "restored" if data.get("restoredFromBackup") else "reset"))
            return
        data.pop("restoredFromBackup", None)
        data.pop("stateReset", None)
        if save_state(data):
            job.log("Every installed version has a provisioning record again -- the state-recovery guard "
                    "is lifted.")


# -- stack facts --

def check_known(version):
    if version not in VERSIONS:
        raise AgentError(400, "Unknown version: %s." % version, code="unknown_version")


def is_configured(version):
    return bool(VERSIONS.get(version, {}).get("dir"))


def configured_versions():
    return [v for v, meta in VERSIONS.items() if meta["dir"]]


def dir_setting_key(version):
    """The config key that holds a version's stack folder: "2.8" -> "GIO_DIR_28"."""
    return "GIO_DIR_" + version.replace(".", "")


def set_version_dir(version, new_dir):
    """Point a version at another stack folder ("" = not configured) for the rest of this process.
    ONE assignment of a fresh dict: status() and the watchdogs read VERSIONS without a lock and must
    never see the new dir with the old project name. The caller holds _op_lock, has brought the old
    project down and has already persisted dir_setting_key(version) (save_config_values)."""
    check_known(version)
    VERSIONS[version] = {"dir": new_dir, "project": _project_name(new_dir) if new_dir else ""}
    _invalidate_public_status()


def stack_dir(version):
    check_known(version)
    d = VERSIONS[version]["dir"]
    if not d:
        raise AgentError(404, "Version %s is not configured on this server (GIO_DIR_%s is empty)."
                         % (version, version.replace(".", "")))
    return d


def is_present(version):
    """The stack's files are on this box (a compose file or its template is enough)."""
    d = VERSIONS[version]["dir"]
    return bool(d) and os.path.isdir(d) and (
        os.path.isfile(os.path.join(d, "docker-compose.yml"))
        or os.path.isfile(os.path.join(d, "docker-compose.yml.tmpl"))
    )


def is_bootstrapped(version):
    """The bootstrap has completed: it drops .bootstrap.lock and the rendered compose file."""
    d = VERSIONS[version]["dir"]
    return bool(d) and os.path.isfile(os.path.join(d, ".bootstrap.lock")) and os.path.isfile(
        os.path.join(d, "docker-compose.yml"))


def payload_dir(version):
    return os.path.join(PAYLOAD_DIR, version)


def read_manifest(version):
    path = os.path.join(payload_dir(version), "manifest.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_stack_catalog(version):
    """payloads/<version>/stack.json -- where the ready-made server package of this version lives
    on the Internet Archive (url, size, md5, extractedSize, topDir, strays); None when this version
    ships no catalogue. Not validated here: validate_stack_catalog() is the caller's gate."""
    path = os.path.join(payload_dir(version), "stack.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


_PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


def validate_stack_catalog(cat):
    """Refuse a catalogue that could send the job anywhere but an https archive whose top folder is
    one plain path component: the file ships inside the payloads, but a tampered copy must not be
    able to point the agent at http:// or a file path, nor rename "../x" onto the stack dir.
    Pure; raises ValueError with the reason."""
    if not isinstance(cat, dict):
        raise ValueError("not a JSON object")
    url = cat.get("url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) < len("https://x/y"):
        raise ValueError("url must be an https:// address")
    if any(c in url for c in " \t\r\n\"'\\"):
        raise ValueError("url holds a blank, a quote or a backslash")
    size = cat.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size must be a positive integer (bytes)")
    md5 = cat.get("md5")
    if not isinstance(md5, str) or not re.match(r"^[0-9a-fA-F]{32}$", md5):
        raise ValueError("md5 must be 32 hex digits")
    ext = cat.get("extractedSize", 0)
    if isinstance(ext, bool) or not isinstance(ext, int) or ext < 0:
        raise ValueError("extractedSize must be a non-negative integer (bytes)")
    top = cat.get("topDir")
    if not isinstance(top, str) or not _PLAIN_NAME_RE.match(top) or top in (".", "..") or top.startswith("."):
        raise ValueError("topDir must be one plain folder name")
    strays = cat.get("strays", [])
    if not isinstance(strays, list) or not all(isinstance(s, str) and _PLAIN_NAME_RE.match(s) and s not in (".", "..")
                                               for s in strays):
        raise ValueError("strays must be a list of plain file names")
    return True


def read_env(version):
    """Parse the stack's .env (KEY=VALUE, tolerant of CRLF, BOM and comments)."""
    out = {}
    path = os.path.join(stack_dir(version), ".env")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip().lstrip("\ufeff")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


# -- bind IP guard (a re-extracted stack ships OUTER_IP=127.0.0.1) --

def _is_loopback(ip):
    return not ip or ip == "localhost" or ip.startswith("127.")


def _bindable(ip):
    """Can this box bind a socket to `ip`? Catches a WAN address put where the LAN one belongs --
    docker would fail the same way ("cannot assign requested address"), just much later."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return True
        finally:
            s.close()
    except OSError:
        return False


def detect_local_ip():
    """The box's primary outbound IP. UDP connect() only does a route lookup -- no packet leaves."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("203.0.113.1", 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def write_env_value(version, key, value):
    """Set KEY=value in the stack's .env, touching nothing else (comments document real passwords)
    and keeping the file's line endings.

    A leading BOM (a Windows editor's round trip) is stripped: it is not \\s, so it would hide a
    first-line KEY from the regex and this function would append a duplicate instead of editing."""
    path = os.path.join(stack_dir(version), ".env")
    try:
        text = read_text(path).lstrip("\ufeff")
    except FileNotFoundError:
        text = ""
    nl = "\r\n" if "\r\n" in text else "\n"
    line = "%s=%s" % (key, value)
    # [^\r\n]* rather than .*$: '.' would swallow the CR of a CRLF file and the rewrite would
    # silently convert that one line to LF.
    new, n = re.subn(r"(?m)^[ \t]*%s[ \t]*=[^\r\n]*" % re.escape(key), line, text)
    if n == 0:
        new = text + ("" if text.endswith("\n") or not text else nl) + line + nl
    write_text(path, new)


def advertise_intent():
    """An advertised address is configured: a static IP, or a host -- resolved or not. A DDNS name
    that does not resolve at this moment must not degrade the box to localhost-only play."""
    return bool(ADVERTISED_IP or ADVERTISED_HOST)


def reachable_intent():
    """This box is meant to be reached by clients outside itself: an advertised WAN IP/host or an
    explicit non-loopback bind IP both say so. Only with neither is localhost-only play assumed."""
    return bool(advertise_intent() or (BIND_IP and not _is_loopback(BIND_IP)))


def ensure_bind_ip(version, job):
    """Make .env OUTER_IP a bindable local IP BEFORE bootstrap renders from it.

    prepare-vars.sh bakes OUTER_IP into the compose port bindings, every server XML and data.sql,
    so the vendor default (127.0.0.1) yields a stack only the box itself can reach: the game client
    gets Fiddler's 502 "server busy" with every service showing green. GIO_BIND_IP wins; otherwise a
    loopback (or no-longer-owned) value is replaced with the auto-detected LAN IP. Only a genuine
    localhost-only setup (no advertised IP, no bind IP) is left alone."""
    cur = read_env(version).get("OUTER_IP", "").strip()
    target = BIND_IP
    if target and _is_loopback(target) and advertise_intent():
        raise AgentError(500, "GIO_BIND_IP=%s is a loopback address but GIO_ADVERTISED_IP/HOST is set -- "
                              "clients would have nothing to connect to. Put the machine's LAN IP "
                              "in GIO_BIND_IP (agent config)." % target)
    if not target:
        if not _is_loopback(cur) and _bindable(cur):
            return cur  # a real IP of this box -- trust it
        if _is_loopback(cur) and not advertise_intent():
            job.log("OUTER_IP=%s and GIO_ADVERTISED_IP/HOST is not set -- assuming play on this machine "
                    "only, leaving .env alone." % (cur or "(empty)"))
            return cur
        # loopback on a reachable server, or an IP the box no longer owns (unbindable is wrong in
        # EVERY mode -- docker would refuse it): detect the real one.
        target = detect_local_ip()
        if not target or _is_loopback(target):
            raise AgentError(500, "OUTER_IP in .env is '%s' (loopback or an IP this machine no longer "
                                  "has) and the LAN IP could not be detected. Set GIO_BIND_IP in the "
                                  "agent config and restart the agent." % (cur or ""))
    if not _bindable(target):
        # Checked BEFORE the cur == target shortcut: a stale value in BOTH .env and GIO_BIND_IP
        # (box renumbered) must fail here, not minutes later inside docker.
        raise AgentError(500, "Cannot bind to %s -- it is not an IP of this machine. OUTER_IP/"
                              "GIO_BIND_IP must be the LOCAL IP (docker binds to it); the public IP "
                              "clients use goes in GIO_ADVERTISED_IP." % target)
    if cur == target:
        return cur
    job.log("Fixing OUTER_IP in .env: %s -> %s (docker binds its ports to it)."
            % (cur or "(empty)", target))
    write_env_value(version, "OUTER_IP", target)
    return target


def compose_binds_loopback(version):
    """The rendered compose publishes ports on loopback although this server is meant to be reached
    from outside -- the tell-tale of a stack re-extracted over a bootstrapped install and rendered
    from the vendor .env. Every published port in the vendor template is %OUTER_IP%:..., so a
    correct render leaves no '127.0.0.1:' behind. Keyed on reachable_intent(), not ADVERTISED_IP
    alone: a LAN-only box (advertised empty, GIO_BIND_IP set) walks into the same 502 trap."""
    if not reachable_intent():
        return False
    path = os.path.join(stack_dir(version), "docker-compose.yml")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return "127.0.0.1:" in f.read()
    except OSError:
        return False


def stack_templates(version):
    """Every *.tmpl the vendor prepare-vars.sh renders: compose, data.sql, sdk config, server XMLs."""
    d = stack_dir(version)
    templates = []
    for name in ("docker-compose.yml.tmpl", "data.sql.tmpl",
                 os.path.join("sdk", "data", "config.json.tmpl")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            templates.append(p)
    conf_root = os.path.join(d, "server")
    for entry in sorted(os.listdir(conf_root)) if os.path.isdir(conf_root) else []:
        conf = os.path.join(conf_root, entry, "conf")
        if not os.path.isdir(conf):
            continue
        for name in sorted(os.listdir(conf)):
            if name.endswith(".tmpl"):
                templates.append(os.path.join(conf, name))
    return templates


def render_stack_configs(version, job):
    """Re-render every config the stack's prepare-vars.sh renders, from the same *.tmpl files and
    the same .env values -- without docker and without the .bootstrap.lock dance that script needs.

    Used to heal a re-extracted stack whose DB is fine but whose rendered files carry the vendor
    OUTER_IP, and after a secrets rotation; bootstrap.sh would wipe mysql/redis for the same result.
    Substitution is generic (%KEY% for every .env key), so it tracks the vendor templates instead of
    hardcoding their variable list. Line endings are preserved byte-for-byte."""
    d = stack_dir(version)
    env = read_env(version)

    def render(tmpl):
        dst = tmpl[:-len(".tmpl")]
        text = read_text(tmpl)
        for k, v in env.items():
            text = text.replace("%%%s%%" % k, v)
        left = sorted(set(re.findall(r"%([A-Z_][A-Z0-9_]*)%", text)))
        if left:
            job.log("  WARNING: %s still has unresolved placeholder(s): %s"
                    % (os.path.relpath(dst, d), ", ".join(left)))
        write_text(dst, text)
        return os.path.relpath(dst, d)

    templates = stack_templates(version)
    for tmpl in templates:
        job.log("  rendered: " + render(tmpl))
    return len(templates)


# -- pathfinding server on/off, file level (the pure text editors live next to _rewrite) --

def _compose_paths(version):
    d = stack_dir(version)
    return os.path.join(d, "docker-compose.yml.tmpl"), os.path.join(d, "docker-compose.yml")


def pathfinding_enabled(version):
    """Whether the stack runs pathfindingserver, read from the RENDERED docker-compose.yml when it
    exists (what `up -d` reads -- a box where only that file was edited by hand makes it the truth),
    else the .tmpl. None when neither is readable or the service is not in the file (a foreign stack)."""
    tmpl, rendered = _compose_paths(version)
    for path in (rendered, tmpl):
        try:
            text = read_text(path)
        except OSError:
            continue
        prof = compose_service_profiled(text, PATHFINDING_SERVICE)
        return None if prof is None else not prof
    return None


def compose_disabled_services_of(version):
    """Service names the rendered compose file switches off with the donotstart profile (an empty
    set on any error -- the health accounting then relies on compose's own view alone)."""
    try:
        return compose_disabled_services(read_text(_compose_paths(version)[1]))
    except (OSError, AgentError):
        return set()


def apply_pathfinding(version, job, enabled):
    """Write the decision into BOTH compose files: the .tmpl every re-render reads (bootstrap's
    prepare-vars, the loopback heal, a secrets rotation) and the rendered file `up -d` reads.
    Atomic, endings kept, idempotent; a missing rendered file = not bootstrapped yet. Returns
    whether anything changed."""
    tmpl, rendered = _compose_paths(version)
    n1 = _rewrite(tmpl, lambda t: set_pathfinding_text(t, enabled))
    n2 = _rewrite(rendered, lambda t: set_pathfinding_text(t, enabled))
    if n1 or n2:
        job.log("Pathfinding server %s in %s."
                % ("enabled" if enabled else "excluded (profile %s)" % DONOTSTART_PROFILE,
                   " + ".join(name for name, k in (("docker-compose.yml.tmpl", n1),
                                                   ("docker-compose.yml", n2)) if k)))
    return bool(n1 or n2)


def reconcile_pathfinding(version, job, enabled):
    """Make a RUNNING stack match the files. Disable = stop + remove the container: addressed
    through the profile (so a retry after a half-done run is idempotent) and with `rm -s` so its
    static 172.10.3.8 endpoint is gone before the next `down` walks the network. Enable = start just
    that service (--no-deps: nothing else is recreated, nobody is kicked) and prove it survives the
    75 s window -- a broken one dies ~5 s in, an OOM-killed one shows as a restart loop. Returns
    whether the docker half ran (False = not up, not bootstrapped, or docker mute)."""
    up, err = stack_up(version)
    if err:
        job.log("Cannot read the docker state (%s) -- the change applies at the next start." % err)
        return False
    if not up or not is_bootstrapped(version):
        return False
    d = stack_dir(version)
    if not enabled:
        rc = _compose_stream(d, job, "--profile", DONOTSTART_PROFILE, "rm", "-s", "-f",
                             PATHFINDING_SERVICE, prefix="  ")
        if rc != 0:
            raise AgentError(500, "The compose files were changed, but stopping %s failed (code %d) -- "
                                  "stop and start the version to apply it." % (PATHFINDING_SERVICE, rc))
        return True
    rc = _compose_stream(d, job, "up", "-d", "--no-deps", PATHFINDING_SERVICE, prefix="  ")
    if rc != 0:
        raise AgentError(500, "docker compose up %s failed (code %d)." % (PATHFINDING_SERVICE, rc))
    verify_stack(version, job)
    return True


def do_pathfinding(version, job, enabled):
    """POST /server/pathfinding {version, enabled}: edit both compose files, persist the decision
    BEFORE the docker half (like hotpatch -- a failed reconcile or a re-extracted archive is
    re-asserted by the next start), then reconcile a running stack."""
    require_state_readable()
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                         % (version, stack_dir(version)))
    changed = apply_pathfinding(version, job, enabled)
    version_state_set(version, pathfinding=bool(enabled))
    applied = reconcile_pathfinding(version, job, enabled)
    if enabled:
        job.log("The pathfinding server runs with the stack -- several GB of RAM of its own; a machine "
                "with too little kills and restarts it in a loop.")
    else:
        job.log("Monsters and NPCs will not path-find while it is off; connecting and playing work.")
    return {"enabled": bool(enabled), "changed": changed, "applied": applied}


def _add_exec_bit(path, fixed, base):
    """chmod +x semantics: add x wherever r already is, keep everything else. Regular files only."""
    try:
        if not os.path.isfile(path):
            return
        st = os.stat(path)
        if st.st_mode & 0o111:
            return
        os.chmod(path, st.st_mode | ((st.st_mode & 0o444) >> 2))
        fixed.append(os.path.relpath(path, base))
    except OSError:
        pass


def ensure_exec_bits(version, job):
    """Give back the exec bits a 7z/zip extraction dropped -- unlike tar, those formats carry no
    Unix permissions, so everything lands mode 644. Docker EXECUTES three kinds of files straight
    off the bind mounts: every game service is `command: ./<svc>` in server/<svc>/ (nine ELF
    binaries), the docker-preinstall.yml containers exec the mounted dockerfiles/*/*.sh by path,
    and the vendor *.sh in the stack root are what an admin runs by hand. At mode 644 the whole
    stack dies on "permission denied" before its first log line. Runs before bootstrap and before
    every start so a re-extraction over a live install heals too. Idempotent, a dozen stat()
    calls, never fatal. A no-op on non-POSIX (Windows has no Unix exec bit; Docker Desktop mounts
    do not need it)."""
    if os.name != "posix":
        job.log("Exec-bit repair skipped (not a POSIX filesystem).")
        return 0
    d = stack_dir(version)
    fixed = []
    try:
        for name in sorted(os.listdir(d) if os.path.isdir(d) else []):
            if name.endswith(".sh"):
                _add_exec_bit(os.path.join(d, name), fixed, d)
        for sub in ("dockerfiles", "sdk"):
            top = os.path.join(d, sub)
            for walk_root, _dirs, files in os.walk(top) if os.path.isdir(top) else []:
                for name in files:
                    if name.endswith(".sh"):
                        _add_exec_bit(os.path.join(walk_root, name), fixed, d)
        srv = os.path.join(d, "server")
        for name in sorted(os.listdir(srv) if os.path.isdir(srv) else []):
            # the vendor convention: the service's binary is server/<name>/<name>
            _add_exec_bit(os.path.join(srv, name, name), fixed, d)
    except OSError as e:
        # same contract as apply_tower_schedule: log and let the start continue -- an unreadable
        # dir will fail loudly (and with a better message) in whatever runs next anyway.
        job.log("WARNING: could not check exec bits (%s) -- continuing." % e)
    if fixed:
        job.log("Repairing %d exec bits lost at extraction (7z/zip keep no permissions): %s"
                % (len(fixed), ", ".join(fixed[:6]) + (" ..." if len(fixed) > 6 else "")))
    return len(fixed)


# The 2.8 archive (gio_2.8.7z on archive.org) ships server/data one level too deep:
# server/data/2.8_live-output_9464149-server-data/{txt,json,lua,server_data_version*.txt}, beside a
# stray, OLDER top-level json/ tree. gameserver/multiserver/pathfinding read server/data/txt/ and exit
# within 3 s when ConstValueData.txt is not there -- while `up -d` answers 0. The regex is specific
# on purpose (a future vendor re-pack with another nesting name must be looked at, not guessed).
_NESTED_DATA_RE = re.compile(r"-output_\d+-server-data$")
_NESTED_DATA_DIRS = ("txt", "json", "lua")


def _count_files(path):
    if os.path.isfile(path):
        return 1
    return sum(len(fns) for _dp, _dn, fns in os.walk(path))


def _hoist(src, dst, rel, owned, log):
    """Move src onto dst (rel = dst's stack-relative path with "/"): a rename when nothing is there,
    else a file-by-file merge where a file the payload manifest OWNS keeps the top copy (the agent's
    own install, e.g. server/data/txt/MaterialDeleteData.txt) and every other conflict lets the
    NESTED copy win (the top-level stray is the older tree). Returns the files moved."""
    if not os.path.lexists(dst):
        n = _count_files(src)
        os.rename(src, dst)
        return n
    if os.path.isfile(src):
        if not os.path.isfile(dst):
            log("  WARNING: %s is a folder at the top and a file in the nested tree -- left alone." % rel)
            return 0
        if rel in owned:
            os.remove(src)
            return 0
        replace_file(src, dst)
        return 1
    if not os.path.isdir(dst):
        log("  WARNING: %s is a file at the top and a folder in the nested tree -- left alone." % rel)
        return 0
    moved = 0
    for entry in sorted(os.listdir(src)):
        moved += _hoist(os.path.join(src, entry), os.path.join(dst, entry), rel + "/" + entry, owned, log)
    try:
        os.rmdir(src)
    except OSError:
        pass  # something was left alone above
    return moved


def heal_nested_server_data(stack, owned_dsts, log):
    """Hoist txt/, json/, lua/ and server_data_version*.txt out of every
    server/data/*-output_<n>-server-data/ into server/data/ (see _NESTED_DATA_RE). owned_dsts = the
    manifest's dst paths (stack-relative, "/"): those keep the TOP copy on a conflict, everything
    else lets the nested copy win. An emptied nested folder is removed, a non-empty leftover is
    logged. Idempotent (a flat tree is a no-op), returns the number of files moved, never raises
    for a filesystem problem (WARNING + what was done so far, like ensure_exec_bits)."""
    data = os.path.join(stack, "server", "data")
    if not os.path.isdir(data):
        return 0
    moved = 0
    try:
        for name in sorted(os.listdir(data)):
            nested = os.path.join(data, name)
            if not _NESTED_DATA_RE.search(name) or not os.path.isdir(nested):
                continue
            log("Nested server/data layout found (%s) -- hoisting its files one level up." % name)
            for entry in sorted(os.listdir(nested)):
                src = os.path.join(nested, entry)
                is_data_dir = entry in _NESTED_DATA_DIRS and os.path.isdir(src)
                is_version = entry.startswith("server_data_version") and entry.endswith(".txt") and os.path.isfile(src)
                if not (is_data_dir or is_version):
                    continue
                moved += _hoist(src, os.path.join(data, entry), "server/data/" + entry, owned_dsts, log)
            try:
                os.rmdir(nested)
            except OSError:
                log("  %s is not empty after the hoist -- left in place (nothing the server reads)." % name)
    except OSError as e:
        log("WARNING: could not finish the server/data layout repair (%s) -- continuing; %d files moved so far."
            % (e, moved))
    return moved


def ensure_data_layout(version, job):
    """heal_nested_server_data for a configured version, with the payload manifest's dst paths as
    the owned set. Runs before every bootstrap/provision/start/txt-fix and right after a stack
    download -- a stack extracted by hand from the 2.8 archive is healed on its first Prepare."""
    d = stack_dir(version)
    man = read_manifest(version) or {}
    owned = {str(item.get("dst", "")).replace("\\", "/") for item in man.get("files", []) if item.get("dst")}
    try:
        n = heal_nested_server_data(d, owned, job.log)
    except Exception as e:  # noqa: BLE001 -- a layout check must never fail the operation
        job.log("WARNING: could not check the server/data layout (%s) -- continuing." % e)
        return 0
    if n:
        job.log("Healed the nested server/data layout (%d files hoisted)." % n)
    return n


# -- jobs --

class Job:
    """A long operation with a streamed log. Clients poll /jobs/<id>?since=<next>."""

    def __init__(self, kind, version, job_id=None):
        self.id = job_id or "%d-%s" % (int(time.time() * 1000),
                                       "".join(random.choice(string.hexdigits[:16]) for _ in range(4)))
        self.kind = kind
        self.version = version
        self.state = "running"  # running | done | error
        self.error = None
        self.error_status = None  # HTTP status of the AgentError that failed the job (if any)
        self.error_code = None    # its public code (PUBLIC_ERROR_CODES), if it carried one
        self.result = None
        self.started = time.time()
        self.finished = None
        # True once a job only touches files no other job touches (the voice-pack download): the
        # watchdog's jobs and player signups may then start beside it (current_job(skip_yielding)).
        self.yielding = False
        self._lines = []
        self._lock = threading.Lock()

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + str(msg).rstrip()
        with self._lock:
            self._lines.append(line)
            # A runaway `docker compose` build must not eat the box's RAM.
            if len(self._lines) > 4000:
                del self._lines[:1000]
        log_line("gio-agent[%s] %s" % (self.kind, line))

    def snapshot(self, since=0):
        with self._lock:
            total = len(self._lines)
            since = max(0, min(int(since or 0), total))
            lines = self._lines[since:]
        return {
            "id": self.id, "kind": self.kind, "version": self.version, "state": self.state,
            "error": self.error, "result": self.result, "lines": lines, "next": total,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }

    def public_snapshot(self):
        """What a token-less caller may see: no log lines, no paths, a generic 5xx message -- and,
        for the same reason, a generic 5xx code (a 5xx reason is never more specific than its text)."""
        err = code = None
        if self.state == "error":
            if (self.error_status or 500) < 500:
                err, code = self.error, self.error_code
            else:
                err, code = "server error, try later", "server_error"
        res = self.result if isinstance(self.result, dict) else {}
        return {
            "id": self.id, "kind": self.kind, "state": self.state,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "error": err, "errorCode": code,
            "result": {k: res.get(k) for k in ("name", "uid", "template", "nickname")}
            if self.state == "done" else None,
        }


JOBS = {}
JOBS_ORDER = []
# Player-facing jobs (signup) live in their own registry: unguessable ids, never listed by the
# admin /jobs, expired after PUBLIC_JOB_TTL.
PUBLIC_JOBS = {}
PUBLIC_JOBS_ORDER = []
PUBLIC_JOB_CAP = 50
PUBLIC_JOB_TTL = 900
_jobs_lock = threading.Lock()
# Everything that touches docker for a stack serialises here: two concurrent `compose up` runs on
# stacks that share ports and a subnet would fight each other.
_op_lock = threading.Lock()


# What the agent starts on its own (the service / tower / advertise watchdogs pass
# beside_yielding=True) and the player signups do not wait for a YIELDING job -- a crashed
# gameserver must not stay down, nor every signup answer server_busy, for the hour a voice-pack
# download can take. Everything an admin starts still waits for it.
_voice_cancel = threading.Event()  # POST /server/hotpatch/voice {"cancel": true}
_fetch_cancel = threading.Event()  # POST /server/fetch {"cancel": true} (the stack download job)
_relocate_cancel = threading.Event()  # POST /server/relocate {"cancel": true} (a cross-volume copy)
# POST /agent/restart accepted (set under _jobs_lock together with its no-job check): from then on
# no job of any kind starts -- the process is about to go away under it.
_restarting = False


def _busy_error(busy):
    return AgentError(409, "Another operation is already running on the server (%s, version %s). "
                           "Wait for it to finish." % (busy.kind, busy.version))


def _restarting_error():
    return AgentError(409, "The agent is restarting -- try again in a few seconds.")


def _current_job_locked(skip_yielding=False):
    for reg, order in ((JOBS, JOBS_ORDER), (PUBLIC_JOBS, PUBLIC_JOBS_ORDER)):
        for jid in reversed(order):
            if reg[jid].state == "running" and not (skip_yielding and reg[jid].yielding):
                return reg[jid]
    return None


def current_job(skip_yielding=False):
    """The running job, admin or public -- both registries share the single job slot.
    skip_yielding=True (the watchdogs' + the public starters) looks past a job in its yielding phase."""
    with _jobs_lock:
        return _current_job_locked(skip_yielding)


def _run_job(job, fn, lock=True):
    """lock=False: fn takes _op_lock itself, for the part of its work that needs it."""
    def runner():
        try:
            if lock:
                with _op_lock:
                    job.result = fn(job)
            else:
                job.result = fn(job)
            job.state = "done"
            job.log("DONE.")
        except AgentError as e:
            job.state, job.error, job.error_status, job.error_code = "error", e.message, e.status, e.code
            job.log("ERROR: " + e.message)
        except Exception as e:  # noqa: BLE001 -- a job must never take the agent down
            job.state, job.error, job.error_status = "error", str(e), 500
            job.log("ERROR: " + str(e))
        finally:
            job.finished = time.time()
            # Whatever the job did (start/stop/provision/...), the cached /public/status snapshot
            # must not keep telling players the pre-job state for another PUBLIC_STATUS_TTL seconds.
            _invalidate_public_status()

    threading.Thread(target=runner, daemon=True).start()


def start_job(kind, version, fn, lock=True, beside_yielding=False):
    """Run fn(job) on a worker thread. Returns the Job immediately; 409 while another job runs
    (beside_yielding=True -- the watchdogs -- looks past a job in its yielding phase). The busy
    check and the registration are one step: a job that does not queue behind _op_lock
    (lock=False) would otherwise let two simultaneous POSTs both pass."""
    job = Job(kind, version)
    with _jobs_lock:
        restarting = _restarting
        busy = None if restarting else _current_job_locked(skip_yielding=beside_yielding)
        if busy is None and not restarting:
            JOBS[job.id] = job
            JOBS_ORDER.append(job.id)
            while len(JOBS_ORDER) > 20:
                # only FINISHED jobs leave the registry: since 3.3 watchdog jobs register beside a
                # running voice job, and trimming that one would blind /status, cancel and every 409
                old = next((j for j in JOBS_ORDER if JOBS[j].state != "running"), None)
                if old is None:
                    break
                JOBS_ORDER.remove(old)
                JOBS.pop(old, None)
    if restarting:
        raise _restarting_error()
    if busy is not None:
        raise _busy_error(busy)
    _run_job(job, fn, lock=lock)
    return job


def _purge_public_jobs(now=None):
    """Caller holds _jobs_lock. Drop finished public jobs older than the TTL and enforce the cap."""
    now = time.time() if now is None else now
    keep = []
    for jid in PUBLIC_JOBS_ORDER:
        j = PUBLIC_JOBS[jid]
        if j.state != "running" and now - (j.finished or j.started) > PUBLIC_JOB_TTL:
            PUBLIC_JOBS.pop(jid, None)
        else:
            keep.append(jid)
    while len(keep) > PUBLIC_JOB_CAP:
        PUBLIC_JOBS.pop(keep.pop(0), None)
    PUBLIC_JOBS_ORDER[:] = keep


def public_busy_error():
    """The job slot is taken: the only public 429 a launcher retries on its own (code server_busy)."""
    return AgentError(429, "the server is busy with another operation, try again shortly",
                      retry_after=30, code="server_busy")


def start_public_job(kind, version, fn):
    """Same single slot as start_job, but a busy agent answers 429 server_busy + retryAfter (the
    player's client simply retries) and the id is unguessable."""
    job = Job(kind, version, job_id=secrets.token_urlsafe(16))
    with _jobs_lock:
        restarting = _restarting
        busy = None if restarting else _current_job_locked(skip_yielding=True)
        if busy is None and not restarting:
            _purge_public_jobs()
            PUBLIC_JOBS[job.id] = job
            PUBLIC_JOBS_ORDER.append(job.id)
    if busy is not None or restarting:
        raise public_busy_error()  # a restarting agent answers again in seconds: the launcher's retry fits
    _run_job(job, fn)
    return job


def public_job(job_id):
    with _jobs_lock:
        _purge_public_jobs()
        return PUBLIC_JOBS.get(job_id)


# -- docker / compose --

class _Failed:
    """Stand-in result when the executable is missing, so the agent degrades instead of crashing."""

    def __init__(self, msg):
        self.returncode, self.stdout, self.stderr = 127, "", msg


# docker compose prints its own notices on stderr -- with the vendor compose file, EVERY call carries
# `time="..." level=warning msg="<dir>/docker-compose.yml: the attribute `version` is obsolete ..."`.
# They are not failures, and an error message quoting raw stderr used to be nothing BUT that notice,
# with the real reason cut off by the length limit (2026-09-23: "Writing the progress failed: <the
# obsolete-version warning>" while MariaDB had actually crashed under the INSERT). Only the schema
# notices of the vendor's file are dropped -- a compose warning that matters ("The X variable is not
# set", "Found orphan containers") must stay in the job log and in the message.
_CMD_NOISE_RE = re.compile(r'^\s*(?:time="[^"]*"\s+level=(?:warning|info|debug)\s|(?:WARN|INFO|DEBU)\[\d+\])'
                           r'.*\b(?:is obsolete|is deprecated)\b')


def _clean_cmd_output(text):
    """`text` without the tool's own schema notices. Pure."""
    return "\n".join(l for l in (text or "").splitlines() if not _CMD_NOISE_RE.match(l)).strip()


def _cmd_err(r, limit=300, fallback=""):
    """Why a finished command failed, for a user-facing message: stderr without the tool's notices,
    else its stdout, else the notices after all (better than an empty reason) -- the TAIL of it, since
    that is where the real error sits after the echoes."""
    text = (_clean_cmd_output(getattr(r, "stderr", "")) or _clean_cmd_output(getattr(r, "stdout", ""))
            or (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip() or fallback)
    return ("..." + text[-limit:]) if len(text) > limit else text


def _run(args, cwd=None, timeout=T_COMPOSE, stdin_path=None, input_text=None):
    try:
        stdin = open(stdin_path, "rb") if stdin_path else None
        try:
            return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                  stdin=stdin, input=None if stdin else input_text,
                                  **_popen_kwargs())
        finally:
            if stdin:
                stdin.close()
    except FileNotFoundError:
        return _Failed(f"executable not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return _Failed(f"timed out ({timeout}s): {' '.join(args[:4])}")


def _stream(args, job, cwd=None, timeout=T_COMPOSE, prefix=""):
    """Run a command, streaming merged stdout+stderr into the job log. Returns the exit code."""
    job.log("$ " + " ".join(args))
    try:
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, errors="replace", **_popen_kwargs())
    except FileNotFoundError:
        job.log(prefix + "executable not found: " + args[0])
        return 127
    # A timer, not a deadline check inside the read loop: a command that hangs without printing
    # anything (a wedged docker daemon) would otherwise block this thread -- and _op_lock with it --
    # until the agent is restarted.
    killed = []

    def _kill():
        killed.append(True)
        try:
            p.kill()
        except Exception:  # noqa: BLE001 -- already gone
            pass

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        for line in p.stdout:
            if _CMD_NOISE_RE.match(line):
                continue  # the tool's own notices, not output of what we asked for
            job.log(prefix + line.rstrip())
        rc = p.wait()
    finally:
        watchdog.cancel()
    if killed:
        job.log(prefix + "TIMEOUT (%ds) -- the command was killed." % timeout)
        return 124
    return rc


def _compose(directory, *args, **kw):
    return _run(["docker", "compose", "--project-directory", directory, *args], **kw)


def _compose_stream(directory, job, *args, **kw):
    return _stream(["docker", "compose", "--project-directory", directory, *args], job, **kw)


def _parse_ls_json(text):
    """`docker compose ls --format json` prints one JSON array today, but `ps` already switched to
    NDJSON once (compose v2.21) -- accept both shapes here too so a future compose cannot blind the
    up-checks and watchdogs. Returns the project-name list, or None when the output is unreadable --
    the caller must treat None as "docker is mute", never as "nothing is running"."""
    entries = _parse_ls_entries(text)
    return None if entries is None else [e.get("Name") for e in entries]


def _parse_ls_entries(text):
    """The entry dicts of `docker compose ls --format json` (either shape), or None when unreadable."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        entries = data if isinstance(data, list) else [data]
    except ValueError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                return None
    if not all(isinstance(e, dict) for e in entries):
        return None
    return entries


def running_projects():
    ls = _run(["docker", "compose", "ls", "--format", "json"], timeout=60)
    if ls.returncode != 0:
        return None, (ls.stderr or "docker unavailable").strip()
    names = _parse_ls_json(ls.stdout)
    if names is None:
        return None, "could not parse docker compose output"
    return names, None


def compose_projects_all():
    """([(name, config files -- comma-separated)] of EVERY compose project docker knows, stopped ones
    included (`ls -a`), None) -- or (None, why) when docker does not answer. The stack relocation checks a
    new folder's project name against it."""
    ls = _run(["docker", "compose", "ls", "-a", "--format", "json"], timeout=60)
    if ls.returncode != 0:
        return None, (ls.stderr or "docker unavailable").strip()
    entries = _parse_ls_entries(ls.stdout)
    if entries is None:
        return None, "could not parse docker compose output"
    return [(str(e.get("Name") or ""), str(e.get("ConfigFiles") or "")) for e in entries], None


def _parse_ps_json(text):
    """`docker compose ps -a --format json` prints NDJSON (one object per line) on modern compose
    and a JSON array on older ones. Returns {service: state-lowercased} or None when the output is
    unreadable -- the caller must treat None as "docker is mute", never as "everything is down"."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        entries = data if isinstance(data, list) else [data]
    except ValueError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                return None
    out = {}
    for e in entries:
        if isinstance(e, dict) and e.get("Service"):
            out[e["Service"]] = str(e.get("State") or "").lower()
    return out


def _dead_from(expected, states):
    """Every declared service that is not literally "running" -- exited, restarting, created, paused
    and never-created all count: none of them is serving players."""
    return [s for s in expected if (states or {}).get(s, "") != "running"]


def dead_services(version):
    """(dead, states) for a stack, or (None, None) when docker cannot answer. A blind read must
    never be acted on: reviving on a docker hiccup could resurrect a stack someone is stopping."""
    d = stack_dir(version)
    ps = _compose(d, "ps", "-a", "--format", "json", timeout=60)
    if ps.returncode != 0:
        return None, None
    states = _parse_ps_json(ps.stdout)
    if states is None:
        return None, None
    cfg = _compose(d, "config", "--services", timeout=60)
    expected = sorted(s.strip() for s in (cfg.stdout or "").splitlines() if s.strip()) \
        if cfg.returncode == 0 else []
    if not expected:
        expected = sorted(states)
    # Belt and braces over compose's own profile filtering: a service carrying the `donotstart`
    # profile (the vendor's oaserver, a switched-off pathfindingserver) is never expected, whatever
    # `config --services` printed -- and the `ps` fallback above would otherwise count a container
    # that outlived the profile edit.
    off = compose_disabled_services_of(version)
    if off:
        expected = [s for s in expected if s not in off]
    return _dead_from(expected, states), states


def _service_error_tail(version, service):
    """The GIO services log to files under server/<svc>/log, not to stdout (`docker logs` is
    empty) -- pull the last error lines so a failed start says WHY, not just which service."""
    path = os.path.join(stack_dir(version), "server", service, "log", service + ".error.log")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        if lines:
            return " Last errors from %s:\n  %s" % (service, "\n  ".join(lines[-3:]))
    except OSError:
        pass
    r = _compose(stack_dir(version), "logs", "--tail", "3", "--no-color", service, timeout=60)
    tail = (r.stdout or "").strip()
    if tail:
        return " Last lines from docker logs:\n  " + "\n  ".join(tail.splitlines()[-3:])
    return ""


def verify_stack(version, job, rounds=REVIVE_ROUNDS):
    """Watch a freshly-started stack until every service survives T_STABLE seconds of quiet,
    `up -d`-ing the fallen ones -- the automated version of "run ./start.sh once more". Fails the
    job (with the culprit's own error log) only when a game-critical service refuses to stay up
    after `rounds` resurrections; without this a start "succeeds" into a white screen."""
    d = stack_dir(version)
    revives = 0
    job.log("Checking that the services stay up (%ds of quiet)..." % T_STABLE)
    while True:
        t0 = time.time()
        dead = states = None
        for offset in STABLE_PROBES:
            time.sleep(max(0.0, offset - (time.time() - t0)))
            dead, states = dead_services(version)
            if dead is None:
                job.log("Cannot read the container states -- skipping the stability check.")
                return None
            if dead:
                break
        if not dead:
            job.log("All %d services stayed up for %ds -- the server is stable."
                    % (len(states), T_STABLE))
            return {"stable": True, "revived": revives}
        critical = [s for s in dead if s not in NONCRITICAL_SERVICES]
        if revives >= rounds:
            if not critical:
                job.log("Services %s will not stay up, but the game runs without them -- leaving "
                        "them." % ", ".join(dead))
                return {"stable": True, "revived": revives, "ignored": dead}
            raise AgentError(500, "Service %s stops by itself shortly after starting, even after %d "
                                  "restarts -- the server is NOT functional.%s"
                             % (critical[0], revives, _service_error_tail(version, critical[0])))
        revives += 1
        job.log("Services down after start: %s -- restarting them (attempt %d/%d)."
                % (", ".join(dead), revives, rounds))
        rc = _compose_stream(d, job, "up", "-d", prefix="  ")
        if rc != 0:
            raise AgentError(500, "docker compose up (restarting the fallen services) failed (code %d)." % rc)


# The job kinds that never stop a service: while one of them runs, status() keeps reporting
# servicesDown/healthy (see the comment there). A start/stop-class job hides that report -- `relocate`
# is one (it brings the stack down, moves it and starts it again), so it is deliberately not listed.
SERVICE_REPORT_JOBS = ("revive", "signup", "accountcreate", "templates", "password", "auth",
                       "hotpatch-voice", "fetch")


def status():
    out = {"versions": {}, "running": [], "agent": AGENT_VERSION,
           "platform": "windows" if IS_WINDOWS else "linux",
           "advertisedIp": ADVERTISED_IP, "provisionMode": PROVISION_MODE,
           "txtFixesMode": TXT_FIXES_MODE,
           # GIO_ADVERTISED_HOST (null without one): the last lookup, why it failed, and an answer
           # waiting for its confirming second check. Admin only -- never in /public/status.
           "advertisedHost": ADVERTISED_HOST or None, "advertisedCheckedAt": _ADVERTISE["checkedAt"],
           # advertisedError: the lookup's, else the follow job that keeps failing (see _follow_failed)
           "advertisedError": _ADVERTISE["error"] or follow_error(), "advertisedPending": _ADVERTISE["pending"]}
    running, err = running_projects()
    if err:
        out["error"] = err
    running = running or []
    # Never a 500 over a broken state file: every state-derived fact becomes unknown (null) and the
    # "state" block says why -- the admin's screen is where the repair starts.
    try:
        st, state_err = load_state(), None
    except StateUnreadable as e:
        st, state_err = {}, e.reason
    known = state_err is None
    busy_job = current_job()
    for v, meta in VERSIONS.items():
        vs = (st.get("versions") or {}).get(v) or {}
        # A record lost with the state file still counts when the stack's marker holds it: the same
        # value needs_provision re-seeds at the next Start, so the generation does not jump either.
        marker = read_provisioned_marker(v) if known else None
        prov_at = vs.get("provisionedAt") or (marker["at"] if marker else None)
        # The progress record (agent 3.4) follows the same rule: the state's, else the marker's --
        # the marker is what needs_provision re-seeds from, so /status never disagrees with it.
        progress = vs.get("progress") if vs.get("progress") in PROGRESS_MODES else (marker["progress"] if marker else None)
        default_account = (bool(vs.get("defaultAccount")) if "defaultAccount" in vs
                           else (marker["defaultAccount"] if marker else None)) if known else None
        man = read_manifest(v)
        present = is_present(v)
        hp = hotpatch_brief(v, vs)
        if not known:
            hp.update(enabled=None, pending=None)
        entry = {
            "project": meta["project"],
            "dir": meta["dir"],
            "configured": bool(meta["dir"]),
            "up": bool(meta["dir"]) and meta["project"] in running,
            "present": present,
            # A loopback-poisoned render reports NOT bootstrapped: the app's only card with a
            # "Prepare server" button is the not-ready one, and setup is exactly the call whose
            # heal repairs this state -- otherwise the UI offers only Start, which 409s forever.
            "bootstrapped": present and is_bootstrapped(v) and not compose_binds_loopback(v),
            "provisioned": bool(prov_at) if known else None,
            "provisionedAt": prov_at,
            # agent 3.4: the progress mode of the last provision (null = never / unknown) and whether
            # the shipped save is in THIS database (null while the state file is unreadable)
            "progress": progress if known else None,
            "defaultAccount": default_account,
            "txtFixes": vs.get("txtFixes", "none") if known else None,
            "advertisedIp": vs.get("advertisedIp"),
            "advertisedIpSql": vs.get("advertisedIpSql"),
            "hasPayload": man is not None,
            # The manifest account is reported only when the save IS in this database (or nobody can
            # tell -- unreadable state keeps the old answer so an old launcher is not worse off):
            # /public/status turns a null into "" and the launcher hides the login.
            "account": (man or {}).get("account") if (default_account is None or default_account) else None,
            "templates": (vs.get("templates") or {}) if known else None,
            "passwordVerify": bool(vs.get("passwordVerify")) if known else None,
            "hotpatch": hp,
            # the ready-made package download (agent 3.4): admin only, NEVER in /public/status
            "fetch": fetch_brief(v, vs),
            # agent 3.4: what the compose files say (rendered first); null = not present / not in the file
            "pathfinding": pathfinding_enabled(v) if present else None,
            # agent 3.4: is muipserver still on the archive's published sign key? (never the key)
            "muipKey": (("vendor" if is_vendor_muip_key(_read_sign_key_file(muip_key_file(v))) else "custom")
                        if present else None),
            "muipChangedAt": (vs.get("secrets") or {}).get("muipChangedAt") if known else None,
        }
        out["versions"][v] = entry
        if entry["up"] and (busy_job is None or busy_job.kind in SERVICE_REPORT_JOBS):
            # (hotpatch-voice only ever touches the mirror folder -- and runs for up to an hour;
            # fetch only ever touches an ABSENT stack's archive + temp folder)
            # "up" only says the compose project exists -- the game services exit 0 when they die
            # and stay dead, so tell the client WHICH services are actually serving right now.
            # Not while a start/stop-class job runs: mid-start the staged boot has only mysql/redis
            # up and mid-stop the down fells services one by one -- both would read as a false
            # DEGRADED. A *revive* job is the opposite case and must NOT hide the report: it exists
            # precisely because services are dead, and hiding them flipped the badge to LIVE and let
            # PLAY launch into the white screen for the whole repair window. Jobs that never stop
            # anything (SERVICE_REPORT_JOBS: signup, accountcreate, templates, password, auth) do not
            # hide it either. Only the game-critical dead go in servicesDown -- the watchdog
            # deliberately never revives adminer/phpmyadmin (restart policy "no"), and reporting
            # them would pin a false DEGRADED with a false "being repaired" promise forever.
            dead, _states = dead_services(v)
            if dead is not None:
                crit = [s for s in dead if s not in NONCRITICAL_SERVICES]
                entry["servicesDown"] = crit
                entry["healthy"] = not crit
                extra = [s for s in dead if s in NONCRITICAL_SERVICES]
                if extra:
                    entry["servicesIgnored"] = extra
    out["running"] = running
    out["lastStarted"] = st.get("lastStarted")
    out["busy"] = None if busy_job is None else {"id": busy_job.id, "kind": busy_job.kind,
                                                 "version": busy_job.version}
    out["policy"] = get_policy()
    # agent 3.4: seeds the Prepare dialog's pathfinding toggle for a stack nobody decided about yet
    out["pathfindingDefault"] = PATHFINDING_DEFAULT
    out["state"] = state_report(state_err, st)
    return out


def down_all(job, keep=None, grace=None):
    """Stop every stack we could possibly have started. Never touches a stack that isn't set up.

    Each game service declares stop_grace_period: 30s and ignores SIGTERM, and compose walks them in
    dependency order -- a full down of a live stack takes minutes. That wait is the price of letting
    the gameserver flush player data, so only pass `grace` when nobody can possibly have played yet."""
    extra = ["-t", str(grace)] if grace is not None else []
    for v, meta in VERSIONS.items():
        if v == keep or not is_present(v) or not is_bootstrapped(v):
            continue
        job.log("Stopping the %s stack..." % v)
        # BEFORE the down: from here on this stack is in transition, and a transition the job does
        # not finish must never look like "supposed to be up" to the service watchdog. Best effort:
        # a stop must work over an unreadable state file, and the watchdog skips while it is.
        version_state_set(v, best_effort=True, runningOk=False)
        _compose_stream(meta["dir"], job, "down", "--remove-orphans", *extra, prefix="  ")


# -- mysql --

def mysql_password(version):
    pw = read_env(version).get("MYSQL_ROOT_PASSWORD", "")
    if not pw:
        raise AgentError(500, "MYSQL_ROOT_PASSWORD not found in the %s stack's .env." % version)
    return pw


def _mysql_args(version, db=None, password=None):
    d = stack_dir(version)
    return ["docker", "compose", "--project-directory", d, "exec", "-T",
            "-e", "MYSQL_PWD=" + (password if password is not None else mysql_password(version)),
            "mysql", "mysql", "-uroot", "--default-character-set=utf8mb4"] + ([db] if db else [])


def mysql_exec(version, sql, db=None, timeout=300, password=None):
    return _run(_mysql_args(version, db, password) + ["-e", sql], timeout=timeout)


def mysql_import(version, sql_path, db, timeout=1800):
    return _run(_mysql_args(version, db), stdin_path=sql_path, timeout=timeout)


def mysql_reachable(version):
    return mysql_exec(version, "SELECT 1", timeout=30).returncode == 0


def wait_for_mysql(version, job, timeout=T_MYSQL_READY):
    job.log("Waiting for MySQL...")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = mysql_exec(version, "SELECT 1", timeout=30)
        if r.returncode == 0:
            job.log("MySQL is ready.")
            return
        last = _cmd_err(r, 200).splitlines()[-1:] or [""]
        last = last[0]
        time.sleep(3)
    raise AgentError(500, "MySQL did not come up within %ds. Last error: %s" % (timeout, last))


# -- payload install --

def validate_tsv(path):
    """The server's excel-config .txt files are TSV. A row with the wrong number of columns makes the
    gameserver reject the whole file and crash ~50s after start (the classic NewActivityCondData
    line-217 bug: a tab that became two spaces). Repair what we safely can and refuse to install
    anything still malformed."""
    raw = open(path, "rb").read().decode("utf-8", "replace")
    lines = raw.split("\n")
    if not lines:
        return raw, 0, []
    expected = lines[0].count("\t")
    repaired = 0
    for i, line in enumerate(lines):
        if not line.strip() or line.count("\t") == expected:
            continue
        candidate = re.sub(r"  +", "\t", line)
        if candidate.count("\t") == expected:
            lines[i] = candidate
            repaired += 1
    bad = [i + 1 for i, l in enumerate(lines) if l.strip() and l.count("\t") != expected]
    return "\n".join(lines), repaired, bad


def _stat_or_none(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def _restore_owner_mode(dst, prev):
    """A payload install replaces a stack file's CONTENT, never its identity. shutil.copy2 stamps
    the PAYLOAD's mode (644, uploaded from Windows) and the agent's owner (root) onto the stack
    file, which breaks any container process running as non-root that must rewrite it afterwards --
    the redis dump belongs to the container's uid 999, and a root-owned dump.rdb in a root dir
    stops BGSAVE, which with stop-writes-on-bgsave-error makes redis refuse writes. So: an
    overwritten file gets its previous mode and owner back; a NEW file gets 0644 and its
    directory's owner, so a file dropped into a container-owned data dir belongs to that
    container's user, not to root. Never fatal -- a filesystem that refuses chown (or Windows)
    must not fail the install."""
    try:
        if prev is not None:
            os.chmod(dst, prev.st_mode & 0o7777)
            if hasattr(os, "chown"):
                os.chown(dst, prev.st_uid, prev.st_gid)
        else:
            os.chmod(dst, 0o644)
            if hasattr(os, "chown"):
                parent = os.stat(os.path.dirname(dst) or ".")
                os.chown(dst, parent.st_uid, parent.st_gid)
    except OSError:
        pass


def parse_progress(raw):
    """The `progress` field of /server/setup and /server/provision: None / "" is "default" (older
    launchers send nothing), anything else must be one of PROGRESS_MODES -> 400."""
    if raw is None or raw == "":
        return "default"
    if not isinstance(raw, str) or raw.strip().lower() not in PROGRESS_MODES:
        raise AgentError(400, "progress must be one of default, keep, fixes")
    return raw.strip().lower()


def effective_stage(item):
    """The stage a manifest `files[]` entry belongs to: `save` (the progress -- sdk.db, dump.rdb;
    installed only by a default provision), `config` (server data files installed in every mode and
    never reverted) or `txt_fix` (the reversible fixes of /server/txtfixes). A `save` entry flagged
    `tsv` is a server data file, not progress (the 2.8 MaterialDeleteData.txt shipped that way before
    the manifests named the `config` stage), so it counts as `config` -- keeps a box whose payloads
    were not redeployed correct in keep/fixes mode."""
    stage = item.get("stage", "save")
    return "config" if stage == "save" and item.get("tsv") else stage


def install_files(version, job, stages):
    """Copy payload files into the stack. The stack MUST be down (redis rewrites dump.rdb on exit)."""
    man = read_manifest(version)
    if not man:
        job.log("No payload for version %s -- skipping files." % version)
        return 0
    src_root, dst_root = payload_dir(version), stack_dir(version)
    count = 0
    for item in man.get("files", []):
        if effective_stage(item) not in stages:
            continue
        src = os.path.join(src_root, item["src"])
        dst = os.path.join(dst_root, item["dst"])
        if not os.path.isfile(src):
            raise AgentError(500, "Payload file missing: " + src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        prev = _stat_or_none(dst)
        # Keep the stack's own copy the first time we overwrite it, so txt fixes can be reverted.
        backup = dst + ".orig"
        if prev is not None and not os.path.isfile(backup):
            shutil.copy2(dst, backup)
        if item.get("tsv"):
            text, repaired, bad = validate_tsv(src)
            if bad:
                raise AgentError(500, "File %s has a wrong column count on lines %s -- not installing "
                                      "it, it would crash the gameserver." % (item["src"], bad[:5]))
            with open(dst, "wb") as f:
                f.write(text.encode("utf-8"))
            job.log("  %s%s" % (item["dst"], "  (repaired %d row)" % repaired if repaired else ""))
        else:
            shutil.copy2(src, dst)
            job.log("  " + item["dst"])
        _restore_owner_mode(dst, prev)
        count += 1
    return count


def revert_files(version, job, stages):
    man = read_manifest(version) or {}
    dst_root = stack_dir(version)
    count = 0
    for item in man.get("files", []):
        if effective_stage(item) not in stages:
            continue
        dst = os.path.join(dst_root, item["dst"])
        backup = dst + ".orig"
        if os.path.isfile(backup):
            prev = _stat_or_none(dst)
            shutil.copy2(backup, dst)
            _restore_owner_mode(dst, prev)
            job.log("  restored " + item["dst"])
            count += 1
    return count


# -- advertised IP (so people outside the LAN can actually connect) --

IP_RE = r"(?<![\d.])%s(?![\d.])"
DISPATCH_OUTER_IP_RE = re.compile(r"""outer_ip\s*(?:=\s*["']|>)\s*(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])""")


def dispatch_outer_ip(version):
    """The IPv4 address dispatch.xml's outer_ip holds on disk ('' when the file is missing, unreadable
    or holds none) -- attribute or element form."""
    path = os.path.join(stack_dir(version), "server", "dispatch", "conf", "dispatch.xml")
    try:
        m = DISPATCH_OUTER_IP_RE.search(read_text(path))
        return str(ipaddress.IPv4Address(m.group(1))) if m else ""
    except (OSError, ValueError):
        return ""


ADVERTISED_SQL_OWED_MAX = 16  # old IPs remembered for the database half (a bound, not a real limit)


def _sql_ip_rewrite(col, olds, new):
    """One SQL expression rewriting every IP of `olds` in `col` to `new`.

    Plain REPLACE has no IP-boundary anchor, and an old IP can sit INSIDE the new one ('203.0.113.1'
    in '203.0.113.17'): REPLACE over a row that already holds the new IP would turn it into
    '203.0.113.177'. Such rows are routine once an old IP is re-run after a database pass that
    failed half-way. So the new IP is parked behind a placeholder (no digit, no dot: nothing an IP
    can match) before the olds are rewritten, and put back last -- in the same statement, so a
    failure can never leave a placeholder behind. Olds that CONTAIN the new IP are rewritten before
    it is parked (parking would split them). Longest first inside each group: a prefix old
    ('192.0.2.5') run before a longer one ('192.0.2.57') would corrupt it."""
    ph = "{relic-advertised}"
    order = lambda ips: sorted(ips, key=lambda ip: (-len(ip), ip))  # noqa: E731
    expr = col
    for old in order(o for o in olds if new in o):
        expr = "REPLACE(%s, '%s', '%s')" % (expr, _sql_q(old), ph)
    expr = "REPLACE(%s, '%s', '%s')" % (expr, _sql_q(new), ph)
    for old in order(o for o in olds if new not in o):
        expr = "REPLACE(%s, '%s', '%s')" % (expr, _sql_q(old), ph)
    return "REPLACE(%s, '%s', '%s')" % (expr, ph, _sql_q(new))


def advertised_sql_current(version, vs=None):
    """The database half of the advertised-IP rewrite holds the effective IP (nothing owed)."""
    vs = version_state(version) if vs is None else vs
    return not ADVERTISED_IP or (vs.get("advertisedIpSql") == ADVERTISED_IP
                                 and not vs.get("advertisedIpSqlOwed"))


def apply_advertised_ip(version, job, with_sql=True):
    """Re-point everything the client is TOLD to connect to at the WAN IP.

    .env OUTER_IP stays the machine's LAN IP on purpose -- docker binds to it, and binding to a WAN
    address the box does not own fails with "cannot assign requested address". Bind and advertised
    address are decoupled by design; this is the advertised half.

    The two halves are recorded apart because they do not always run together: `advertisedIp` =
    what the XML files were rewritten to; `advertisedIpSql` = what the database URL columns were
    rewritten to, recorded only when with_sql ran and every UPDATE succeeded (a table this version
    does not have counts as done). Until then the IPs the database may still hold stay in
    `advertisedIpSqlOwed`. Before the split, an XML-only pass (netfix on a stopped stack, a secrets
    re-render, a start whose MySQL never answered) recorded the new IP, the next database pass no
    longer knew the old WAN IP, and dispatch_url + the gacha URLs kept it for good."""
    if not ADVERTISED_IP:
        if ADVERTISED_HOST:
            job.log("GIO_ADVERTISED_HOST %s has not resolved to a usable address yet -- the stack keeps "
                    "advertising what it had." % ADVERTISED_HOST)
        else:
            job.log("GIO_ADVERTISED_IP is not set -- the server keeps advertising the IP from .env.")
        return 0
    adv = ADVERTISED_IP  # one value for the whole pass (it changes only under _op_lock anyway)
    lan = read_env(version).get("OUTER_IP", "").strip()
    vs = version_state(version)
    previous = (vs.get("advertisedIp") or "").strip()
    previous_sql = (vs.get("advertisedIpSql") or "").strip()
    owed = [ip for ip in (vs.get("advertisedIpSqlOwed") or []) if isinstance(ip, str) and ip.strip()]
    # XML pass NEVER rewrites loopback -- a conf entry may legitimately point at 127.0.0.1, and the
    # render guards upstream mean the XMLs are always produced from a non-loopback OUTER_IP anyway.
    # The address dispatch.xml really holds is an old IP too: a lost record (a state reset, a .bak
    # rollback past the pass that wrote it, a hand edit) must not leave the previous WAN IP in the XMLs
    # and -- through olds -- in the database for good.
    on_disk = dispatch_outer_ip(version)
    olds = sorted(ip for ip in {lan, previous, on_disk}
                  if ip and ip != adv and not _is_loopback(ip))
    # The DB can still hold the vendor 127.0.0.1 (data.sql imported before .env was repaired) -- the
    # SQL below touches only client-facing URL columns, where loopback is wrong for any reachable
    # server -- and every IP an earlier database pass did not finish replacing.
    sql_olds = sorted({*olds, "127.0.0.1", previous_sql, *owed} - {adv, ""}, key=lambda ip: (-len(ip), ip))
    if not olds and not (with_sql and sql_olds):
        job.log("The advertised IP is already %s." % adv)
        return 0
    job.log("Setting the advertised IP: %s -> %s" % (", ".join(olds or sql_olds), adv))

    # a) the XML configs the servers read (dispatch.xml's outer_ip is the critical one)
    conf_root = os.path.join(stack_dir(version), "server")
    touched = 0
    for entry in sorted(os.listdir(conf_root)) if os.path.isdir(conf_root) else []:
        conf = os.path.join(conf_root, entry, "conf")
        if not os.path.isdir(conf):
            continue
        for name in sorted(os.listdir(conf)):
            if not name.endswith(".xml"):
                continue
            path = os.path.join(conf, name)
            text = read_text(path)
            new = text
            for old in olds:
                new = re.sub(IP_RE % re.escape(old), adv, new)
            if new != text:
                write_text(path, new)
                job.log("  %s/%s" % (entry, name))
                touched += 1
    job.log("  %d XML files updated." % touched)

    # b) the same IP lives in the database (region dispatch url + the gacha/wish page urls)
    def sets(*cols):
        return ", ".join("%s = %s" % (c, _sql_ip_rewrite(c, sql_olds, adv)) for c in cols)

    sql_done = False
    if with_sql:
        sql_done = run_sql(version, job, [
            "UPDATE hk4e_db_deploy_config.t_region_config SET " + sets("dispatch_url"),
            "UPDATE hk4e_db_config.t_gacha_newbie_url_config SET " + sets("gacha_prob_url", "gacha_record_url"),
            "UPDATE hk4e_db_config.t_gacha_schedule_config SET " + sets(
                "gacha_prob_url", "gacha_record_url", "gacha_prob_url_oversea", "gacha_record_url_oversea"),
        ], tolerate=True, missing_ok=True)
    if sql_done:
        version_state_set(version, advertisedIp=adv, advertisedIpSql=adv, advertisedIpSqlOwed=[])
    else:
        # Whatever the database may still hold (a skipped pass: the last applied IPs; a failed one:
        # possibly any of them, row by row) is replaced by the next pass that runs.
        still = [ip for ip in (previous, previous_sql, *owed) if ip and ip != adv and not _is_loopback(ip)]
        still = list(dict.fromkeys(still))[:ADVERTISED_SQL_OWED_MAX]
        rec = {"advertisedIp": adv, "advertisedIpSqlOwed": still}
        if "advertisedIpSql" in vs or previous != adv or with_sql:
            # The database half is recorded as not done (null when no pass ever completed): a stack
            # advertised for the first time owes no old IP, so without it advertised_lags() would read
            # the failed pass as healed and the catch-up would never retry it. Only a skipped pass over
            # a pre-3.2 record that already vouches for this address keeps the record as it was.
            rec["advertisedIpSql"] = previous_sql or None
        version_state_set(version, **rec)
        if with_sql:
            job.log("WARNING: the database half of the advertised-IP rewrite did not complete -- %s "
                    "stay%s on the list the next pass (start or netfix) replaces."
                    % (", ".join(still) or "the old address", "" if len(still) > 1 else "s"))
    # The caller decides whether running services must be restarted to pick the files up.
    return touched


def events_open_sql(ids, open_until, closed_end, base=None):
    """The UPDATE that opens the listed events until `open_until`. Pure. begin_time: a future one
    becomes yesterday (the event runs from now on); one at or before the closed window's end --
    parked there by an earlier list -- becomes `base` ('YYYY-MM-DD HH:MM:SS') or yesterday; any
    other stays as it is."""
    yesterday = "DATE_SUB(NOW(), INTERVAL 1 DAY)"
    rebase = "'%s'" % base if base and _STAMP_RE.fullmatch(base) else yesterday
    return ("UPDATE t_activity_schedule_config SET begin_time = CASE WHEN begin_time > NOW() THEN %s "
            "WHEN begin_time <= '%s' THEN %s ELSE begin_time END, end_time = '%s' WHERE schedule_id IN (%s)"
            % (yesterday, closed_end, rebase, open_until, ids))


def apply_events(version, job):
    """Open exactly the GAA events and close everything else.

    bootstrap.sh's adjust.sql rebases EVERY row to start now, so a stack left alone comes up running
    events that belong to other patches entirely (Invitation of Windblume is a 1.4 event). And for
    2.8 the closing is not cosmetic: the GAA2 unlock quest only triggers once the 1.6 events it
    references have ended."""
    man = read_manifest(version) or {}
    ev = man.get("events")
    if not ev:
        return
    gaa = {str(k): v for k, v in (ev.get("gaa") or {}).items()}
    others = {str(k): v for k, v in (ev.get("others") or {}).items()}
    if not gaa:
        raise AgentError(500, "The %s manifest lists no GAA event." % version)
    open_until = ev.get("openUntil", EVER)
    closed = ev.get("closedWindow") or ["1998-06-09 10:00:00", "1998-06-28 03:59:59"]

    ids = ", ".join(sorted(gaa))
    job.log("GAA events open until %s:" % open_until[:10])
    for k in sorted(gaa):
        job.log("  %s  %s" % (k, gaa[k]))
    job.log("Closed events (window %s):" % closed[0][:10])
    for k in sorted(others):
        job.log("  %s  %s" % (k, others[k]))

    # An event this list opens that an earlier list had PARKED in the closed window (agent 3.7: 2.8's
    # 5083001 Reminiscent Regimen moved from 'others' to 'gaa') would keep its 1998 begin_time and run
    # at event day ~10,000 -- its end-of-event mail at once, and a 1998 start on the event page. It
    # joins the events already running instead (their earliest begin_time -- on a fresh stack the
    # bootstrap time, which it would have had anyway), as if it had opened with them; with none
    # running it starts now (yesterday, so its day-1 conditions hold).
    base = None
    r = mysql_exec(version, "SELECT MIN(begin_time) FROM t_activity_schedule_config WHERE schedule_id "
                            "IN (%s) AND begin_time > '%s' AND begin_time <= NOW()" % (ids, closed[1]),
                   db="hk4e_db_config")
    if r.returncode == 0 and _STAMP_RE.fullmatch(_scalar(r)):
        base = _scalar(r)
    r = mysql_exec(version, "SELECT schedule_id FROM t_activity_schedule_config WHERE schedule_id IN (%s) "
                            "AND begin_time <= '%s'" % (ids, closed[1]), db="hk4e_db_config")
    parked = [l.strip() for l in (r.stdout or "").splitlines()[1:] if l.strip().isdigit()] \
        if r.returncode == 0 else []
    if parked:
        job.log("Re-opening %s (parked in the closed window until now) from %s." % (
            ", ".join(parked), base or "yesterday"))

    run_sql(version, job, [
        "USE hk4e_db_config",
        events_open_sql(ids, open_until, closed[1], base),
        # Everything not on the GAA list, whether or not the manifest names it: a row nobody
        # classified must not silently end up running.
        "UPDATE t_activity_schedule_config SET begin_time = '%s', end_time = '%s' "
        "WHERE schedule_id NOT IN (%s)" % (closed[0], closed[1], ids),
    ])

    r = mysql_exec(version, "SELECT schedule_id, begin_time, end_time, LEFT(`desc`, 46) "
                            "FROM t_activity_schedule_config ORDER BY schedule_id",
                   db="hk4e_db_config")
    known = set(gaa) | set(others)
    unlisted = []
    for line in (r.stdout or "").splitlines()[1:]:
        cols = line.split("\t")
        if not cols or not cols[0].strip().isdigit():
            continue
        job.log("  %s  %s -> %s  %s" % tuple((cols + ["", "", ""])[:4]))
        if cols[0].strip() not in known:
            unlisted.append(cols[0].strip())
    if unlisted:
        job.log("WARNING: %s are not in the manifest -- closed them. Add them to 'gaa' if they "
                "belong to the archipelago." % ", ".join(unlisted))


_SQL_MISSING_OBJECT_RE = re.compile(r"\bERROR (1146|1049)\b")  # table / database doesn't exist


def run_sql(version, job, statements, tolerate=False, missing_ok=False):
    """Run statements one by one so a table that doesn't exist in this game version is a warning,
    not a dead provisioning run. Returns True when every statement succeeded; a tolerated failure
    makes it False. missing_ok=True: a table or database this version does not have (MySQL 1146 /
    1049) counts as success -- there is nothing in it to change."""
    db = None
    ok = True
    for stmt in statements:
        s = stmt.strip().rstrip(";")
        if not s:
            continue
        if s.upper().startswith("USE "):
            db = s[4:].strip().strip("`")
            continue
        r = mysql_exec(version, s, db=db)
        if r.returncode != 0:
            detail = _cmd_err(r, 600).splitlines()
            detail = detail[-1] if detail else "rc=%d" % r.returncode
            if missing_ok and _SQL_MISSING_OBJECT_RE.search(detail):
                job.log("  (not in this version) " + detail)
            elif tolerate:
                job.log("  (ignored) " + detail)
                ok = False
            else:
                raise AgentError(500, "SQL failed: %s\n%s" % (s[:120], detail))
        else:
            job.log("  ok: " + s[:110])
    return ok


# -- Spiral Abyss (TowerScheduleData.txt) --

# Relative to the stack dir -- the same data/txt folder the manifest's txt_fix entries target.
TOWER_TXT = os.path.join("server", "data", "txt", "TowerScheduleData.txt")
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _plus_years(stamp, years):
    """'2020-07-16 04:00:00' + N years, as a string. A shifted Feb 29 lands on Feb 28 when the
    target year is not leap -- not a case in the real files (their windows sit on the 1st/16th),
    but this must never emit a date the gameserver cannot parse."""
    y = int(stamp[:4]) + years
    out = "%04d%s" % (y, stamp[4:])
    if out[5:10] == "02-29" and (y % 4 or (y % 100 == 0 and y % 400)):
        out = out[:8] + "28" + out[10:]
    return out


def shift_tower_calendar(text, now):
    """Return (new_text, info): the Spiral Abyss calendar shifted forward by a whole number of
    years so that `now` falls inside it.

    TowerScheduleData.txt is a fixed list of half-month windows (the 1st and the 16th at 04:00)
    that simply ENDS -- 2021-08 on 1.6, 2022-09 on 2.8 -- and there is no tower schedule anywhere in
    the DB, so past that date the client shows the abyss closed and no round can be played. Adding
    whole years is the smallest change that reopens it: the biweekly rotation, the 1st/16th
    boundaries, the row order and every reward column stay exactly the vendor's -- only the year
    digits move. Timestamps are compared as strings (ISO order == chronological) and rewritten in
    place, so tabs and the file's CRLF endings survive untouched."""
    stamps = sorted(set(_STAMP_RE.findall(text)))
    if not stamps:
        raise AgentError(500, "TowerScheduleData.txt contains no date -- unexpected format, not "
                              "modifying it.")
    first, last = stamps[0], stamps[-1]
    # The LARGEST +K that still covers `now`, not the smallest: 2.8's calendar spans 26 months, so
    # the smallest cover can leave `now` two weeks from the calendar's end -- and past that end the
    # abyss silently closes until something restarts the gameserver. The largest cover starts the
    # calendar at most a year back, leaving the longest possible runway of future windows. Negative
    # shifts are allowed on purpose: a file shifted under a future-skewed clock (dead CMOS battery,
    # an admin date-forward test) must come BACK once the clock is right, otherwise it sits wedged
    # decades ahead while every run happily reports "already covers today".
    shift = max((k for k in range(-200, 200)
                 if _plus_years(first, k) <= now < _plus_years(last, k)), default=None)
    stretched = None
    if shift is None:
        # A calendar spanning under 12 months can leave `now` in the gap between "+K ends too
        # early" and "+K+1 starts too late" (never the shipped files -- 13 and 26 months). Take the
        # first +K that ends after `now` and pull the earliest open time back to cover the gap.
        shift = next((k for k in range(-200, 200) if now < _plus_years(last, k)), None)
        if shift is None:
            raise AgentError(500, "Cannot shift the Abyss calendar over %s -- the dates in the file "
                                  "are unexpected (%s .. %s)." % (now, first, last))
        stretched = now[:10] + " 00:00:00"
    new = _STAMP_RE.sub(lambda m: _plus_years(m.group(0), shift), text) if shift else text
    if stretched:
        new = new.replace(_plus_years(first, shift), stretched)
    # The rewrite must never change the TSV shape: same lines, same tab count on each -- a short row
    # is exactly the class of bug that kills the gameserver ~50s after boot.
    old_lines, new_lines = text.split("\n"), new.split("\n")
    if len(old_lines) != len(new_lines) or any(
            a.count("\t") != b.count("\t") for a, b in zip(old_lines, new_lines)):
        raise AgentError(500, "Shifting the Abyss calendar would break the TSV structure -- not "
                              "writing the file.")
    return new, {"shift": shift, "start": stretched or _plus_years(first, shift),
                 "end": _plus_years(last, shift)}


def tower_active_window(text, now):
    """(schedule_id, until) for the row whose window covers `now`, else None. Layout-tolerant: a
    row is its first column (the schedule id) plus whatever timestamps it carries -- the earliest is
    its open time, the latest its close (1.6 and 2.8 headers differ only in the reward columns,
    which hold no dates)."""
    for line in text.split("\n")[1:]:
        stamps = _STAMP_RE.findall(line)
        if len(stamps) >= 2 and min(stamps) <= now < max(stamps):
            return line.split("\t", 1)[0].strip(), max(stamps)
    return None


def tower_mark_applied(version):
    """The running (or freshly booted) gameserver has read the current calendar -- nothing pending."""
    version_state_set(version, towerRestartPending=False)


def apply_tower_schedule(version, job):
    """Reopen the Spiral Abyss: make TowerScheduleData.txt's calendar cover today.

    Called wherever the stack (re)reads its config -- provision, every start, /server/events, the
    watchdog -- because the gameserver loads the file once, at boot (RELOAD_CONFIG_INTERVAL=-1).
    Idempotent while the calendar covers today; when it runs out, the next start (or the watchdog)
    shifts it forward again. Returns True when the file changed. A change also sets the version's
    `towerRestartPending` state BEFORE writing the file: the restart that makes a running
    gameserver re-read it can be lost (the job dies between write and restart, a `docker compose
    ls` hiccup), and on the retry the file already covers today -- without the persisted flag every
    later start would skip the restart and report success while the gameserver keeps the expired
    calendar in memory. Never raises: a failed abyss fix must not take a server start down with
    it -- the abyss just stays closed and the job log says why."""
    path = os.path.join(stack_dir(version), TOWER_TXT)
    try:
        if not os.path.isfile(path):
            job.log("WARNING: %s is missing -- the Abyss stays closed." % TOWER_TXT)
            return False
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        new, info = shift_tower_calendar(text, now)
        active = tower_active_window(new, now)
        if new == text:
            job.log("The Abyss calendar already covers today (until %s)%s." % (
                info["end"][:10],
                " -- schedule %s, next rotation at %s" % active if active else ""))
            return False
        backup = path + ".orig"
        if not os.path.isfile(backup):
            shutil.copy2(path, backup)
        # strict: the flag exists precisely for a restart that gets lost -- if it cannot be recorded,
        # the file is not shifted this time (the except below turns that into a WARNING).
        version_state_set(version, strict=True, towerRestartPending=True)
        # Write-tmp + rename, like save_state: a torn in-place write would leave exactly the class
        # of short TSV row that kills the gameserver ~50s after boot -- and it would look healthy to
        # the next run, whose surviving stamps can still "cover today".
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(new.encode("utf-8"))
        # The rename swaps the inode, which would silently flip the file to the agent's root:644 --
        # give the tmp the original's mode and owner first, same contract as _restore_owner_mode.
        try:
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            if hasattr(os, "chown"):
                os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        replace_file(tmp, path)
        job.log("Abyss calendar shifted by %+d years: covers %s -> %s (rotation on the 1st and "
                "16th of each month at 04:00)." % (info["shift"], info["start"][:10], info["end"][:10]))
        if active:
            job.log("  Abyss open now: schedule %s, until %s." % active)
        else:
            job.log("  WARNING: no window covers today even after the shift -- check the file "
                    "manually.")
        return True
    except Exception as e:  # noqa: BLE001 -- abyss must never block a server start
        job.log("WARNING: the Abyss fix failed (%s) -- the start continues, but the Abyss may stay "
                "closed." % e)
        return False


def do_towerfix(version, job):
    """Refresh the abyss calendar on a RUNNING stack and reboot the services that keep it in
    memory. The watchdog's job; harmless to run by hand."""
    apply_tower_schedule(version, job)
    if not version_state(version).get("towerRestartPending"):
        job.log("The gameserver already has the current calendar -- nothing to restart.")
        return {"tower": False}
    job.log("Restarting the services that keep the calendar in memory...")
    rc = _compose_stream(stack_dir(version), job, "restart", "gameserver", "multiserver", prefix="  ")
    if rc != 0:
        raise AgentError(500, "The Abyss calendar was written, but restarting the services failed "
                              "(code %d) -- stop and start the version to apply it." % rc)
    tower_mark_applied(version)
    return {"tower": True, "restarted": True}


def tower_watchdog():
    """Keep the abyss open on a stack that runs untouched for months.

    The shifted calendar still ENDS (worst case ~1 month out, when the largest cover lands today
    near the 13-month 1.6 calendar's tail), and every other apply site only runs inside an
    admin-triggered job -- so a live box nobody touches would watch the abyss close and stay closed.
    Hourly, for the version that is up: act when the calendar no longer covers now+2h (the yearly
    re-shift, timed onto the 04:00 window boundary where a reset is natural) or when a
    towerRestartPending flag survived a lost restart. Skips quietly whenever another job runs --
    next hour retries. Any failure is logged and the loop lives on."""
    while True:
        time.sleep(3600)
        try:
            if not state_readable_for("tower watchdog"):
                continue
            running, err = running_projects()
            if err or not running:
                continue
            for v in configured_versions():
                if VERSIONS[v]["project"] not in running:
                    continue
                path = os.path.join(stack_dir(v), TOWER_TXT)
                if not os.path.isfile(path):
                    continue
                pending = bool(version_state(v).get("towerRestartPending"))
                if not pending:
                    with open(path, "rb") as f:
                        stamps = _STAMP_RE.findall(f.read().decode("utf-8", "replace"))
                    soon = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 7200))
                    if stamps and min(stamps) <= soon < max(stamps):
                        continue
                try:
                    job = start_job("towerfix", v, lambda j, v=v: do_towerfix(v, j), beside_yielding=True)
                    log_line("gio-agent: tower watchdog started job %s (%s)" % (job.id, v))
                except AgentError:
                    pass  # another job is running -- the next hourly tick retries
        except Exception as e:  # noqa: BLE001 -- the watchdog must outlive any hiccup
            log_line("gio-agent: tower watchdog: %s" % e)


def do_revive(version, job):
    """The service watchdog's job: bring a half-dead but running stack back to full strength."""
    running, err = running_projects()
    if err or VERSIONS[version]["project"] not in (running or []):
        job.log("The %s stack is not running (any more) -- nothing to revive." % version)
        return {"revived": False}
    dead, _states = dead_services(version)
    if not dead:
        job.log("All services are already running.")
        return {"revived": False}
    job.log("Services down although the %s stack is running: %s -- restarting them."
            % (version, ", ".join(dead)))
    verify_stack(version, job)
    return {"revived": True, "services": dead}


_next_revive = {}     # version -> earliest time.time() the service watchdog may try again
_pending_revive = {}  # version -> critical dead set seen last sweep, awaiting confirmation


def service_watchdog():
    """Resurrect services that die while a stack is up.

    Docker cannot do this itself: the game services exit 0 even on fatal errors and their
    on-failure policy ignores clean exits, so a gameserver that dies at 03:00 stays dead until an
    admin notices the white screen. Guards, each earned by a review finding:

    * `runningOk` -- the watchdog only tends a stack whose last agent transition ENDED well (set
      after verify_stack in start/provision/txtfixes; cleared at the start of every transition).
      Without it, the leftovers of a provision that died mid-import (mysql up, project listed,
      hk4e_db_user half-empty) read as "supposed to be up" and the revive would boot the full
      stack over a broken database, letting players build progress the admin's retry then wipes.
      Side effect: a stack started outside the agent (./start.sh) is not tended until its first
      agent start -- that is the price of never trusting a state the agent has not verified.
    * two-sweep confirmation -- a manual `docker compose down` typed over SSH runs ~4 minutes with
      the project still listed and no agent job to 409 behind; its signature is a dead set that
      GROWS sweep to sweep. Only a set identical across two consecutive sweeps (a crash is static)
      triggers the revive; anything still changing waits.
    * one-job-at-a-time keeps a sweep from touching a stack mid-agent-operation (start_job answers
      409; the next sweep retries). Only game-critical deaths trigger a job: a web UI that refuses
      to live would spawn a doomed revive every backoff window, each hogging the job slot for
      minutes. Per-version backoff keeps a service that truly cannot live from being hammered
      every sweep; a start_job refusal sets no backoff (nothing was attempted)."""
    while True:
        time.sleep(SERVICE_WATCH_EVERY)
        try:
            # runningOk cannot be read: tend nothing (the safe side -- never revive a stack whose last
            # transition the agent cannot vouch for).
            if not state_readable_for("service watchdog"):
                _pending_revive.clear()
                continue
            running, err = running_projects()
            if err or not running:
                continue
            for v in configured_versions():
                meta = VERSIONS[v]
                if meta["project"] not in running or not version_state(v).get("runningOk") \
                        or time.time() < _next_revive.get(v, 0):
                    _pending_revive.pop(v, None)
                    continue
                dead, _states = dead_services(v)
                crit = None if dead is None else frozenset(s for s in dead
                                                           if s not in NONCRITICAL_SERVICES)
                if not crit:  # None (docker mute) or empty (healthy) -- nothing confirmed either
                    _pending_revive.pop(v, None)
                    continue
                if _pending_revive.get(v) != crit:
                    _pending_revive[v] = crit
                    continue
                _pending_revive.pop(v, None)
                try:
                    job = start_job("revive", v, lambda j, v=v: do_revive(v, j), beside_yielding=True)
                    _next_revive[v] = time.time() + REVIVE_BACKOFF
                    log_line("gio-agent: service watchdog started job %s (%s: %s)"
                             % (job.id, v, ", ".join(sorted(crit))))
                except AgentError:
                    pass  # another job is running -- the next sweep retries
        except Exception as e:  # noqa: BLE001 -- the watchdog must outlive any hiccup
            log_line("gio-agent: service watchdog: %s" % e)


# -- advertised host (GIO_ADVERTISED_HOST: follow a DDNS name) --
# A home server on a dynamic WAN IP: the ISP hands out a new address and every client is still told
# the old one (dispatch.xml outer_ip, dispatch_url, the gacha URLs, the derived hotpatch URL) -- 4206
# for everyone until an admin notices. With a DNS name the admin keeps current (a DDNS updater on the
# router or the box), the agent resolves it at boot and every ADVERTISED_CHECK seconds, and re-applies
# a confirmed new address to the running stack itself. outer_ip only takes an IP, so the stack keeps
# getting plain IPv4 addresses: the name itself is never written into it.

ADVERTISE_RESOLVE_TIMEOUT = 10  # seconds for one lookup (getaddrinfo has no timeout of its own)
ADVERTISE_BOOT_BUDGET = 12      # seconds main() may spend on the host before it serves anyway
ADVERTISE_FOLLOW_BACKOFF = 900  # per version: a follow netfix is attempted at most this often
ADVERTISE_RETRY_MAX = 24 * 3600  # a failing catch-up doubles its wait from the backoff up to this
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_THIS_NET = ipaddress.ip_network("0.0.0.0/8")
_ADVERTISE = {"checkedAt": None, "error": None, "pending": None, "loggedError": None}
_advertise_lookup = {"thread": None}
_next_follow = {}     # version -> earliest time.time() a follow netfix may start again
_follow_refused = {}  # version -> the refusal already logged (a loopback render), logged once
_follow_failures = {}  # version -> {"count", "retryAt", "error"} of the follow jobs failing in a row


def _is_global_ip(ip):
    """ipaddress' own verdict. A seam: the selftest counts the documentation ranges as public."""
    return ip.is_global


def advertisable_ip(addr, allow_private=None):
    """An IPv4 address a player outside could be handed: a public one, or a private RFC 1918 one when
    GIO_ADVERTISED_ALLOW_PRIVATE=1 (a LAN-only server, split-horizon DNS). Never loopback, unspecified,
    link-local, multicast or reserved -- and never CGNAT 100.64/10 (the ISP side of a carrier NAT,
    unreachable from outside), which is neither public nor private."""
    allow = ADVERTISED_ALLOW_PRIVATE if allow_private is None else allow_private
    try:
        ip = ipaddress.ip_address(str(addr).strip())
    except ValueError:
        return False
    if ip.version != 4 or ip in _THIS_NET or ip.is_loopback or ip.is_link_local or ip.is_multicast \
            or ip.is_reserved:
        return False
    return bool(_is_global_ip(ip) or (allow and any(ip in n for n in _RFC1918)))


def _ip_sort_key(addr):
    try:
        return 0, int(ipaddress.ip_address(addr))
    except ValueError:
        return 1, 0


def _default_resolver(host):
    return [ai[4][0] for ai in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)]


def _resolve_advertised(host, resolver=None, current=None, allow_private=None,
                        timeout=ADVERTISE_RESOLVE_TIMEOUT):
    """(ip, None) -- the address to advertise for `host` -- or (None, why).

    The lookup runs on a worker thread with a timeout, so a hung resolver stalls neither the watchdog
    nor main() at boot, and at most one lookup is ever outstanding (a still-hung one answers "has not
    returned yet" instead of piling threads up). Addresses a remote player cannot use are dropped;
    if the current IP is among the rest it is kept -- a name with several A records must not flap
    between them -- otherwise the lowest one wins. IPv4 only: outer_ip is IPv4."""
    allow = ADVERTISED_ALLOW_PRIVATE if allow_private is None else allow_private
    prev = _advertise_lookup["thread"]
    if prev is not None and prev.is_alive():
        return None, "the previous lookup of %s has not returned yet" % host
    box = {}

    def work():
        try:
            box["addrs"] = list((resolver or _default_resolver)(host))
        except Exception as e:  # noqa: BLE001 -- gaierror, UnicodeError for a malformed name, ...
            box["error"] = e

    t = threading.Thread(target=work, name="advertise-lookup", daemon=True)
    _advertise_lookup["thread"] = t
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "no DNS answer for %s within %ds" % (host, timeout)
    if "error" in box:
        return None, "cannot resolve %s (%s)" % (host, box["error"])
    addrs = sorted({str(a).strip() for a in box.get("addrs") or [] if str(a).strip()}, key=_ip_sort_key)
    usable = [a for a in addrs if advertisable_ip(a, allow)]
    if not usable:
        if not addrs:
            return None, "%s has no IPv4 address" % host
        return None, ("%s resolves only to addresses players outside cannot use (%s)%s"
                      % (host, ", ".join(addrs[:4]),
                         "" if allow else " -- GIO_ADVERTISED_ALLOW_PRIVATE=1 accepts a private LAN address"))
    return (current if current in usable else usable[0]), None


def _note_advertise_error(err):
    """Record the lookup outcome; log only its changes (a DNS outage must not fill the journal)."""
    _ADVERTISE["error"] = err
    if err == _ADVERTISE["loggedError"]:
        return
    _ADVERTISE["loggedError"] = err
    if err:
        log_line("gio-agent: advertise watchdog: WARNING: %s -- keeping %s." % (err, ADVERTISED_IP or "no advertised address"))
    else:
        log_line("gio-agent: advertise watchdog: %s resolves again." % ADVERTISED_HOST)


def _swap_advertised(ip):
    """Caller holds _op_lock. The address becomes effective and is remembered as the boot fallback."""
    global ADVERTISED_IP
    old, ADVERTISED_IP = ADVERTISED_IP, ip
    state_set(best_effort=True, advertisedResolvedIp=ip)
    log_line("gio-agent: advertised IP %s -> %s (GIO_ADVERTISED_HOST %s)" % (old or "none", ip, ADVERTISED_HOST))


def resolve_advertised_at_boot(resolver=None, budget=ADVERTISE_BOOT_BUDGET, sleep=time.sleep,
                               clock=time.monotonic):
    """main(), before the watchdogs and the HTTP server: whatever runs before the first watchdog tick
    (ensure_bind_ip, the derived hotpatch URL, a Start pressed right away) needs the real address.
    Bounded: retried every 2 s while it fails fast, never past `budget`. On failure the last confirmed
    address in the state (advertisedResolvedIp) is used; with none, nothing is advertised until the
    host resolves -- while reachable_intent() keeps treating the box as reachable from outside."""
    global ADVERTISED_IP
    if not ADVERTISED_HOST:
        return None
    try:
        stored = (load_state().get("advertisedResolvedIp") or "").strip()
    except StateUnreadable:
        stored = ""
    if stored and not advertisable_ip(stored):
        stored = ""
    end = clock() + budget
    while True:
        left = end - clock()
        ip, err = _resolve_advertised(ADVERTISED_HOST, resolver, current=stored or None,
                                      timeout=max(1.0, min(ADVERTISE_RESOLVE_TIMEOUT, left)))
        if ip or clock() + 2 >= end:
            break
        sleep(2)
    with _op_lock:
        _ADVERTISE.update(checkedAt=time.strftime("%Y-%m-%d %H:%M:%S"), error=err, loggedError=err)
        if ip:
            ADVERTISED_IP = ip
            if ip != stored:
                state_set(best_effort=True, advertisedResolvedIp=ip)
            log_line("gio-agent: GIO_ADVERTISED_HOST %s -> %s" % (ADVERTISED_HOST, ip))
            return "resolved"
        ADVERTISED_IP = stored
        if stored:
            log_line("gio-agent: WARNING: %s -- advertising the last confirmed address %s until it "
                     "resolves." % (err, stored))
            return "fallback"
        log_line("gio-agent: WARNING: %s and no address was ever confirmed -- nothing is advertised until "
                 "it resolves (the box still counts as reachable from outside)." % err)
        return "unresolved"


def advertised_lags(version, vs=None):
    """A stack the agent has applied an address to does not hold the effective one (XML or DB half)."""
    if not ADVERTISED_IP:
        return False
    vs = version_state(version) if vs is None else vs
    return (vs.get("advertisedIp") != ADVERTISED_IP
            or ("advertisedIpSql" in vs and vs.get("advertisedIpSql") != ADVERTISED_IP)
            or bool(vs.get("advertisedIpSqlOwed")))


def _follow_versions():
    """Versions a follow netfix may touch: listed by compose AND vouched for by runningOk (the
    service watchdog's guard -- never re-apply onto a stack whose last transition did not end well).
    None = docker is mute, nothing can be decided."""
    running, err = running_projects()
    if err:
        return None
    return [v for v in configured_versions()
            if VERSIONS[v]["project"] in (running or []) and version_state(v).get("runningOk")]


def _follow_blocked(version, retry=False):
    """retry=True: a database-only catch-up, which also waits out the failure backoff. A confirmed
    address change does not, nor does a catch-up whose config files are behind the effective address --
    their XML half is really stale, so they get the job after the plain backoff."""
    now = time.time()
    if now < _next_follow.get(version, 0) or not is_bootstrapped(version):
        return True
    if retry and now < (_follow_failures.get(version) or {}).get("retryAt", 0):
        return True
    if compose_binds_loopback(version):
        # netfix would 409 on it every sweep: say it once, leave the repair (Prepare server) to the admin
        if _follow_refused.get(version) != "loopback":
            _follow_refused[version] = "loopback"
            log_line("gio-agent: advertise watchdog: the %s stack is rendered with its ports on 127.0.0.1 "
                     "-- not re-applying the address; run Prepare server." % version)
        return True
    _follow_refused.pop(version, None)
    return False


def _start_follow(version, ip, why):
    job = start_job("netfix", version, lambda j, v=version, a=ip: do_advertise_follow(v, j, a),
                    beside_yielding=True)
    _next_follow[version] = time.time() + ADVERTISE_FOLLOW_BACKOFF
    log_line("gio-agent: advertise watchdog: %s -- started job %s (%s -> %s)" % (why, job.id, version, ip))
    return job


def _follow_failed(version, why):
    """A follow job failed. Its catch-up retry waits the backoff, then twice as long after every further
    failure in a row (15 min, 30 min, 1 h ... 24 h): a database half that keeps failing must not turn
    into an endless retry loop. /status advertisedError carries the reason until a pass succeeds.
    Returns the seconds until the retry."""
    count = (_follow_failures.get(version) or {}).get("count", 0) + 1
    wait = min(ADVERTISE_FOLLOW_BACKOFF * 2 ** min(count - 1, 16), ADVERTISE_RETRY_MAX)
    retry_at = time.time() + wait
    _follow_failures[version] = {
        "count": count, "retryAt": retry_at,
        "error": "re-applying the address to the %s stack failed %d time(s) in a row (%s) -- next attempt "
                 "at %s" % (version, count, why, time.strftime("%Y-%m-%d %H:%M", time.localtime(retry_at)))}
    log_line("gio-agent: advertise watchdog: %s" % _follow_failures[version]["error"])
    return wait


def follow_error():
    """The follow failures still waiting for their retry, for /status advertisedError (None: none)."""
    return "; ".join(f["error"] for _v, f in sorted(_follow_failures.items())) or None


def advertise_tick(resolver=None):
    """One sweep of the advertise watchdog; returns what it did (the selftest drives it)."""
    if not ADVERTISED_HOST:
        return "off"
    if not state_readable_for("advertise watchdog"):
        return "state"
    ip, err = _resolve_advertised(ADVERTISED_HOST, resolver, current=ADVERTISED_IP or None)
    _ADVERTISE["checkedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _note_advertise_error(err)
    if err:
        _ADVERTISE["pending"] = None  # two CONSECUTIVE identical answers: a failed lookup breaks the run
        return "error"
    if ip != ADVERTISED_IP:
        if _ADVERTISE["pending"] != ip:
            _ADVERTISE["pending"] = ip
            log_line("gio-agent: advertise watchdog: %s now resolves to %s (advertised: %s) -- acting if "
                     "the next check agrees." % (ADVERTISED_HOST, ip, ADVERTISED_IP or "none"))
            return "pending"
        return _advertise_change(ip)
    _ADVERTISE["pending"] = None
    return _advertise_catch_up()


def _advertise_change(ip):
    """A confirmed new address. The running stack gets a netfix job, which swaps the address under
    _op_lock; with nothing the agent may re-apply onto, only the address is swapped (under the same
    lock, taken without waiting) and a stopped stack picks it up at its next start. A busy slot or
    lock keeps the confirmation: the next sweep acts at once."""
    versions = _follow_versions()
    if versions is None:
        return "docker"
    for v in versions:
        if _follow_blocked(v):
            continue
        try:
            _start_follow(v, ip, "%s changed" % ADVERTISED_HOST)
        except AgentError:
            return "busy"
        _ADVERTISE["pending"] = None
        return "job"
    if not _op_lock.acquire(blocking=False):
        return "busy"
    try:
        _swap_advertised(ip)
    finally:
        _op_lock.release()
    _ADVERTISE["pending"] = None
    return "swapped"


def _advertise_catch_up():
    """The address did not change, but a running stack does not hold it: the netfix that should have
    re-applied it failed (retried after the backoff), the swap happened while the stack was blocked,
    or the agent restarted onto a new address with the stack still up."""
    for v in _follow_versions() or []:
        vs = version_state(v)
        if not advertised_lags(v, vs):
            _follow_failures.pop(v, None)  # healed meanwhile (a start, a manual netfix): the backoff starts over
            continue
        # The failure backoff holds only a database-only retry. Config files behind the effective address
        # (a change confirmed inside the plain backoff, only swapped) are as stale as a confirmed change,
        # and every full netfix records advertisedIp, so its later retries are held by the backoff again.
        if _follow_blocked(v, retry=vs.get("advertisedIp") == ADVERTISED_IP):
            continue
        try:
            _start_follow(v, ADVERTISED_IP, "the running %s stack does not advertise %s yet" % (v, ADVERTISED_IP))
        except AgentError:
            return "busy"
        return "job"
    return "idle"


def do_advertise_follow(version, job, ip):
    """The advertise watchdog's job (kind netfix). A job runs under _op_lock -- the one place the
    effective address may change while stacks are operated on. The stack is re-checked here: it may
    have stopped since the sweep, and a stopped stack is never netfixed (it gets the address at its
    next start, from both records)."""
    require_state_readable()
    if ip != ADVERTISED_IP:
        job.log("GIO_ADVERTISED_HOST %s now resolves to %s (was %s)." % (ADVERTISED_HOST, ip, ADVERTISED_IP or "none"))
        _swap_advertised(ip)
    up, _err = stack_up(version)
    if not up or not version_state(version).get("runningOk"):
        job.log("The %s stack is not running (any more) -- it advertises %s from its next start." % (version, ip))
        return {"advertisedIp": ip, "netfix": False}
    try:
        restarted = _follow_reapply(version, job, ip)
    except Exception as e:  # noqa: BLE001 -- counted for the backoff, then failed as usual
        _follow_failed(version, getattr(e, "message", None) or str(e))
        raise
    if not advertised_sql_current(version):
        wait = _follow_failed(version, "the database half did not complete")
        raise AgentError(500, "The address is in the config files, but the database half did not complete "
                              "-- retried in %d minutes." % (wait // 60))
    _follow_failures.pop(version, None)
    return {"advertisedIp": ip, "netfix": True, "restarted": restarted}


def _follow_reapply(version, job, ip):
    """Re-apply the address to the running stack; returns whether its services were restarted.
    The XML half behind the effective address: the full netfix (it restarts the services), then the
    survival check netfix lacks -- unattended, it needs one. The XML half already current (an earlier
    follow, a netfix or a start wrote it): only the database half lags, so only that is retried, and
    the services -- restarting them kicks everyone -- only when something they read at boot changed:
    the database half completed now, or an XML file or the hotpatch config moved. A database half
    that keeps failing therefore never restarts anything."""
    if version_state(version).get("advertisedIp") != ip:
        job.log("Re-applying the public address %s to the running %s stack (connected players are kicked)..."
                % (ip, version))
        do_netfix(version, job)
        verify_stack(version, job)
        return True
    _netfix_guard(version)
    job.log("The %s config files already advertise %s -- retrying the database half only..." % (version, ip))
    touched = apply_advertised_ip(version, job, with_sql=True)
    touched += apply_hotpatch(version, job, with_sql=True)
    if not touched and not advertised_sql_current(version):
        job.log("Nothing the services read at boot changed -- not restarting them.")
        return False
    _restart_ip_services(version, job)
    verify_stack(version, job)
    return True


def advertise_watchdog():
    """Follow GIO_ADVERTISED_HOST. Started only when a host is configured. Every ADVERTISED_CHECK
    seconds: resolve; a new answer acts only once a second consecutive check returns the same one
    (DNS TTL lag, a resolver briefly answering from a stale cache); a lookup error or an answer with no
    usable address keeps the current IP and sets advertisedError. Any failure is logged and the loop
    lives on; sweeps skip while the state file is unreadable."""
    while True:
        time.sleep(ADVERTISED_CHECK)
        try:
            advertise_tick()
        except Exception as e:  # noqa: BLE001 -- the watchdog must outlive any hiccup
            log_line("gio-agent: advertise watchdog: %s" % e)


# -- progress templates (relic_tpl_<id> databases players can start from) --

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9-]{1,24}$")


def template_db_name(tpl_id):
    return "relic_tpl_" + tpl_id


def manifest_templates(version, man=None):
    """[{id, label, sql, md5?}] from the manifest (`fresh` is implicit and never listed here)."""
    man = read_manifest(version) if man is None else man
    out = []
    for t in (man or {}).get("templates") or []:
        if isinstance(t, dict) and t.get("id"):
            out.append({"id": str(t["id"]), "label": str(t.get("label") or t["id"]),
                        "sql": str(t.get("sql") or ""), "md5": t.get("md5")})
    return out


def validate_templates(templates, base_dir):
    """Manifest sanity: ids [a-z0-9-]{1,24}, unique, not 'fresh', sql file present (md5 verified
    when the manifest states one). Raises ValueError with the reason."""
    seen = set()
    for t in templates:
        tid = t["id"]
        if not TEMPLATE_ID_RE.match(tid):
            raise ValueError("template id '%s' must match [a-z0-9-]{1,24}" % tid)
        if tid == "fresh":
            raise ValueError("template id 'fresh' is reserved for the implicit empty account")
        if tid in seen:
            raise ValueError("duplicate template id '%s'" % tid)
        seen.add(tid)
        if not t["sql"]:
            raise ValueError("template '%s' has no sql path" % tid)
        path = os.path.join(base_dir, t["sql"])
        if not os.path.isfile(path):
            raise ValueError("template '%s': dump missing: %s" % (tid, path))
        if t.get("md5") and _file_md5(path) != str(t["md5"]).lower():
            raise ValueError("template '%s': %s does not match the manifest md5" % (tid, t["sql"]))
    return True


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _scalar(result):
    """Last non-empty stdout line of a mysql -e query (the value under the header)."""
    lines = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


def ensure_templates(version, job, only=None):
    """(Re)create every template database named by the manifest: DROP + CREATE + import + verify
    that the dump holds exactly the one player (uid 1) copy_save will read. mysql must be up. Each
    template fails on its own -- the result (md5 + createdAt, or error) is persisted per id in
    versions.<v>.templates and the caller's job goes on. `only` = the ids to (re)create, every
    other record kept as it is (heal_templates); without it the records are replaced whole."""
    templates = manifest_templates(version)
    if only is not None:
        templates = [t for t in templates if t["id"] in only]
        results = dict(version_state(version).get("templates") or {})
    else:
        results = {}
    if not templates:
        job.log("The %s manifest names no progress templates." % version)
    for t in templates:
        tid, db = t["id"], template_db_name(t["id"])
        src = os.path.join(payload_dir(version), t["sql"])
        try:
            if not TEMPLATE_ID_RE.match(tid):
                raise AgentError(500, "invalid template id '%s'" % tid)
            if not os.path.isfile(src):
                raise AgentError(500, "template dump missing: %s" % t["sql"])
            job.log("Template '%s': recreating database %s..." % (tid, db))
            r = mysql_exec(version, "DROP DATABASE IF EXISTS `%s`; CREATE DATABASE `%s` "
                                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;" % (db, db))
            if r.returncode != 0:
                raise AgentError(500, "could not recreate %s: %s" % (db, _cmd_err(r, 300)))
            r = mysql_import(version, src, db)
            if r.returncode != 0:
                raise AgentError(500, "import into %s failed: %s" % (db, _cmd_err(r, 300)))
            r = mysql_exec(version, "SELECT COUNT(*) FROM `%s`.t_player_data_1 WHERE uid=1" % db)
            if r.returncode != 0 or _scalar(r) != "1":
                raise AgentError(500, "template %s must hold exactly one player with uid 1 in "
                                      "t_player_data_1 (found: %s)" % (db, _scalar(r) or "?"))
            results[tid] = {"md5": _file_md5(src), "createdAt": time.strftime("%Y-%m-%d %H:%M:%S")}
            job.log("Template '%s' ready (%s)." % (tid, t["label"]))
        except AgentError as e:
            results[tid] = {"error": e.message, "createdAt": None}
            job.log("WARNING: template '%s' failed: %s" % (tid, e.message))
        except Exception as e:  # noqa: BLE001 -- one broken template must not fail provisioning
            results[tid] = {"error": str(e), "createdAt": None}
            job.log("WARNING: template '%s' failed: %s" % (tid, e))
    version_state_set(version, templates=results)
    return results


def templates_owed(version):
    """The manifest template ids this stack holds no good record of: never imported (an agent
    installed over an existing stack starts without records; payloads that added a template), last
    import failed, or imported from a dump that has changed since. A dump missing from the payload
    folder is not owed -- there is nothing to import."""
    recs = version_state(version).get("templates") or {}
    owed = []
    for t in manifest_templates(version):
        path = os.path.join(payload_dir(version), t["sql"])
        if not os.path.isfile(path):
            continue
        rec = recs.get(t["id"])
        if not template_record_ready(rec) or (rec.get("md5") and rec["md5"] != _file_md5(path)):
            owed.append(t["id"])
    return owed


def heal_templates(version, job):
    """At every start with MySQL up (agent 3.7): import the templates_owed, leave the rest alone --
    an agent reinstalled over a provisioned stack otherwise hid every template from players (the
    policy offers only imported ones) until someone found Templates -> ensure. A stack prepared
    'fixes only' is skipped: that choice keeps the templates out until they are asked for. Never
    fatal (ensure_templates fails per template)."""
    if version_state(version).get("progress") == "fixes":
        return
    try:
        owed = templates_owed(version)
    except Exception as e:  # noqa: BLE001 -- a start never fails over the templates
        job.log("WARNING: could not check the progress templates (%s)." % _short_err(e))
        return
    if owed:
        job.log("Progress templates without a good import on this stack: %s -- importing them now."
                % ", ".join(owed))
        ensure_templates(version, job, only=owed)


def do_templates_ensure(version, job):
    """Import the template databases on a RUNNING stack -- no stop, no player data touched."""
    require_state_readable()
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on this server." % version)
    if not mysql_reachable(version):
        raise AgentError(409, "Version %s is not running (MySQL does not answer). Start it first -- "
                              "the templates are imported without stopping anything." % version)
    results = ensure_templates(version, job)
    return {"templates": results}


def templates_report(version):
    """What GET /server/templates answers: the manifest entries merged with the import state."""
    check_known(version)
    st = version_state(version).get("templates") or {}
    out = []
    for t in manifest_templates(version):
        rec = st.get(t["id"]) or {}
        path = os.path.join(payload_dir(version), t["sql"])
        out.append({"id": t["id"], "label": t["label"], "sql": t["sql"],
                    "present": os.path.isfile(path),
                    "imported": bool(rec.get("createdAt")) and not rec.get("error"),
                    "md5": rec.get("md5"), "createdAt": rec.get("createdAt"),
                    "error": rec.get("error")})
    return {"templates": out}


# -- the long operations --

def bootstrap_plan_windows(has_compose):
    """The vendor bootstrap.bat as data (pure, locked by the selftest): the eight compose steps
    it runs, as ("run", argv, check) / ("rmtree", dir) / ("lock",) tuples. argv is relative to the
    stack dir (cwd), exactly like the .bat. `check` names the OUTPUT that proves a one-shot step
    worked -- `docker compose up <svc>` answers 0 whether or not the container did its job."""
    pre = ["docker", "compose", "-f", "docker-preinstall.yml"]
    steps = []
    if has_compose:
        steps += [("run", ["docker", "compose", "kill"], None),
                  ("run", ["docker", "compose", "rm", "-f"], None)]
    steps += [
        ("rmtree", "mysql"),
        ("rmtree", "redis"),
        ("run", ["docker", "network", "prune", "-f"], None),
        ("run", pre + ["up", "preparevars"], "rendered"),
        ("run", pre + ["kill", "preparevars"], None),
        ("run", pre + ["rm", "-f", "preparevars"], None),
        ("run", ["docker", "compose", "up", "-d", "mysql", "redis", "phpmyadmin", "adminer"], None),
        ("run", pre + ["up", "preparedb"], "database"),
        ("run", pre + ["kill", "preparedb"], None),
        ("run", pre + ["rm", "-f", "preparedb"], None),
        ("lock",),
        ("run", ["docker", "compose", "down"], None),
        ("run", ["docker", "compose", "up", "-d"], None),
    ]
    return steps


def _rmtree_retry(path, attempts=5):
    """rmtree that survives a file still held for a moment (Docker Desktop releasing a bind mount)."""
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            if i == attempts - 1:
                raise
            # read-only bits (MariaDB's data files) make unlink fail on Windows: clear them, retry
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    try:
                        os.chmod(os.path.join(root, n), 0o777)
                    except OSError:
                        pass
            time.sleep(1.0)


def _check_rendered(version):
    """After preparevars: the compose file and every server XML the templates describe must exist."""
    d = stack_dir(version)
    missing = []
    if not os.path.isfile(os.path.join(d, "docker-compose.yml")):
        missing.append("docker-compose.yml")
    for tmpl in stack_templates(version):
        if not os.path.isfile(tmpl[:-len(".tmpl")]):
            missing.append(os.path.relpath(tmpl[:-len(".tmpl")], d))
    return missing


def _bootstrap_windows(version, job):
    """Run the vendor's bootstrap steps from Python (bootstrap.bat ends with `pause` and cannot be
    driven from a service). Exit codes of the one-shot steps are ignored like the .bat does; their
    OUTPUTS are checked instead: rendered files after preparevars, the hk4e_db_user database after
    preparedb. `.env` must already have been repaired by ensure_bind_ip (prepare-vars bakes it)."""
    d = stack_dir(version)
    pre_yml = os.path.join(d, "docker-preinstall.yml")
    if not os.path.isfile(pre_yml) or not os.path.isfile(os.path.join(d, "docker-compose.yml.tmpl")):
        raise AgentError(500, "docker-preinstall.yml / docker-compose.yml.tmpl missing in %s -- is the "
                              "vendor stack extracted there?" % d)
    # prepare-vars.sh / prepare-db.sh refuse to run while .bootstrap.lock exists (the lock without
    # a rendered compose file is a half-finished earlier attempt).
    lock = os.path.join(d, ".bootstrap.lock")
    if os.path.isfile(lock):
        os.remove(lock)
        job.log("Removed a stale .bootstrap.lock.")
    has_compose = os.path.isfile(os.path.join(d, "docker-compose.yml"))
    for step in bootstrap_plan_windows(has_compose):
        if step[0] == "rmtree":
            target = os.path.join(d, step[1])
            try:
                if _rmtree_retry(target):
                    job.log("Removed %s/." % step[1])
            except OSError as e:
                raise AgentError(500, "Could not remove %s (%s) -- is a container still using it? "
                                      "Stop everything in Docker Desktop and retry." % (target, e))
            continue
        if step[0] == "lock":
            with open(lock, "w") as f:
                f.write("\n")
            job.log("Wrote .bootstrap.lock.")
            continue
        _kind, argv, check = step
        # Only a failed `up -d` / `down` of the real stack is fatal by exit code; kill/rm/prune
        # and the one-shot preinstall containers are judged by their OUTPUT checks, like the .bat.
        fatal = argv[2:3] in (["up"], ["down"]) and argv[-1] not in ("preparevars", "preparedb")
        rc = _stream(argv, job, cwd=d, timeout=T_BOOTSTRAP, prefix="  ")
        if rc != 0 and fatal:
            raise AgentError(500, "'%s' failed (code %d). See the log above." % (" ".join(argv), rc))
        if check == "rendered":
            missing = _check_rendered(version)
            if missing:
                raise AgentError(500, "preparevars did not render: %s -- check the container output "
                                      "above (is .env readable? does Docker Desktop share this drive?)."
                                 % ", ".join(missing[:6]))
            job.log("Rendered configs verified.")
        elif check == "database":
            wait_for_mysql(version, job, timeout=180)
            r = mysql_exec(version, "SHOW DATABASES LIKE 'hk4e_db_user'")
            if r.returncode != 0 or "hk4e_db_user" not in (r.stdout or ""):
                raise AgentError(500, "preparedb did not create hk4e_db_user -- check the container "
                                      "output above (bootstrap.sql / data.sql import).")
            job.log("Database hk4e_db_user verified.")


def do_bootstrap(version, job, force=False):
    """Returns "fresh" (the vendor bootstrap ran), "healed" (re-render repaired a poisoned render)
    or None (nothing to do) -- do_setup keys the provisioning depth off this."""
    d = stack_dir(version)
    if is_bootstrapped(version) and not force:
        if not compose_binds_loopback(version):
            job.log("The %s stack is already installed (.bootstrap.lock exists) -- skipping bootstrap."
                    % version)
            return None
        # Re-extracted archive that kept its .bootstrap.lock: the render carries the vendor
        # OUTER_IP but the database is intact, so heal the files instead of wiping mysql/redis.
        job.log("The %s stack is installed but rendered with its ports on 127.0.0.1 (the archive's "
                "original .env) -- re-rendering the configs without reinstalling." % version)
        ensure_bind_ip(version, job)
        # A re-extracted archive brought the vendor's published sign key back too: replace it in the
        # template BEFORE the re-render copies it into muipserver.xml.
        ensure_muip_key(version, job)
        n = render_stack_configs(version, job)
        job.log("%d files re-rendered; the database is untouched." % n)
        if compose_binds_loopback(version):
            # Without this the caller would 409 "press Prepare server" -- the button just pressed.
            raise AgentError(500, "docker-compose.yml still has ports on 127.0.0.1 after the "
                                  "re-render -- docker-compose.yml.tmpl is probably missing or the "
                                  "template has 127.0.0.1 written in. Check %s by hand." % d)
        return "healed"
    if not IS_WINDOWS:
        script = os.path.join(d, "bootstrap.sh")
        if not os.path.isfile(script):
            raise AgentError(500, "bootstrap.sh is missing in " + d)
    # BEFORE the bootstrap: its preinstall containers exec the bind-mounted dockerfiles/*/*.sh by
    # path, so a freshly 7z-extracted stack (mode 644 everywhere) dies inside docker otherwise.
    ensure_exec_bits(version, job)
    # The bootstrap wipes mysql/ + redis/: its provisioning record goes first, before anything stops.
    forget_provisioned(version, job)
    # The bootstrap brings mysql/redis up itself; the other version must not be holding the ports.
    down_all(job, keep=None)
    if force:
        lock = os.path.join(d, ".bootstrap.lock")
        if os.path.isfile(lock):
            os.remove(lock)
            job.log("Removed .bootstrap.lock (forced reinstall).")
    # BEFORE the bootstrap: prepare-vars.sh is about to bake .env's OUTER_IP into the compose port
    # bindings, every server XML and data.sql -- a leftover vendor 127.0.0.1 must be fixed now.
    ensure_bind_ip(version, job)
    # BEFORE the bootstrap as well: prepare-vars renders muipserver.xml from the template, and the
    # archive's published sign_key must not be what it renders (anyone with the archive could sign
    # GM commands against this box otherwise).
    ensure_muip_key(version, job)
    job.log("Installing the %s stack -- this takes long the first time (docker images + database)."
            % version)
    if IS_WINDOWS:
        _bootstrap_windows(version, job)
    else:
        rc = _stream(["bash", "bootstrap.sh"], job, cwd=d, timeout=T_BOOTSTRAP, prefix="  ")
        if rc != 0:
            raise AgentError(500, "bootstrap.sh failed (code %d). See the log above." % rc)
    if not is_bootstrapped(version):
        raise AgentError(500, "The bootstrap finished but .bootstrap.lock/docker-compose.yml are missing.")
    job.log("Bootstrap done.")
    return "fresh"


PROGRESS_LOG = {
    "default": "importing the pre-made save (every player account on this version is wiped)",
    "keep": "the database, sdk.db and redis are NOT touched",
    "fixes": "configuration files only",
}


def do_provision(version, job, txt_fixes=None, fresh=False, keep_db=False, progress="default"):
    """Put the stack in the state the admin chose: endless events, WAN IP, hotpatch, template
    databases -- and, with progress "default", the pre-GAA save.

    `progress` (agent 3.4): default = the shipped save is imported (sdk.db, redis dump, the
    hk4e_db_user dump); keep = no save at all, the database is not touched; fixes = configuration
    files only (templates skipped too). The record (state + marker) remembers the choice, so a
    keep/fixes stack is never auto-imported later.
    `fresh` = the bootstrap has just run, so the stack it left running has never had a player on it
    and can be torn down without waiting out every 30s grace period.
    `keep_db` = reinstall the payload files, events and IPs but do NOT touch the MySQL dumps -- used
    by setup's heal path, where the player database is intact and re-importing the pre-GAA save
    would throw away everyone's progress."""
    progress = parse_progress(progress)
    if not is_bootstrapped(version):
        raise AgentError(409, "The %s stack is not installed yet -- run Prepare server first." % version)
    if compose_binds_loopback(version):
        # A direct /server/provision on a stack whose render still carries the vendor OUTER_IP would
        # "succeed" into unreachability; /server/setup heals the render first, so send them there.
        raise AgentError(409, "The %s stack is rendered with its ports on 127.0.0.1 (the archive's "
                              "original .env). Run Prepare server -- it re-renders the configs and "
                              "only then applies the progress." % version)
    man = read_manifest(version)
    # The marker-merged record, not version_state(): on a stack whose state record was lost the
    # marker still says default/1, and reading only the state would rewrite defaultAccount to 0 and
    # bump the generation for a run that imported nothing. 503 here, before anything is touched,
    # over an unreadable state.
    prev = provision_record(version)
    txt_fixes = (txt_fixes or TXT_FIXES_MODE).lower()
    # `config` is installed in every mode; the save only by default; the reversible txt fixes when
    # asked for now -- and always by "fixes" (the fixes ARE the request there, txtFixes=later ignored).
    stages = {"config"}
    if progress == "default":
        stages.add("save")
    if progress == "fixes" or txt_fixes == "now":
        stages.add("txt_fix")
    ensure_exec_bits(version, job)
    # BEFORE install_files: on the nested 2.8 layout it would otherwise create a top-level
    # server/data/txt/ holding only the agent's files, beside the vendor's still-nested tree.
    ensure_data_layout(version, job)

    job.log("Provisioning version %s%s -- progress: %s (%s)."
            % (version, " (%s)" % man["title"] if man and man.get("title") else "", progress, PROGRESS_LOG[progress]))
    # A provision that dies midway (failed import, mysql timeout) leaves mysql+redis up and the
    # project listed -- the service watchdog must NOT "helpfully" boot the full stack over a
    # half-imported database. runningOk comes back only at the end. strict: 507 before the down.
    version_state_set(version, strict=True, runningOk=False)
    down_all(job, grace=5 if fresh else None)
    # Every service is down now: a stack still on the archive's published sign key gets a private one
    # here, and the final `up -d` below boots muipserver with it (covers fresh and keep_db alike).
    ensure_muip_key(version, job)

    job.log("Copying the payload files into the stack...")
    n = install_files(version, job, stages)
    job.log("%d files installed." % n)

    job.log("Starting only mysql + redis for the import...")
    rc = _compose_stream(stack_dir(version), job, "up", "-d", "mysql", "redis", prefix="  ")
    if rc != 0:
        raise AgentError(500, "Could not start mysql/redis (code %d)." % rc)
    wait_for_mysql(version, job)

    import_db = progress == "default" and not keep_db
    for entry in (man or {}).get("mysql", []):
        src = os.path.join(payload_dir(version), entry["src"])
        db = entry["db"]
        if not import_db:
            job.log("Keeping database %s untouched (%s)."
                    % (db, "stack already provisioned -- the players' progress stays" if keep_db
                       else "progress: %s" % progress))
            continue
        if not os.path.isfile(src):
            raise AgentError(500, "SQL dump missing: " + src)
        if entry.get("recreate"):
            job.log("Recreating database %s..." % db)
            r = mysql_exec(version, "DROP DATABASE IF EXISTS `%s`; CREATE DATABASE `%s` "
                                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;" % (db, db))
            if r.returncode != 0:
                raise AgentError(500, "Could not recreate database %s: %s" % (db, _cmd_err(r, 400)))
        job.log("Importing %s -> %s (%.0f KB)..." % (entry["src"], db, os.path.getsize(src) / 1024.0))
        r = mysql_import(version, src, db)
        if r.returncode != 0:
            raise AgentError(500, "Import into %s failed: %s" % (db, _cmd_err(r, 400)))
        job.log("Import done.")

    # Template databases right after the player DB import (mysql up, nothing else needed). They are
    # agent-owned scratch data, never player progress, so the heal path and "keep" refresh them too
    # -- they are what gives a signup ready-made progress on a server without the default save.
    if progress != "fixes":
        ensure_templates(version, job)
    else:
        job.log("Template databases skipped (fixes only) -- run Templates -> ensure later if you want "
                "signup progress.")

    if man and man.get("sql_events"):
        run_sql(version, job, man["sql_events"], tolerate=True)
    apply_events(version, job)
    repair_settled_activities(version, job)  # the game services are down here (agent 3.7)
    apply_tower_schedule(version, job)  # the gameserver is still down here -- boot picks it up

    apply_advertised_ip(version, job)
    # The data.sql import just reset the four hotpatch URL columns: re-assert the persisted
    # decision (files + SQL) BEFORE the game services boot and read them.
    apply_hotpatch(version, job)

    job.log("Starting the whole stack...")
    rc = _compose_stream(stack_dir(version), job, "up", "-d", prefix="  ")
    if rc != 0:
        raise AgentError(500, "Could not start the stack (code %d)." % rc)
    tower_mark_applied(version)  # the boot that just happened read the calendar written above

    # The record. defaultAccount = the shipped save is in THIS database: true after a default run,
    # carried over by keep/fixes (they touch no account), false after a fresh bootstrap (forget_
    # provisioned). provisionedAt is the public `generation`, the thing every remembered player
    # account is keyed on -- bumped when the save stage was installed (a keep_db heal too, on purpose:
    # install_files just put the payload's sdk.db and redis dump back, so accounts created since are
    # gone) or when no record existed; a keep/fixes run over a provisioned stack keeps it, so nobody's
    # remembered account turns "stale".
    default_account = True if progress == "default" else bool(prev.get("defaultAccount"))
    bump = ("save" in stages) or not prev.get("provisionedAt")
    provisioned_at = time.strftime("%Y-%m-%d %H:%M:%S") if bump else prev["provisionedAt"]
    txt_state = "applied" if "txt_fix" in stages else prev.get("txtFixes", "pending")
    # The marker FIRST: it is what survives a lost state file, so even a state write that fails
    # right after it can never turn the next Start into a second import (nor a keep stack into one).
    try:
        write_provisioned_marker(version, provisioned_at, progress, default_account)
    except OSError as e:
        job.log("WARNING: could not write %s (%s) -- a lost state file would make the next Start "
                "import the save again." % (PROVISIONED_MARKER, e))
    # strict: a provisionedAt that silently fails to persist is exactly the re-import-on-next-Start
    # this job must never leave behind -- fail loudly instead.
    version_state_set(version, strict=True, provisionedAt=provisioned_at, progress=progress,
                      defaultAccount=default_account, txtFixes=txt_state, autoProvision=None)
    state_set(lastStarted=version)
    acc = (man or {}).get("account")
    if default_account and acc:
        job.log("Game account: %s (password: %s)" % (acc, (man or {}).get("password", "any")))
    elif not default_account:
        job.log("No pre-made account on this server. Create accounts through Relic (Server -> Player "
                "accounts, templates give them ready-made progress) or the register page on port 21000.")
    if "txt_fix" not in stages and txt_state != "applied":
        job.log("The 'fixed' txt files were NOT installed. After chapter 1 of the GAA quest run "
                "Apply txt fixes.")
    # AFTER provisionedAt was persisted on purpose: a service that will not stay up must fail the
    # job loudly, but the save WAS imported -- the retry is a plain start, never a second import
    # (which would wipe whatever a player did since).
    verify_stack(version, job)
    version_state_set(version, runningOk=True)
    apply_pending_password(version, job)
    release_provision_guard(job)
    return {"provisioned": True, "progress": progress, "defaultAccount": default_account, "txtFixes": txt_state}


def needs_provision(version, job=None):
    """Whether a start/setup with provision="auto" must import the save. THE destructive decision:
    it never answers True off a state file it could not read (503), a missing provisionedAt next to
    a .relic-provisioned marker re-seeds the record instead (the public generation stays the same),
    after a backup restore or a state reset a version with neither record is left alone, so is one
    re-pointed at an installed stack without a record (autoProvision False), and a stack
    recorded as progress keep/fixes is NEVER imported automatically -- not by `once`, not by `switch`
    (which re-imports only stacks recorded as default); the explicit /server/provision
    {progress: default} is the only way."""
    try:
        data = load_state()
    except StateUnreadable:
        raise state_unreadable_error()
    if PROVISION_MODE == "never" or read_manifest(version) is None:
        return False
    vs = (data.get("versions") or {}).get(version) or {}
    guard = "restored from a backup" if data.get("restoredFromBackup") else \
        "reset" if data.get("stateReset") else None

    def guarded():
        if job is not None:
            job.log("WARNING: the agent state was %s; automatic provisioning is disabled -- run "
                    "Provision explicitly if the %s stack really needs it." % (guard, version))
        return False

    if not vs.get("provisionedAt"):
        marker = read_provisioned_marker(version)
        if marker is not None:
            # The whole record, not only the timestamp: a v2 marker saying keep/fixes must re-seed
            # that decision too, or a lost state file would turn the stack into an auto-import
            # candidate under `switch`.
            version_state_set(version, provisionedAt=marker["at"], progress=marker["progress"],
                              defaultAccount=marker["defaultAccount"])
            if job is not None:
                job.log("The agent state had no provisioning record for %s, but the stack's %s says it "
                        "was provisioned (%s, progress %s) -- record restored, the save is NOT imported again."
                        % (version, PROVISIONED_MARKER, marker["at"], marker["progress"]))
            return False
        if vs.get("autoProvision") is False:
            # _repoint_state: an installed stack without a provisioning record of this agent -- its database
            # may hold players; only the explicit /server/provision clears the flag
            if job is not None:
                job.log("WARNING: version %s was re-pointed at an installed stack without a provisioning record -- "
                        "automatic provisioning is off for it; run Provision (Re-apply GAA progress) explicitly if "
                        "that stack really needs the save." % version)
            return False
        return guarded() if guard else True
    if vs.get("progress") in ("keep", "fixes"):
        # An explicit "own progress" decision is never overridden by once or switch.
        return False
    if PROVISION_MODE == "switch" and data.get("lastStarted") != version:
        # lastStarted is a non-strict write a .bak restore can roll back: the switch re-import waits
        # until release_provision_guard lifts the guard after a start.
        return guarded() if guard else True
    return False


def do_start(version, job, provision="auto"):
    require_state_readable()
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                         % (version, stack_dir(version)))
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server yet. Run Prepare server."
                         % version)
    if compose_binds_loopback(version):
        raise AgentError(409, "The %s stack was reinstalled with the archive's original .env "
                              "(OUTER_IP=127.0.0.1): its ports would bind to loopback only and no "
                              "client could connect (Fiddler would answer 502 'server busy'). Run "
                              "Prepare server -- it re-renders the configs correctly." % version)
    if provision == "force" or (provision == "auto" and needs_provision(version, job)):
        do_provision(version, job)
        return {"started": True, "provisioned": True}
    # Whether THIS version is already running decides below if a changed abyss calendar needs a
    # service restart: `up -d` on an already-up stack does not reboot the gameserver, and the
    # calendar file is only read at boot. An ls ERROR counts as "maybe up" -- assuming down would
    # skip that restart and leave a running gameserver on the expired calendar while the job
    # reports success.
    projects, ls_err = running_projects()
    was_up = ls_err is not None or VERSIONS[version]["project"] in (projects or [])
    # In transition until the very end -- a start that dies midway must not read as "supposed to
    # be up" to the service watchdog (it would boot whatever half-state the failure left).
    # strict: 507 before the down when it cannot be recorded.
    version_state_set(version, strict=True, runningOk=False)
    down_all(job, keep=version)
    # A stack (re-)extracted by hand from the 2.8 archive has its server/data nested one level too
    # deep -- the game services would exit within seconds while `up -d` answers 0.
    ensure_data_layout(version, job)
    # A re-extraction over a bootstrapped stack keeps the lock + render but strips every exec bit
    # again -- services would die on "permission denied" while `up -d` answers 0.
    ensure_exec_bits(version, job)
    if not was_up:
        # A definitely-down stack still on the archive's published sign key (an install upgraded to
        # 3.4) gets a private one before muipserver boots below -- no second restart needed. A running
        # or "maybe up" stack is left alone: its muipserver holds the old key in memory and rewriting
        # the file would break GM commands until a restart; status() keeps flagging it meanwhile.
        ensure_muip_key(version, job)
    # Re-assert the pathfinding decision onto BOTH compose files before the staged up: the stored one
    # (a re-extracted archive or a secrets re-render reset the template), else what the files say
    # (template -> rendered sync when nobody ever decided). Disabling on an already-up stack must
    # also lose the running container; the enable direction is covered by the `up -d` below.
    pf_want = version_state(version).get("pathfinding")
    if pf_want is None:
        pf_want = pathfinding_enabled(version)
    if pf_want is not None and apply_pathfinding(version, job, pf_want) and was_up and not pf_want:
        reconcile_pathfinding(version, job, False)
    apply_tower_schedule(version, job)
    job.log("Starting version %s..." % version)
    # Staged on purpose: depends_on orders only container CREATION, so on a cold boot nodeserver
    # reaches a mysqld that is still initialising, fails its init and exits 0 one second in -- an
    # exit code on-failure ignores, so docker never brings it back -- and half the stack follows it
    # down while `up -d` answers 0. Warming mysql/redis first removes the race instead of healing it
    # after the fact. This wait also serves the DB half of the IP rewrite below, whose statements
    # against a still-booting MySQL would be swallowed as tolerated errors.
    rc = _compose_stream(stack_dir(version), job, "up", "-d", "mysql", "redis", prefix="  ")
    if rc != 0:
        raise AgentError(500, "docker compose up mysql/redis failed (code %d)." % rc)
    try:
        # The full T_MYSQL_READY, not a shorter leash: InnoDB crash recovery on a cold boot after
        # a power loss is exactly the slow case the staged start exists for, and giving up early
        # degrades it back to the old boot race plus a silently skipped events re-apply.
        wait_for_mysql(version, job)
        db_ready = True
    except AgentError as e:
        job.log("Could not verify MySQL (%s) -- starting the remaining services anyway; the "
                "stability check at the end shows whether they suffered." % e.message)
        db_ready = False
    # The hotpatch files + URL columns are read by dispatch/gameserver ONCE at boot, so re-assert
    # them here, BEFORE the game services come up: a cold boot then reads the final state and
    # needs no restart; only a stack that was ALREADY running (was_up) must be restarted below.
    hp_touched = apply_hotpatch(version, job, with_sql=db_ready)
    if db_ready and not was_up:
        # agent 3.7: nobody can be in game before the gameserver boots -- the one moment a player's
        # save may be edited on a start (a running stack is left to /server/events).
        repair_settled_activities(version, job)
    rc = _compose_stream(stack_dir(version), job, "up", "-d", prefix="  ")
    if rc != 0:
        raise AgentError(500, "docker compose up failed (code %d)." % rc)
    if not was_up:
        # A fresh boot just read the calendar written above; only a stack that was ALREADY running
        # still needs the restart below to notice it.
        tower_mark_applied(version)
    if db_ready:
        # The manifest is the source of truth for which events run, so re-assert it on every start
        # rather than only at provisioning: editing the list must not cost the player their save.
        apply_events(version, job)
        # Likewise the progress templates players are offered (agent 3.7): one the agent holds no
        # good record of is imported now, not at the next provision nobody runs.
        heal_templates(version, job)
    touched = apply_advertised_ip(version, job, with_sql=db_ready)
    # Persisted, not this run's return value: a previous start may have written the calendar and
    # then died before its restart -- the retry finds the file "already covering today" and only
    # this flag still knows the running gameserver never read it.
    tower_pending = bool(version_state(version).get("towerRestartPending"))
    if touched or tower_pending or (hp_touched and was_up):
        # The services read their conf XMLs and the excel-config txt files once, at boot
        # (RELOAD_CONFIG_INTERVAL=-1), and they booted BEFORE the rewrite -- without this restart
        # the fixed files sit dead on disk while dispatch keeps handing clients the old IP (or the
        # gameserver a closed abyss). The abyss case only arises when the stack was ALREADY up when
        # start was pressed; on a fresh boot the calendar was written before `up`. multiserver is
        # in the list for the abyss/schedule half, same pair /server/events restarts.
        job.log("Configs changed after the boot -- restarting the services that keep them in "
                "memory...")
        rc = _compose_stream(stack_dir(version), job, "restart", "dispatch", "gateserver",
                             "gameserver", "multiserver", "muipserver", prefix="  ")
        if rc != 0:
            raise AgentError(500, "The configs were written, but restarting the services failed "
                                  "(code %d) -- stop and start the version to apply them." % rc)
        tower_mark_applied(version)
    state_set(lastStarted=version)
    # LAST, after every restart above: `up` reporting 0 says nothing -- services must also survive.
    verify_stack(version, job)
    version_state_set(version, runningOk=True)
    apply_pending_password(version, job)
    release_provision_guard(job)
    if not db_ready:
        # Loud, at the END where the user reads it: a start that raced a still-recovering MySQL
        # skipped the events allowlist and the DB half of the IP rewrite -- "DONE" alone would
        # quietly weaken the "events re-applied on every start" invariant to "usually".
        job.log("WARNING: MySQL did not answer while the stack was starting, so the events and the "
                "IP in the database were NOT re-applied. Once the server is stable, stop and start "
                "the version once more (or run Re-apply events).")
    return {"started": True, "provisioned": False, "eventsApplied": db_ready}


def do_stop(version, job):
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                         % (version, stack_dir(version)))
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server -- nothing to stop." % version)
    job.log("Stopping version %s..." % version)
    # Before the down starts: an intentional stop must never be "repaired" by the watchdog, not
    # even in the ~4 minutes the down needs to walk the grace periods. Best effort: a stop must
    # work over an unreadable state file (the watchdog skips while it is unreadable anyway).
    version_state_set(version, best_effort=True, runningOk=False)
    rc = _compose_stream(stack_dir(version), job, "down", prefix="  ")
    if rc != 0:
        raise AgentError(500, "docker compose down failed (code %d)." % rc)
    return {"stopped": True}


def _pathfinding_decision(explicit, stored, bootstrapped):
    """What Prepare server applies to the compose files: the request's choice, else the stored one,
    else -- only for a stack that was never bootstrapped -- GIO_PATHFINDING; None = leave the files
    alone (a bootstrapped stack nobody decided about keeps what its files say). Pure."""
    if explicit is not None:
        return explicit
    if stored is not None:
        return stored
    return None if bootstrapped else PATHFINDING_DEFAULT


def do_setup(version, job, txt_fixes=None, force=False, progress="default", pathfinding=None):
    """Prepare server: bootstrap (or heal) the stack, then provision it with the chosen `progress`
    (agent 3.4: default | keep | fixes). `pathfinding` (bool | None) is applied to the compose
    templates BEFORE the bootstrap renders them, so the vendor bootstrap's own `up -d` never starts
    a switched-off pathfindingserver."""
    require_state_readable()
    progress = parse_progress(progress)
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                         % (version, stack_dir(version)))
    # First thing on a freshly extracted 2.8 stack: its server/data is nested one level too deep.
    ensure_data_layout(version, job)
    pf_want = _pathfinding_decision(pathfinding, version_state(version).get("pathfinding"),
                                    is_bootstrapped(version))
    pf_changed = False
    if pf_want is not None:
        pf_changed = apply_pathfinding(version, job, pf_want)
        version_state_set(version, pathfinding=pf_want)
    mode = do_bootstrap(version, job, force=force)  # "fresh" | "healed" | None
    # provision_record, and read AFTER needs_provision has had its chance to re-seed the state from
    # the marker: the heal branch below decides whether the player database survives, and a stale
    # "this stack has no default save" would turn a repair into a full re-import over live progress.
    if mode == "fresh" or needs_provision(version, job):
        # A brand-new database needs the chosen progress; likewise a stack the provisioning policy
        # says was never (or must again be) provisioned.
        return do_provision(version, job, txt_fixes=txt_fixes, fresh=(mode == "fresh"), progress=progress)
    if mode == "healed":
        # The archive re-extract clobbered the payload files (sdk.db, redis dump, txt fixes) but
        # the player database is intact: reinstall everything EXCEPT the MySQL dumps. The admin's
        # choice wins: "default" over a stack that already holds the default save stays this
        # non-destructive heal; "default" over a keep/fixes stack imports (the dialog warned). A
        # version re-pointed at an installed stack without a record (autoProvision False) keeps its
        # database too: only an explicit /server/provision may import over it.
        return do_provision(version, job, txt_fixes=txt_fixes, progress=progress,
                            keep_db=(progress == "default"
                                     and (bool(provision_record(version).get("defaultAccount"))
                                          or version_state(version).get("autoProvision") is False)))
    if progress != "default":
        # Non-destructive re-run on a ready stack: files, templates, events, IP, hotpatch -- and the
        # record now says keep/fixes, which stops any later auto-import.
        return do_provision(version, job, txt_fixes=txt_fixes, progress=progress)
    if pf_changed:
        # The only path that does not go through a full down/up: a running stack must lose or gain
        # the container right here.
        reconcile_pathfinding(version, job, pf_want)
    job.log("The %s stack is already installed and provisioned -- nothing redone, the progress "
            "stays. To deliberately re-import the save use Re-apply GAA progress." % version)
    return {"provisioned": False, "alreadyReady": True}


def do_txt_fixes(version, job, action="apply"):
    require_state_readable()
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server." % version)
    ensure_data_layout(version, job)  # the txt files go into server/data/txt/ -- the REAL one
    ensure_exec_bits(version, job)
    projects, ls_err = running_projects()
    if ls_err is not None:
        # Same hazard do_start guards against: reading "docker mute" as "stack down" would skip the
        # stop + staged restart, then record txtFixes="applied" while a RUNNING gameserver keeps the
        # old files in memory. Unlike do_start there is no safe "maybe up" path here (it would boot
        # a stopped stack), so fail loudly instead.
        raise AgentError(500, "Cannot read the docker state (%s) -- retry. Without it I cannot tell "
                              "whether the stack must be stopped before changing the files." % ls_err)
    was_up = VERSIONS[version]["project"] in (projects or [])
    if was_up:
        job.log("Stopping the stack to change the config files...")
        version_state_set(version, strict=True, runningOk=False)  # 507 before the down
        _compose_stream(stack_dir(version), job, "down", prefix="  ")
    if action == "revert":
        n = revert_files(version, job, {"txt_fix"})
        version_state_set(version, txtFixes="pending")
    else:
        n = install_files(version, job, {"txt_fix"})
        version_state_set(version, txtFixes="applied")
    job.log("%d files %s." % (n, "restored" if action == "revert" else "installed"))
    if was_up:
        job.log("Restarting the stack...")
        # The same staged start as /server/start: a cold `up -d` loses the race with mysqld
        # (nodeserver exits 0, on-failure ignores it and half the stack stays dead).
        rc = _compose_stream(stack_dir(version), job, "up", "-d", "mysql", "redis", prefix="  ")
        if rc != 0:
            raise AgentError(500, "docker compose up mysql/redis failed (code %d)." % rc)
        try:
            wait_for_mysql(version, job, timeout=120)
        except AgentError as e:
            job.log("Could not verify MySQL (%s) -- starting the remaining services anyway." % e.message)
        rc = _compose_stream(stack_dir(version), job, "up", "-d", prefix="  ")
        if rc != 0:
            raise AgentError(500, "Could not restart the stack (code %d)." % rc)
        verify_stack(version, job)
        version_state_set(version, runningOk=True)
    return {"files": n, "action": action}


def do_events(version, job):
    """Re-apply just the event schedule. Deliberately separate from provisioning: changing which
    events run must not cost the player the progress they made since."""
    require_state_readable()
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server." % version)
    if VERSIONS[version]["project"] not in (running_projects()[0] or []):
        raise AgentError(409, "Version %s is not running -- start it and the events are applied "
                              "automatically at start." % version)
    wait_for_mysql(version, job, timeout=120)
    man = read_manifest(version) or {}
    if man.get("sql_events"):
        run_sql(version, job, man["sql_events"], tolerate=True)
    apply_events(version, job)
    apply_tower_schedule(version, job)  # same restart below picks the refreshed calendar up
    # RELOAD_CONFIG_INTERVAL=-1: the servers read the schedule once, at startup. Without this the
    # change would sit in the database and the game would keep showing the old events.
    job.log("Restarting the services that keep the config in memory (connected players are "
            "kicked)...")
    # agent 3.7: a stop, then the start -- in between no player is loaded anywhere, so a save that
    # carries a settled record of an event this list opens can be repaired (repair_settled_activities;
    # the gameserver writes its players back while it stops, and every write is md5-guarded).
    rc = _compose_stream(stack_dir(version), job, "stop", "gameserver", "multiserver", prefix="  ")
    if rc == 0:
        repair_settled_activities(version, job)
    # Started whatever the stop said: a failed stop must not leave the services half down.
    rc = _compose_stream(stack_dir(version), job, "start", "gameserver", "multiserver", prefix="  ") or rc
    if rc != 0:
        raise AgentError(500, "The events were written, but restarting the services failed "
                              "(code %d) -- restart the version to apply them." % rc)
    tower_mark_applied(version)
    # The two services just rebooted read the schedule they were handed -- make sure they LIVED
    # through reading it before reporting success (a torn schedule kills the gameserver ~50s in).
    verify_stack(version, job)
    return {"events": True, "restarted": True}


def _netfix_guard(version):
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server." % version)
    if compose_binds_loopback(version):
        # Netfix cannot help a poisoned render (it never touches docker-compose.yml) and it is the
        # intuitive first click when clients cannot connect -- point at the real repair instead.
        raise AgentError(409, "The %s stack is rendered with its ports on 127.0.0.1 (the archive's "
                              "original .env) -- the advertised IP is not the problem. Run Prepare "
                              "server." % version)


def _restart_ip_services(version, job):
    job.log("Restarting the services that keep the IP in memory...")
    _compose_stream(stack_dir(version), job, "restart", "dispatch", "gateserver", "gameserver",
                    "muipserver", prefix="  ")


def do_netfix(version, job):
    _netfix_guard(version)
    up = VERSIONS[version]["project"] in (running_projects()[0] or [])
    apply_advertised_ip(version, job, with_sql=up)
    # The hotpatch mirror URL is derived from the advertised IP: a changed IP means new URL columns.
    apply_hotpatch(version, job, with_sql=up)
    if not up:
        job.log("The stack is stopped -- only the XML files were updated. The database is updated "
                "at the next start.")
        return {"xml": True, "sql": False}
    _restart_ip_services(version, job)
    sql_ok = advertised_sql_current(version)
    if not sql_ok:
        job.log("WARNING: the database half of the address rewrite did not complete (see above) -- the "
                "next start or netfix retries it.")
    return {"xml": True, "sql": sql_ok}


# -- archive extraction (shared by the server package download and the hotpatch bundles) --
# Both features pull a .7z from archive.org and unpack it with whatever 7z-capable tool the box has:
# 7zz/7z/7za/7zr (the 7zip / p7zip packages) or bsdtar (libarchive-tools) on Linux; 7-Zip, Bandizip's
# bz.exe or the built-in System32\tar.exe (bsdtar -- only builds with liblzma read LZMA2 .7z; old
# Windows 10 builds lack it and would waste the whole download) on Windows. The tool is run as a
# child process, polled, and killed on cancel / timeout.

class ExtractError(Exception):
    """The extractor failed, is missing, or ran out of time."""


class ExtractStopped(ExtractError):
    """should_stop() said so: the child was killed, the partial output is the caller's to remove."""


_extractor_probe = {}  # tool path -> bool: does this bsdtar/tar.exe list liblzma? (once per process)


def _extractor_kind(path):
    """"7z" (7zz/7z/7za/7zr/7z.exe), "bsdtar" (bsdtar, tar, tar.exe) or "bz" (Bandizip) by basename.

    Both separators, always: a Windows tool path (GIO_7Z, or the System32 tar.exe find_extractor picks)
    must classify the same way whatever OS is doing the classifying. os.path.basename does not split on
    '\\' under POSIX, so on Linux "C:\\Windows\\System32\\tar.exe" came back whole, missed the tar test
    and was driven with 7-Zip's arguments -- which is what made the selftest fail on the server box and,
    because install_agent.sh runs --selftest before it installs anything, aborted the upgrade."""
    base = os.path.basename(path.replace("\\", "/")).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base in ("bsdtar", "tar"):
        return "bsdtar"
    if base == "bz":
        return "bz"
    return "7z"


def _bsdtar_has_lzma(tool):
    """`<tool> --version` names liblzma -- the only bsdtar that can read the LZMA2 .7z archives."""
    hit = _extractor_probe.get(tool)
    if hit is None:
        try:
            r = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=15,
                               errors="replace", **_popen_kwargs())
            hit = "liblzma" in (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            hit = False
        _extractor_probe[tool] = hit
    return hit


def find_extractor():
    """(path, kind) of the first usable extractor, or None. GIO_7Z first (an explicit path); then
    Linux: 7zz, 7z, 7za, 7zr, bsdtar on PATH; Windows: 7-Zip's 7z.exe (Program Files, x86, the
    per-user install), 7z on PATH, Bandizip's bz.exe, then System32\\tar.exe by FULL path (Git's GNU
    tar shadows it on PATH). A bsdtar of any kind is accepted only when it lists liblzma."""
    candidates = []
    if EXTRACTOR_PATH:
        candidates.append(EXTRACTOR_PATH)
    if IS_WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [os.path.join(pf, "7-Zip", "7z.exe"), os.path.join(pf86, "7-Zip", "7z.exe")]
        if local:
            candidates.append(os.path.join(local, "Programs", "7-Zip", "7z.exe"))
        candidates += [shutil.which("7z"), os.path.join(pf, "Bandizip", "bz.exe"),
                       os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")]
    else:
        candidates += [shutil.which(n) for n in ("7zz", "7z", "7za", "7zr", "bsdtar")]
    for tool in candidates:
        if not tool or not os.path.isfile(tool):
            continue
        kind = _extractor_kind(tool)
        if kind == "bsdtar" and not _bsdtar_has_lzma(tool):
            continue
        return tool, kind
    return None


def extract_argv(tool, archive, dest, kind=None):
    """The command line per tool family. Pure, selftest-locked. 7z: no -bso0/-bsp0 (p7zip 9.20 on
    EPEL does not know them; the output is discarded anyway), -bd = no progress indicator."""
    kind = kind or _extractor_kind(tool)
    if kind == "bsdtar":
        return [tool, "-xf", archive, "-C", dest]
    if kind == "bz":
        return [tool, "x", "-y", "-o:" + dest, archive]
    return [tool, "x", "-y", "-bd", "-o" + dest, archive]


def extract_archive(archive, dest, job, should_stop=None, timeout=EXTRACT_TIMEOUT):
    """Unpack archive into dest (created) with find_extractor()'s tool. Polled every 0.5 s: a
    should_stop() that turns true kills the child (ExtractStopped), the timeout too (ExtractError),
    a non-zero exit is ExtractError with the tool's last output. Returns the elapsed seconds.
    Module-level so the selftest can replace it through globals(), like _fetch_to_file."""
    found = find_extractor()
    if found is None:
        raise ExtractError("no 7z extractor on this box")
    tool, kind = found
    os.makedirs(dest, exist_ok=True)
    argv = extract_argv(tool, archive, dest, kind)
    job.log("$ %s x %s -> %s" % (os.path.basename(tool), archive, dest))
    started = time.time()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             **_popen_kwargs())
    except OSError as e:
        raise ExtractError("%s: %s" % (tool, e))
    tail = []

    def drain():
        # A pipe nobody reads would block a chatty p7zip after 64 KiB; keep only the end for the
        # error text.
        try:
            buf = b""
            for chunk in iter(lambda: p.stdout.read(4096), b""):
                buf = (buf + chunk)[-2048:]
            tail.append(buf)
        except Exception:  # noqa: BLE001 -- the pipe closing under a kill
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    while True:
        rc = p.poll()
        if rc is not None:
            break
        if should_stop is not None and should_stop():
            _kill_quiet(p)
            raise ExtractStopped("stopped on request")
        if time.time() - started > timeout:
            _kill_quiet(p)
            raise ExtractError("timed out after %d s" % timeout)
        time.sleep(0.5)
    t.join(5)
    if rc != 0:
        text = (tail[0] if tail else b"").decode("utf-8", "replace").strip().replace("\r", "")
        text = " | ".join(l.strip() for l in text.splitlines()[-4:] if l.strip())
        raise ExtractError("%s exited with code %d%s" % (os.path.basename(tool), rc, (": " + text) if text else ""))
    return time.time() - started


def _kill_quiet(p):
    try:
        p.kill()
        p.wait(10)
    except Exception:  # noqa: BLE001 -- already gone
        pass


# -- official hotpatch mirror (the 2021 in-version client fixes, served from this box) --
# A private-server 1.6 client never gets its 2021 hotfixes: the vendor dispatch advertises no
# resource/data revision (server/data/version.txt = "{}", no server/res/PC_version.txt, the four
# URL columns of t_region_config empty), so the client's own downloader has nothing to do.
# Enabling the hotpatch makes the agent (1) mirror the official output files from miHoYo's CDN
# into HOTPATCH_DIR in the CDN layout, every file md5/size-verified against the manifest -- whose
# values were copied from the official index files, the client's own trust anchor -- (2) serve
# them token-less under /hotpatch/<path> and (3) write the two dispatch config files + the four
# URL columns, so every client that logs in hotpatches itself into GenshinImpact_Data/Persistent
# with its own downloader, exactly like in 2021. The mirror MUST be a non-game host: the
# launcher's Fiddler redirects *.yuanshen.com to the private sdk, so the official URL can never be
# advertised to a Relic-driven client. dispatch and gameserver read the files + columns ONCE at
# boot (RELOAD_CONFIG_INTERVAL=-1): every change is followed by a restart of the config-holding
# services, or deferred ("pending") to the next start when the stack is down.

HOTPATCH_TIMEOUT = 60                # seconds per upstream request (socket-level, urllib)
HOTPATCH_ONDEMAND_FILE_CAP = 64 << 20  # an unlisted miss has no manifest size to trust: small cap
HOTPATCH_ONDEMAND_PARALLEL = 4       # upstream transfers the token-less route may run at once
HOTPATCH_WAIT = 25                   # seconds a request waits for a fetch of the SAME file (game GET timeout: 30)
HOTPATCH_CHUNK = 1 << 20
# One write to a player's socket: Handler.timeout (20 s) applies to EACH sendall, so 1 MiB writes
# cut every connection slower than ~51 KiB/s mid-body; 256 KiB lowers that floor to ~13 KiB/s.
HOTPATCH_SEND_CHUNK = 256 << 10
HOTPATCH_SERVICES = ("dispatch", "gateserver", "gameserver", "multiserver", "muipserver")
# The real CDN paths are <= 9 segments / ~120 characters; anything bigger is not a game file.
HOTPATCH_PATH_MAX, HOTPATCH_SEGMENTS_MAX, HOTPATCH_SEGMENT_MAX = 300, 12, 128
# The mirror URL ends up in a varchar column AND in the protobuf the client parses: a strict
# allowlist (scheme, host or [v6], optional port, an unreserved path) -- never a quote, a
# backslash, a space or a query string.
HOTPATCH_URL_RE = re.compile(r"^https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?)"
                             r"(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$")
PC_VERSION_TXT = os.path.join("server", "res", "PC_version.txt")
DATA_VERSION_TXT = os.path.join("server", "data", "version.txt")
SERVER_DATA_VERSION_TXT = os.path.join("server", "data", "server_data_version.txt")
_hotpatch_fetch_locks = {}   # CDN path -> Lock: one fetch per path at a time
_hotpatch_locks_guard = threading.Lock()
_hotpatch_ondemand_sem = threading.BoundedSemaphore(HOTPATCH_ONDEMAND_PARALLEL)
_ondemand_lock = threading.Lock()
_ondemand_bytes = None   # bytes of unlisted (on-demand) files under HOTPATCH_DIR; None = not walked yet
# Voice packs of the BASE build (see "voice packs" below): "<Language>/<file>.pck", one folder deep.
VOICE_PACK_RE = re.compile(r"^([A-Za-z][A-Za-z0-9()_\-]{0,31})/([A-Za-z0-9][A-Za-z0-9._\-]{0,63}\.pck)$")
VOICE_INDEX_FILES = ("release_res_versions_external", "res_versions_external")
VOICE_LANGUAGES_MAX = 8
_voice_index_cache = {}  # local index path -> ((mtime_ns, size, manifest md5), {language: [entry]})
_voice_index_lock = threading.Lock()
_voice_refused = {}      # (version, language) -> {"count", "last", "logged"}: what players asked for in vain


def _sql_q(s):
    """Quote a value for a single-quoted MySQL literal: the backslash is an escape too in the
    default sql_mode, so "'" -> "''" alone would let \\' break out of the string."""
    return str(s).replace("\\", "\\\\").replace("'", "''")


def _log_rel(rel):
    """A request path in a log line: bounded, so a caller cannot pad the log."""
    return rel if len(rel) <= 200 else rel[:200] + "...(%d chars)" % len(rel)


def read_hotpatch_manifest(version):
    """payloads/<version>/hotpatch.json, or None when this version ships no hotpatch."""
    path = os.path.join(payload_dir(version), "hotpatch.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def hotpatch_res_root(man):
    """CDN-relative root of the game-res output (index files live right under it)."""
    return "client_game_res/%s/%s/client/%s/" % (man["branch"], man["res"]["output"],
                                                  man["res"].get("platform", "StandaloneWindows64"))


def hotpatch_data_root(man, silence=False):
    part = man["silence"] if silence else man["data"]
    return "client_design_data/%s/%s/%s/General/AssetBundles/" % (
        man["branch"], part["output"], "client_silence" if silence else "client")


# -- voice packs of the BASE build (agent 3.3) --
# The hotpatch CONTENT is language-complete: the changed text blocks of all 13 text languages and
# the only changed voice packs (1.6 English(US)/VO_1.6_11.pck, 2.8 Chinese/VO_2.8_0.pck) are
# manifest files. What differs per player is the BASE build. Once real revisions are advertised,
# the client checks every pack of each ENABLED voice language on disk (StreamingAssets/Audio/
# GeneratedSoundBanks/Windows/<Lang>/<pck> must exist with the index size) and downloads a missing
# one -- and "enabled" is the lines of Persistent/audio_lang_14 plus the current voice setting,
# both seeded from the WINDOWS DISPLAY LANGUAGE on a fresh profile (Japanese / Korean / Chinese
# pick that voice, everything else English(US)), never from what is installed. An unchanged pack
# is isPatch:false, which the client fetches from the BASE output named by base_revision
# (`res.base` in the manifest): 84-111 files, 3.2-3.9 GB (1.6) or 7.3-9.4 GB (2.8) per language.
# The official CDN hosts them only there, so a mirror that refuses the base output turns "Relic
# installs English only + a Japanese Windows" into a download-error dialog at every login (and the
# in-game voice-pack download can never work). So the mirror serves those packs too, fenced twice:
#   * WHICH files: only "<Lang>/<file>.pck" lines of the mirrored, md5-verified release index that
#     are not isPatch (a patched pack lives in the advertised output; its base copy has another
#     md5) -- each fetched with the index's own md5 + size. Nothing else of the base output, ever.
#   * HOW MUCH disk: only the voice languages the ADMIN selected (versions.<v>.hotpatch.voice),
#     pre-mirrored by the `hotpatch-voice` job after a free-space check; a selected pack that is
#     still missing is fetched when a client asks for it (same md5 + size, same free-space check)
#     -- a FALLBACK only: the hotpatch must be enabled and on-demand fetching on, and the 2.8
#     client waits 30 s per GET, 6 tries, so a big pack does not arrive in time (_hotpatch_miss).
# A request for a language nobody selected stays a 404 and is counted for the admin panel.

def hotpatch_base_audio_root(man):
    """CDN-relative AudioAssets root of the BASE res output, or None when the manifest names none."""
    res = man.get("res") or {}
    base = str(res.get("base") or "")
    if not re.match(r"^output_\d{1,12}_[0-9a-f]{1,32}$", base):
        return None
    return "client_game_res/%s/%s/client/%s/AudioAssets/" % (
        man["branch"], base, res.get("platform", "StandaloneWindows64"))


def _parse_voice_index(path):
    """{language: [{"name": "<Lang>/<file>.pck", "md5", "size"}]} from one official res index: the
    .pck lines one language folder deep that are NOT isPatch. Anything malformed is skipped."""
    langs = OrderedDict()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.endswith("}") or ".pck" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("isPatch") is True:
                continue
            name, md5, size = row.get("remoteName"), row.get("md5"), row.get("fileSize")
            if not isinstance(name, str) or not isinstance(md5, str) or isinstance(size, bool) \
                    or not isinstance(size, int) or size <= 0 or not re.match(r"^[0-9a-fA-F]{32}$", md5):
                continue
            m = VOICE_PACK_RE.match(name)
            if m:
                langs.setdefault(m.group(1), []).append({"name": name, "md5": md5.lower(), "size": size})
    return langs


def hotpatch_voice_index(man):
    """The voice packs a client fetches from the BASE output, per language -- read from the mirrored
    release index of the ADVERTISED output, which is a manifest file: it is trusted only while its
    md5 is the manifest's (re-hashed when the file changes). {} while it is not mirrored yet."""
    if hotpatch_base_audio_root(man) is None:
        return {}
    root = hotpatch_res_root(man)
    for name in VOICE_INDEX_FILES:
        entry = next((f for f in man["files"] if f["path"] == root + name), None)
        if entry is None:
            continue
        path = hotpatch_local(entry["path"])
        st = _stat_or_none(path)
        if st is None or st.st_size != int(entry["size"]):
            continue
        key = (st.st_mtime_ns, st.st_size, entry["md5"].lower())
        with _voice_index_lock:
            hit = _voice_index_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        try:
            if _file_md5(path) != entry["md5"].lower():
                continue  # not the official index: it authorises nothing
            langs = _parse_voice_index(path)
        except OSError:
            continue
        with _voice_index_lock:
            if len(_voice_index_cache) > 16:
                _voice_index_cache.clear()
            _voice_index_cache[path] = (key, langs)
        return langs
    return {}


def hotpatch_voice_path(rel):
    """A CDN path under a known manifest's base AudioAssets root that names a voice pack ->
    (version, manifest, language, remoteName); None for everything else."""
    for v in VERSIONS:
        man = read_hotpatch_manifest(v)
        if man is None:
            continue
        root = hotpatch_base_audio_root(man)
        if root and rel.startswith(root):
            m = VOICE_PACK_RE.match(rel[len(root):])
            return (v, man, m.group(1), m.group(0)) if m else None
    return None


def hotpatch_voice_selected(version):
    """The voice languages the admin selected for this version ([] when none / state unreadable)."""
    try:
        st = version_state(version).get("hotpatch") or {}
    except AgentError:
        return []
    return [x for x in (st.get("voice") or []) if isinstance(x, str)]


def _note_voice_refused(version, lang):
    """A client asked for a pack of a voice language the mirror does not serve: counted for the
    admin panel (GET /server/hotpatch) and logged at most once an hour per language -- one such
    login asks for ~100 packs."""
    now = time.time()
    with _voice_index_lock:
        rec = _voice_refused.setdefault((version, lang), {"count": 0, "last": 0.0, "logged": 0.0})
        rec["count"] += 1
        rec["last"] = now
        if now - rec["logged"] < 3600:
            return
        rec["logged"] = now
        count = rec["count"]
    log_line("gio-agent: a client asked for the %s voice packs of %s, which this mirror does not serve "
             "(%d such requests so far): a player whose game voice language is %s has no such pack on "
             "disk and gets a download error at login. Mirror the language (hotpatch panel > Voice "
             "packs, POST /server/hotpatch/voice)." % (lang, version, count, lang))


def hotpatch_voice_expect(rel):
    """The token-less route's view of a base-output voice pack: (manifest, manifest-style entry) --
    md5 + size from the verified index, so the fetch is verified and needs no small cap -- when the
    path is a listed, non-patched pack of a language the admin selected on an ENABLED hotpatch;
    else None."""
    hit = hotpatch_voice_path(rel)
    if hit is None:
        return None
    version, man, lang, name = hit
    entry = next((e for e in hotpatch_voice_index(man).get(lang, ()) if e["name"] == name), None)
    if entry is None:
        return None
    try:
        st = version_state(version).get("hotpatch") or {}
    except AgentError:
        return None  # unreadable state: fail closed, like the policy does
    if not st.get("enabled") or lang not in (st.get("voice") or []):
        _note_voice_refused(version, lang)
        return None
    return man, {"path": rel, "md5": entry["md5"], "size": entry["size"], "voice": lang}


def _dir_bytes(path):
    total = 0
    for dp, _dn, fns in os.walk(path):
        for fn in fns:
            st = _stat_or_none(os.path.join(dp, fn))
            total += st.st_size if st is not None else 0
    return total


def _free_bytes_at(path):
    """Free bytes on the volume that holds path (its nearest existing ancestor); None when that
    cannot be read -- the callers then proceed without the disk guard."""
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _mirror_free_bytes():
    """Free bytes on the volume that holds HOTPATCH_DIR; None when unreadable (as before 3.3)."""
    return _free_bytes_at(HOTPATCH_DIR)


def hotpatch_voice_summary(version, man):
    """GET /server/hotpatch `voice`: per language the files/bytes a client would need, how much of
    it is cached (by size), whether the admin selected it and how often players asked for it in
    vain. Before the index is mirrored the manifest's own `voice` totals stand in."""
    base_root = hotpatch_base_audio_root(man)
    selected = hotpatch_voice_selected(version)
    out = {"base": (man.get("res") or {}).get("base") if base_root else None, "selected": selected,
           "indexed": False, "languages": [], "free": _mirror_free_bytes(), "reserve": HOTPATCH_DISK_RESERVE}
    if base_root is None:
        return out
    index = hotpatch_voice_index(man)
    rows = OrderedDict()
    if index:
        out["indexed"] = True
        for lang, entries in index.items():
            cached = cbytes = 0
            for e in entries:
                st = _stat_or_none(hotpatch_local(base_root + e["name"]))
                if st is not None and st.st_size == e["size"]:
                    cached += 1
                    cbytes += e["size"]
            rows[lang] = {"files": len(entries), "bytes": sum(e["size"] for e in entries),
                          "cached": cached, "cachedBytes": cbytes,
                          # everything in the language folder, a stopped run's .part included: what
                          # the job's purge option would delete
                          "diskBytes": _dir_bytes(hotpatch_local(base_root + lang))}
    else:
        for lang, tot in (man.get("voice") or {}).items():
            if isinstance(tot, dict) and VOICE_PACK_RE.match("%s/x.pck" % lang):
                rows[lang] = {"files": int(tot.get("files") or 0), "bytes": int(tot.get("bytes") or 0),
                              "cached": 0, "cachedBytes": 0,
                              "diskBytes": _dir_bytes(hotpatch_local(base_root + lang))}
    with _voice_index_lock:
        refused = {k[1]: dict(r) for k, r in _voice_refused.items() if k[0] == version}
    try:
        sources = (version_state(version).get("hotpatch") or {}).get("voiceSource") or {}
    except AgentError:
        sources = {}
    for lang, row in rows.items():
        r = refused.get(lang) or {}
        row.update(name=lang, selected=lang in selected, requested=int(r.get("count") or 0),
                   requestedAt=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["last"])) if r.get("last") else None,
                   # where the last mirroring run of this language got its packs (agent 3.4)
                   source=sources.get(lang) if isinstance(sources, dict) else None)
        out["languages"].append(row)
    return out


def _version_line(item, root):
    """One line of a version list, in the official index files' own style (", " / ": ")."""
    return json.dumps({"remoteName": item["path"][len(root):], "md5": item["md5"],
                       "fileSize": int(item["size"])}, separators=(", ", ": "))


def render_res_lines(man):
    """ResVersionConfig.md5: the version-list lines of every index file of the res output, CRLF
    separated (the official index files are CRLF), no trailing newline. The client looks up the
    "release_res_versions_external" and "base_revision" lines here and md5-checks the index files it
    downloads against them."""
    root = hotpatch_res_root(man)
    lines = [_version_line(f, root) for f in man["files"]
             if f.get("role") == "index" and f["path"].startswith(root)]
    if not lines:
        raise AgentError(500, "The hotpatch manifest lists no index file under %s." % root)
    return "\r\n".join(lines)


def render_pc_version(man):
    """server/res/PC_version.txt (protobuf-JSON ResVersionConfig). `branch` makes the dispatch
    append "/<branch>" to resource_url itself. Validated with json.loads: a malformed file is a
    boot failure of dispatch AND gameserver."""
    text = json.dumps({"version": int(man["res"]["revision"]), "md5": render_res_lines(man),
                       "release_total_size": "0", "version_suffix": man["res"]["suffix"],
                       "branch": man["branch"]})
    json.loads(text)
    return text


def _data_versions_line(man, silence):
    root = hotpatch_data_root(man, silence)
    for f in man["files"]:
        if f.get("role") == "index" and f["path"] == root + "data_versions":
            return _version_line(f, root)
    raise AgentError(500, "The hotpatch manifest has no data_versions entry under %s." % root)


def render_version_txt(man, server_rev):
    """server/data/version.txt (protobuf-JSON DataVersionConfig). The md5 maps are keyed by the
    platform name the client sends ("PC"); `server` keeps the stack's own server_data_version."""
    text = json.dumps({"server": int(server_rev), "client": int(man["data"]["revision"]),
                       "client_silence": int(man["silence"]["revision"]),
                       "client_md5": {"PC": _data_versions_line(man, False)},
                       "client_silence_md5": {"PC": _data_versions_line(man, True)},
                       "client_version_suffix": man["data"]["suffix"],
                       "client_silence_version_suffix": man["silence"]["suffix"]})
    json.loads(text)
    return text


def hotpatch_local(rel):
    """Where a CDN-relative path lives in the mirror."""
    return os.path.join(HOTPATCH_DIR, *rel.split("/"))


_served_roots_cache = [None, ()]  # [(manifest path, mtime_ns, size) per version, the roots derived from them]
_served_roots_lock = threading.Lock()


def hotpatch_served_roots():
    """The top of every subtree the mirror may serve: "<first segment>/<branch>/" of every path the loaded
    hotpatch manifests name -- each listed file, the advertised res / data / silence outputs and the base
    output's voice packs (hotpatch_base_audio_root) -- so client_game_res/1.6_live/ and friends, DERIVED from
    the manifests rather than listed here. The token-less route serves nothing outside them: even a mirror
    folder that also holds other files (a hand-set GIO_HOTPATCH_DIR over an agent or stack folder) exposes
    none of those. Cached per manifest file (path, mtime, size); a manifest that does not parse adds nothing."""
    key = []
    for v in list(VERSIONS):
        path = os.path.join(payload_dir(v), "hotpatch.json")
        st = _stat_or_none(path)
        key.append((path, st.st_mtime_ns, st.st_size) if st is not None else (path, None, None))
    key = tuple(key)
    with _served_roots_lock:
        if _served_roots_cache[0] == key:
            return _served_roots_cache[1]
    roots = set()
    for v in list(VERSIONS):
        try:
            man = read_hotpatch_manifest(v)
            if man is None:
                continue
            paths = [f["path"] for f in man.get("files") or []]
            paths += [hotpatch_res_root(man), hotpatch_data_root(man), hotpatch_data_root(man, silence=True)]
            base = hotpatch_base_audio_root(man)
            if base:
                paths.append(base)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # a broken manifest serves nothing (read_hotpatch_manifest's callers report it)
        for p in paths:
            parts = str(p).split("/")
            if len(parts) >= 3 and parts[0] and parts[1] and not parts[0].startswith(".") \
                    and parts[1] not in (".", ".."):
                roots.add("%s/%s/" % (parts[0], parts[1]))
    out = tuple(sorted(roots))
    with _served_roots_lock:
        _served_roots_cache[0], _served_roots_cache[1] = key, out
    return out


def safe_hotpatch_path(raw):
    """A request path after "/hotpatch/" -> (cdn_rel, local_abs), or None for anything that is
    not a plain relative file path confined to HOTPATCH_DIR: "..", a drive/colon, a backslash, a
    leading slash, an empty segment, a segment starting with "." (.env, .relic-*, .git: never a CDN
    name), control characters, a path outside the manifests' subtrees (hotpatch_served_roots), or a
    realpath outside the mirror."""
    try:
        rel = urllib.parse.unquote(raw or "")
    except Exception:  # noqa: BLE001
        return None
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    if len(rel) > HOTPATCH_PATH_MAX or any(ord(c) < 32 or ord(c) == 127 for c in rel):
        return None
    parts = rel.split("/")
    if len(parts) > HOTPATCH_SEGMENTS_MAX or any(len(p) > HOTPATCH_SEGMENT_MAX for p in parts):
        return None  # a miss creates directories: bound how deep/long a caller can make them
    if any(p in ("", ".", "..") or ".." in p or p.startswith(".") for p in parts):
        return None
    if any(p.endswith((".", " ")) or "~" in p for p in parts):
        return None  # no CDN name looks like this; on Windows these are ALIASES of other files (8.3, "x.")
    if not rel.startswith(hotpatch_served_roots()):
        return None  # only the manifests' own subtrees: nothing else under the folder is ever served
    local = hotpatch_local(rel)
    root = os.path.realpath(HOTPATCH_DIR)
    try:
        real = os.path.realpath(local)
        if os.path.commonpath([root, real]) != root:
            return None
    except ValueError:  # different drives on Windows -- certainly outside
        return None
    if rel.lower().endswith(".part") or real.lower().endswith(".part"):
        return None  # a transfer's temp file (kept across a network cut since 3.3): never a CDN name
    return rel, local


def parse_range(header, size):
    """A single-range `Range: bytes=a-b | a- | -n` -> (start, end) inclusive; None when absent
    or not understood (serve the whole file, RFC 7233 says to ignore it); AgentError(416) when
    the range is syntactically fine but unsatisfiable."""
    if not header:
        return None
    # Bounded digit runs: int() of a 5000-digit string raises on Python >= 3.11 (and no file is
    # anywhere near 10**19 bytes) -- a longer number is simply "not understood".
    m = re.match(r"^\s*bytes\s*=\s*(\d{0,19})\s*-\s*(\d{0,19})\s*$", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):  # suffix: the last n bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            raise AgentError(416, "range not satisfiable")
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        raise AgentError(416, "range not satisfiable")
    return start, min(end, size - 1)


def hotpatch_upstream_enabled():
    return HOTPATCH_UPSTREAM and bool(get_policy().get("hotpatchUpstream", True))


def hotpatch_source():
    """Where the mirror is filled from: "archive" (the archive.org bundles, then the CDN for what
    is left) or "cdn" (the official CDN alone, file by file). The admin's stored choice wins; a
    policy that cannot be read or holds something else falls back to what the environment seeded."""
    try:
        val = get_policy().get("hotpatchSource", HOTPATCH_SOURCE)
    except Exception:
        return HOTPATCH_SOURCE
    if isinstance(val, str) and val.strip().lower() in HOTPATCH_SOURCES:
        return val.strip().lower()
    return HOTPATCH_SOURCE


def hotpatch_public_url(version, man, strict=True):
    """The base URL players fetch the mirror from ("<url>/<cdn path>"). GIO_HOTPATCH_URL wins (a
    reverse proxy, another port, https); otherwise the agent's own listener on the advertised IP
    (or the stack's OUTER_IP). Returns (url, problem) -- (None, why) when players could not reach
    it; strict=True turns that into a 409 (enabling must refuse, not advertise a dead URL)."""
    problem = None
    if HOTPATCH_URL:
        url = HOTPATCH_URL
    else:
        host = ADVERTISED_IP or read_env(version).get("OUTER_IP", "").strip()
        if ADVERTISED_HOST and not ADVERTISED_IP:
            # The LAN OUTER_IP is not a fallback here: it would put a URL no remote player reaches
            # into the columns every client reads.
            problem = ("GIO_ADVERTISED_HOST %s has not resolved to a usable address yet -- the mirror "
                       "URL follows it once it does (or set GIO_HOTPATCH_URL)." % ADVERTISED_HOST)
        elif _is_loopback(host):
            problem = ("no address players could reach: set GIO_ADVERTISED_IP (or a non-loopback "
                       "OUTER_IP in the stack's .env), or point GIO_HOTPATCH_URL at a mirror URL players "
                       "can reach.")
        elif _is_loopback(LISTEN_HOST):
            problem = ("the agent listens on loopback only (GIO_AGENT_LISTEN=%s:%d, tunnel mode) -- "
                       "players could not reach the mirror. Listen on 0.0.0.0 or set GIO_HOTPATCH_URL."
                       % (LISTEN_HOST, LISTEN_PORT))
        url = "http://%s:%d/hotpatch" % ("[%s]" % host if ":" in host else host, LISTEN_PORT)
    if not problem and not HOTPATCH_URL_RE.match(url):
        # The value lands in a SQL literal and in the protobuf every client parses: allowlist it.
        problem = ("the mirror URL %r is not a plain http(s)://host[:port]/path URL (letters, digits, "
                   ". - _ ~ / only)." % url)
    if not problem:
        longest = max(len(url + "/client_game_res"), len(url + "/client_design_data/" + man["branch"]))
        if longest > 128:
            problem = ("the mirror URL is too long (%d characters; t_region_config columns are "
                       "varchar(128))." % longest)
    if problem:
        if strict:
            raise AgentError(409, "Cannot enable the hotpatch mirror: " + problem)
        return None, problem
    return url, None


def hotpatch_file_state(man):
    """(total, cached, bytes, cachedBytes) -- by size only; the md5 re-check (126 MB) is the job's."""
    total = cached = nbytes = cbytes = 0
    for f in man["files"]:
        size = int(f["size"])
        total += 1
        nbytes += size
        st = _stat_or_none(hotpatch_local(f["path"]))
        if st is not None and st.st_size == size:
            cached += 1
            cbytes += size
    return total, cached, nbytes, cbytes


def hotpatch_brief(version, vs=None):
    """The per-version status() entry."""
    st = (vs if vs is not None else version_state(version)).get("hotpatch") or {}
    man = read_hotpatch_manifest(version)
    out = {"available": man is not None, "enabled": bool(st.get("enabled")),
           "pending": bool(st.get("pending")), "complete": False,
           "res": None, "data": None, "silence": None,
           "voice": [x for x in (st.get("voice") or []) if isinstance(x, str)],
           # where the last Enable got its files: cached | bundle | cdn | bundle+cdn (agent 3.4)
           "source": st.get("source") if isinstance(st.get("source"), str) else None}
    if man is not None:
        total, cached, _b, _cb = hotpatch_file_state(man)
        out.update(complete=(total > 0 and cached == total), res=man["res"]["revision"],
                   data=man["data"]["revision"], silence=man["silence"]["revision"])
    return out


def hotpatch_info(version):
    """GET /server/hotpatch: everything the admin panel shows."""
    check_known(version)
    man = read_hotpatch_manifest(version)
    st = version_state(version).get("hotpatch") or {}
    out = {"version": version, "available": man is not None, "enabled": bool(st.get("enabled")),
           "pending": bool(st.get("pending")), "complete": False, "url": None, "problem": None,
           "branch": None, "res": None, "data": None, "silence": None,
           "files": {"total": 0, "cached": 0, "bytes": 0, "cachedBytes": 0},
           "appliedAt": st.get("appliedAt"), "dir": HOTPATCH_DIR,
           "upstream": hotpatch_upstream_enabled(), "voice": None,
           # agent 3.4: where the last Enable got its files, where the NEXT one will look first
           # (the admin's policy choice), and the archive.org bundles' state
           "source": st.get("source") if isinstance(st.get("source"), str) else None,
           "mirrorSource": hotpatch_source(),
           "bundles": hotpatch_bundles_info(man)}
    if man is None:
        return out
    out["voice"] = hotpatch_voice_summary(version, man)
    total, cached, nbytes, cbytes = hotpatch_file_state(man)
    out.update(branch=man["branch"], complete=(total > 0 and cached == total),
               files={"total": total, "cached": cached, "bytes": nbytes, "cachedBytes": cbytes})
    for k in ("res", "data", "silence"):
        out[k] = {"output": man[k]["output"], "revision": man[k]["revision"], "suffix": man[k]["suffix"]}
    if is_configured(version):
        out["url"], out["problem"] = hotpatch_public_url(version, man, strict=False)
    return out


# -- TLS (agent 3.6) --
# Since Python 3.13 ssl.create_default_context() sets VERIFY_X509_STRICT. archive.org serves leaf ->
# "Go Daddy Secure CA G2" -> the cross-signed "Go Daddy Root CA G2" -> the 2004 "Go Daddy Class 2 CA",
# whose basicConstraints is not marked critical. A Windows store that holds only the Class 2 root
# (Windows fetches the others on demand; OpenSSL never triggers that) ends the path there, and STRICT
# rejects it: CERTIFICATE_VERIFY_FAILED "Basic Constraints of CA cert not marked critical". Linux stores
# carry the G2 root. The fix keeps FULL verification (chain to a trusted root, host name, expiry) and
# drops only that flag. Every request goes through the opener install_http_opener() installs -- never
# pass context= to urlopen: it builds an opener with the default ProxyHandler, which reads the WINDOWS
# system proxy (the launcher's Fiddler).

def tls_context_and_error(ca_file=None):
    """(the verifying context of every outbound HTTPS request, why ca_file did not load or None): the
    stdlib default (CERT_REQUIRED + check_hostname, TRUSTED_FIRST / PARTIAL_CHAIN kept) minus
    VERIFY_X509_STRICT, plus ca_file's roots when given -- a failure there is a WARNING and a context
    without them, never a half-loaded bundle. (None, reason-or-None) without ssl."""
    if ssl is None:
        return None, ("this Python has no ssl module" if ca_file else None)
    ctx = ssl.create_default_context()
    ctx.verify_flags = int(ctx.verify_flags) & ~int(getattr(ssl, "VERIFY_X509_STRICT", 0))
    if ca_file:
        try:
            ctx.load_verify_locations(cafile=ca_file)
        except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
            log_line("gio-agent: WARNING: GIO_CA_FILE=%s could not be loaded (%s) -- ignored." % (ca_file, e))
            return tls_context_and_error(None)[0], (str(e) or e.__class__.__name__)[:200]
    return ctx, None


def build_tls_context(ca_file=None):
    """tls_context_and_error's context alone. None without ssl."""
    return tls_context_and_error(ca_file)[0]


_TLS_CONTEXT = None  # what install_http_opener() installed (GET /agent/config reports its STRICT bit)
_TLS_CA_LOADED = ""  # the GIO_CA_FILE bundle that context really carries ("" = none): GET /agent/config tls.caFile
_TLS_CA_ERROR = None  # why GIO_CA_FILE did not load into it (the opener runs without it): tls.caFileError


def install_http_opener():
    """The process-wide urllib opener: only *_proxy environment variables (never the Windows system
    proxy) and the TLS context above. main() calls it once; POST /agent/config again when GIO_CA_FILE
    changes. Records whether GIO_CA_FILE really loaded (_TLS_CA_LOADED / _TLS_CA_ERROR): a hand-set
    value is never validated before this, and a validated file can change before the next start."""
    global _TLS_CONTEXT, _TLS_CA_LOADED, _TLS_CA_ERROR
    ca_file = CA_FILE or None
    ctx, err = tls_context_and_error(ca_file)
    handlers = [urllib.request.ProxyHandler(urllib.request.getproxies_environment())]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    urllib.request.install_opener(urllib.request.build_opener(*handlers))
    _TLS_CONTEXT = ctx
    _TLS_CA_LOADED = ca_file if (ca_file and ctx is not None and err is None) else ""
    _TLS_CA_ERROR = err if ca_file else None


def _tls_failure(e):
    """The ssl.SSLCertVerificationError behind e (itself, its .reason -- urllib's URLError --, its
    __cause__ / __context__; a bounded walk), else None. Test this BEFORE any ValueError branch: the
    class is an OSError AND a ValueError."""
    if ssl is None:
        return None
    todo, seen = [e], set()
    while todo and len(seen) < 8:
        x = todo.pop(0)
        if not isinstance(x, BaseException) or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, ssl.SSLCertVerificationError):
            return x
        todo.extend((getattr(x, "reason", None), x.__cause__, x.__context__))
    return None


def _tls_detail(cert_err):
    """What OpenSSL said, readable (a hand-built instance has no verify_message)."""
    msg = getattr(cert_err, "verify_message", None) or str(cert_err) or cert_err.__class__.__name__
    return str(msg)[:160]


class TlsVerifyError(OSError):
    """The server's certificate could not be verified. An OSError (never a ValueError, so no checksum
    branch catches it), raised at once: retrying cannot change a certificate."""

    def __init__(self, host, detail):
        super().__init__("the TLS certificate of %s could not be verified (%s)" % (host, detail))
        self.host, self.detail = host, detail


_TLS_FALLBACK_HOSTS = set()  # hosts a pinned download reached UNVERIFIED (GET /agent/config reports them)
_tls_lock = threading.Lock()
_tls_fallback_opener = [None]


def _note_tls_fallback(host, detail=None):
    """Record a host reached over the unverified fallback; a WARNING the first time per process."""
    with _tls_lock:
        first = host not in _TLS_FALLBACK_HOSTS
        _TLS_FALLBACK_HOSTS.add(host)
    if first:
        # Recorded on the ATTEMPT (GET /agent/config fallbackHosts = hosts reached unverified): whether the
        # download is then accepted is the md5 + size check's call, so this line promises nothing more.
        log_line("gio-agent: WARNING: the TLS certificate of %s could not be verified%s -- this pinned download "
                 "continues over an UNVERIFIED connection; only a file whose md5 + size match is kept "
                 "(GIO_TLS_PINNED_FALLBACK=0 turns this off; GIO_CA_FILE adds a missing root)."
                 % (host, " (%s)" % detail if detail else ""))


def _open_pinned_fallback(req, timeout):
    """Open req over TLS WITHOUT certificate verification -- only for a download whose md5 + size are
    pinned beforehand (_open_url). Module level so the selftest can swap it. Same proxy rule as the
    main opener; every host it connects to (a redirect's too) is recorded."""
    with _tls_lock:
        opener = _tls_fallback_opener[0]
        if opener is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False  # before verify_mode: CERT_NONE is refused while it is on
            ctx.verify_mode = ssl.CERT_NONE

            class _RecordingHTTPSHandler(urllib.request.HTTPSHandler):
                def https_open(self, r):
                    _note_tls_fallback(urllib.parse.urlsplit(r.full_url).hostname or r.host)
                    return super().https_open(r)

            opener = _tls_fallback_opener[0] = urllib.request.build_opener(
                urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
                _RecordingHTTPSHandler(context=ctx))
    return opener.open(req, timeout=timeout)


def _open_url(req, timeout, pinned=False):
    """urllib.request.urlopen(req, timeout=...) -- looked up at call time, the selftest's fakes take
    exactly that -- with a certificate failure made explicit: pinned + GIO_TLS_PINNED_FALLBACK -> the
    unverified fallback (the caller's md5 + size check proves the bytes), else TlsVerifyError.
    HTTPError and every other failure propagate untouched."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception as e:  # noqa: BLE001 -- only a certificate failure is handled here
        cert = _tls_failure(e)
        if cert is None:
            raise
        host = urllib.parse.urlsplit(req.full_url).hostname or "?"
        detail = _tls_detail(cert)
        if not (pinned and TLS_PINNED_FALLBACK and ssl is not None):
            raise TlsVerifyError(host, detail) from e
    # Outside the except block: a network failure of the fallback must not carry the certificate error
    # as its __context__ (_tls_failure would read it as another certificate failure).
    _note_tls_fallback(host, detail)
    return _open_pinned_fallback(req, timeout)


def _short_err(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code  # keep: the /hotpatch/ route greps "HTTP 404"
    if isinstance(e, TlsVerifyError):
        return ("TLS certificate not verified: %s (%s)" % (e.detail, e.host))[:200]
    cert = _tls_failure(e)
    if cert is not None:
        return ("TLS certificate not verified: %s" % _tls_detail(cert))[:200]
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)[:120]
    return str(e)[:120]


def _makedirs_tracked(path):
    """os.makedirs(path) that returns the directories it actually created, innermost first, so a
    failed fetch can take them away again (an anonymous miss must not leave directory trees)."""
    created = []
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        created.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(path, exist_ok=True)
    return created


def _prune_dirs(created):
    for d in created:  # innermost first; stop at the first one that is not empty
        try:
            os.rmdir(d)
        except OSError:
            break


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


class FetchRefused(ValueError):
    """The upstream DECLARES a body that can never pass (over the cap, not the expected size):
    nothing was streamed, and neither a retry nor the next upstream will change it."""


class FetchCut(OSError):
    """The NETWORK ended a transfer mid-body (stall, reset, short body). The only failure after
    which a resumable transfer keeps its .part."""


class FetchStopped(FetchCut):
    """The caller's should_stop() said so (a cancelled / yielding voice job): the .part stays, and
    no other try or upstream is attempted."""


class MirrorWriteError(OSError):
    """The LOCAL side failed (full disk, permissions, a vanished folder): no retry and no other
    upstream can help, and the .part must not stay on a disk that is already full."""


class MirrorBusyError(OSError):
    """The finished, verified .part could not replace dst because dst is open (Windows: a client
    is being served the old copy right now). Downloading again cannot help; a resumable transfer
    keeps the complete .part, which the next run adopts without a request."""


def _md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HOTPATCH_CHUNK), b""):
            h.update(chunk)
    return h


def _fetch_to_file(url, dst, timeout=HOTPATCH_TIMEOUT, cap=None, expect_md5=None, expect_size=None,
                   resume=False, should_stop=None, progress=None):
    """GET url into dst (tmp + rename), md5/size-verified when known. Returns the byte count;
    raises OSError (HTTP/network) or ValueError (verification) -- the caller picks the next try --
    or MirrorWriteError (the local disk: the caller stops). The upstream is opened BEFORE
    anything touches the disk: a 404 or a dead CDN creates no directory, and a transfer that
    fails half-way takes the directories it created away again.
    resume=True (the multi-hundred-MB voice packs, md5 + size known): a transfer the NETWORK cut
    (FetchCut) keeps its .part, and the next call continues it with a Range request. The kept
    bytes are hashed only once the upstream answered 206 -- a dead CDN costs no disk read -- and
    the md5 still covers the whole file, so a wrong splice is caught like any other corruption.
    should_stop() is polled per chunk (a cancelled voice job): FetchStopped, .part kept.
    progress(bytes_so_far) is called after every chunk written (the multi-GB bundle and stack
    downloads log a line from it; the callback throttles itself)."""
    tmp = dst + ".part"
    h = hashlib.md5()
    have = 0
    if resume and expect_size and expect_md5:
        st = _stat_or_none(tmp)
        if st is not None and st.st_size == expect_size:
            # complete, only the final rename never happened (the file was being served, a crash)
            if _md5_of(tmp).hexdigest() == expect_md5.lower():
                _finish_fetch(tmp, dst, expect_size, expect_md5)
                return expect_size
            _remove_quiet(tmp)
        elif st is not None and 0 < st.st_size < expect_size:
            have = st.st_size
    else:
        resume = False
    headers = {"User-Agent": "gio-agent/" + AGENT_VERSION}
    if have:
        headers["Range"] = "bytes=%d-" % have
    # pinned = the bytes are proven by md5 + size whatever the connection: only then may a certificate
    # that cannot be verified fall back to an unverified connection (never an unlisted on-demand miss)
    resp = _open_url(urllib.request.Request(url, headers=headers), timeout,
                     pinned=bool(expect_md5) and expect_size is not None)
    n = 0
    created = []
    try:
        with resp:
            if have and getattr(resp, "status", 200) != 206:
                have = 0  # the upstream ignored the Range: the body is the whole file
            try:
                declared = int((getattr(resp, "headers", None) or {}).get("Content-Length") or "")
            except (TypeError, ValueError):
                declared = None
            if declared is not None:
                if cap is not None and have + declared > cap:
                    raise FetchRefused("%d bytes, over the %d MiB cap" % (have + declared, cap >> 20))
                if expect_size is not None and have + declared != expect_size:
                    raise FetchRefused("%d bytes, expected %d" % (have + declared, expect_size))
            try:
                if have:
                    h = _md5_of(tmp)
                created = _makedirs_tracked(os.path.dirname(dst))
                f = open(tmp, "ab" if have else "wb")
            except OSError as e:
                raise MirrorWriteError(str(e))
            n = have
            try:
                while True:
                    if should_stop is not None and should_stop():
                        raise FetchStopped("stopped on request")
                    try:
                        chunk = resp.read(HOTPATCH_CHUNK)
                    except (OSError, http.client.HTTPException) as e:  # stall, reset, IncompleteRead
                        raise FetchCut("connection lost mid-transfer (%s)" % _short_err(e))
                    if not chunk:
                        break
                    n += len(chunk)
                    if cap is not None and n > cap:
                        raise ValueError("larger than the %d MiB cap" % (cap >> 20))
                    if expect_size is not None and n > expect_size:
                        raise ValueError("longer than the expected %d bytes" % expect_size)
                    h.update(chunk)
                    try:
                        f.write(chunk)
                    except OSError as e:
                        raise MirrorWriteError(str(e))
                    if progress is not None:
                        progress(n)
            finally:
                try:
                    f.close()
                except OSError as e:  # the flush of the last buffer: the disk again
                    raise MirrorWriteError(str(e))
        if expect_size is not None and n != expect_size:
            if n < expect_size:
                raise FetchCut("connection closed after %d of %d bytes" % (n, expect_size))
            raise ValueError("size %d, expected %d" % (n, expect_size))
        if expect_md5 and h.hexdigest() != expect_md5.lower():
            raise ValueError("md5 %s, expected %s" % (h.hexdigest(), expect_md5.lower()))
        _finish_fetch(tmp, dst, expect_size, expect_md5)
        return n
    except BaseException as e:
        # Only what the network cut short is worth keeping (or a complete .part that could not be
        # renamed yet), and only when the next call can verify it.
        if not (resume and n > 0 and isinstance(e, (FetchCut, MirrorBusyError))):
            _remove_quiet(tmp)
            _prune_dirs(created)
        raise


def _finish_fetch(tmp, dst, expect_size, expect_md5=None):
    """tmp -> dst. A rename that fails while dst already IS the file -- same size AND md5: another
    fetch won the race -- is no failure. A dst that exists but is NOT the file (the same-size
    corrupt copy a force re-fetch is there to repair) and cannot be replaced is open somewhere:
    MirrorBusyError. Anything else is the local disk."""
    try:
        replace_file(tmp, dst)
    except OSError as e:
        st = _stat_or_none(dst)
        if st is None:
            raise MirrorWriteError(str(e))
        try:
            same = bool(expect_md5) and st.st_size == expect_size and _md5_of(dst).hexdigest() == expect_md5.lower()
        except OSError:
            same = False
        if same:
            _remove_quiet(tmp)
            return
        raise MirrorBusyError("the file is in use (a client is being served it) -- run this again: %s" % e)


def _path_lock(rel):
    with _hotpatch_locks_guard:
        if len(_hotpatch_fetch_locks) > 4096:
            # bounded: an attacker probing thousands of distinct misses must not grow this forever
            for k in [k for k, v in _hotpatch_fetch_locks.items() if not v.locked()]:
                del _hotpatch_fetch_locks[k]
        lk = _hotpatch_fetch_locks.get(rel)
        if lk is None:
            lk = _hotpatch_fetch_locks[rel] = threading.Lock()
        return lk


def hotpatch_fetch(upstreams, rel, expect=None, cap=None, force=False, resume=False, should_stop=None):
    """Fetch one CDN-relative path from the upstreams in order (two tries each; a 404 moves on to
    the next upstream at once) into the mirror. `expect` = the manifest entry (md5 + size are
    verified) or None (an on-demand miss: only the size cap applies). Serialised per path, so two
    clients missing the same file cause one upstream fetch. resume=True continues a .part the
    network cut (voice packs). Returns the byte count or raises AgentError: 502 saying what every
    upstream answered, 507 when the LOCAL disk failed (no retry, no other upstream), 409 when
    should_stop() ended it."""
    dst = hotpatch_local(rel)
    with _path_lock(rel):
        st = _stat_or_none(dst)
        if st is not None and not force and (expect is None or st.st_size == int(expect["size"])):
            return st.st_size  # someone fetched (and verified) it while we waited for the lock
        errors = []
        for base in upstreams:
            url = base.rstrip("/") + "/" + urllib.parse.quote(rel, safe="/()")
            for attempt in (1, 2):
                if should_stop is not None and should_stop():
                    raise AgentError(409, "%s: stopped on request" % rel)
                try:
                    kw = {"resume": True} if resume else {}
                    if should_stop is not None:
                        kw["should_stop"] = should_stop
                    return _fetch_to_file(url, dst, cap=cap, expect_md5=(expect or {}).get("md5"),
                                          expect_size=int(expect["size"]) if expect else None, **kw)
                except FetchStopped:
                    raise AgentError(409, "%s: stopped on request" % rel)
                except MirrorWriteError as e:
                    raise AgentError(507, "%s: writing to the mirror folder failed: %s" % (rel, _short_err(e)))
                except MirrorBusyError as e:
                    raise AgentError(502, "%s: %s" % (rel, _short_err(e)))
                except TlsVerifyError as e:
                    errors.append("%s try %d: %s" % (base, attempt, _short_err(e)))
                    break  # a certificate that does not verify will not on a second try: next upstream
                except (OSError, ValueError) as e:
                    errors.append("%s try %d: %s" % (base, attempt, _short_err(e)))
                    if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                        break  # the file is not there; retrying will not make it appear
                    if isinstance(e, FetchRefused):
                        break  # the declared length can never pass: do not ask again (nor stream it)
        raise AgentError(502, "%s: %s" % (rel, "; ".join(errors)))


# -- archive.org bundles (agent 3.4): the same files as one .7z, tried BEFORE the per-file CDN path --
# archive.org holds the complete hotpatch output and the base-build voice packs of every language as
# one archive each (hotpatch.json `bundles`, md5/size from the item's <item>_files.xml). Third-party
# bytes: the archive itself is md5-pinned, it is unpacked into a staging folder OUTSIDE the mirror,
# and ONLY files whose md5 + size equal the manifest (or the md5-verified release index, for voice
# packs) are moved in -- nothing else can ever be served. A bundle that cannot be used, for whatever
# reason, is one log line and the per-file CDN path (3.3 behaviour). The anonymous /hotpatch/ route
# never pulls a bundle.

class BundleUnavailable(Exception):
    """Use the CDN for this run (404, network, md5 mismatch, extractor failure) -- never a job failure."""


def hotpatch_bundle_spec(man, lang=None):
    """{url, size, md5, name} of the manifest's hotpatch bundle (lang=None) or of a voice language's
    bundle; None when the manifest names none or the entry is malformed (a bad entry is silently a
    "no bundle", never a job failure). The url is passed to urlopen as stored (percent-encoded)."""
    bundles = man.get("bundles")
    if not isinstance(bundles, dict):
        return None
    if lang is None:
        raw, tag = bundles.get("hotpatch"), "hotpatch"
    else:
        voice = bundles.get("voice")
        raw = voice.get(lang) if isinstance(voice, dict) else None
        tag = "voice_" + re.sub(r"[^A-Za-z0-9_-]", "_", lang)
    if not isinstance(raw, dict):
        return None
    url, size, md5 = raw.get("url"), raw.get("size"), raw.get("md5")
    if not isinstance(url, str) or not re.match(r"^https?://[^\s\"'\\]+$", url):
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if not isinstance(md5, str) or not re.match(r"^[0-9a-fA-F]{32}$", md5):
        return None
    return {"url": url, "size": size, "md5": md5.lower(), "name": "%s_%s.7z" % (man["branch"], tag)}


def hotpatch_bundle_path(spec):
    return os.path.join(HOTPATCH_BUNDLE_DIR, spec["name"])


def hotpatch_bundle_state(spec):
    """{url, size, present, partial}: a complete archive on disk (by size; the md5 is checked when
    it is used) and the bytes of a kept .part."""
    p = hotpatch_bundle_path(spec)
    st, part = _stat_or_none(p), _stat_or_none(p + ".part")
    return {"url": spec["url"], "size": spec["size"],
            "present": st is not None and st.st_size == spec["size"],
            "partial": part.st_size if part is not None and part.st_size < spec["size"] else 0}


def hotpatch_bundle_remaining(spec):
    """Bytes a download of this bundle would still pull: 0 with a complete archive on disk, the
    rest of a kept .part otherwise."""
    s = hotpatch_bundle_state(spec)
    return 0 if s["present"] else spec["size"] - s["partial"]


def hotpatch_bundles_info(man):
    """GET /server/hotpatch `bundles`: the switch, the tool, the folder, and each bundle's state."""
    found = find_extractor()
    out = {"enabled": HOTPATCH_BUNDLES, "extractor": os.path.basename(found[0]) if found else None,
           "dir": HOTPATCH_BUNDLE_DIR, "hotpatch": None, "voice": {}}
    if man is None:
        return out
    spec = hotpatch_bundle_spec(man)
    if spec is not None:
        out["hotpatch"] = hotpatch_bundle_state(spec)
    voice = (man.get("bundles") or {}).get("voice") if isinstance(man.get("bundles"), dict) else None
    for lang in (voice or {}):
        vspec = hotpatch_bundle_spec(man, lang)
        if vspec is not None and VOICE_PACK_RE.match("%s/x.pck" % lang):
            out["voice"][lang] = hotpatch_bundle_state(vspec)
    return out


def hotpatch_bundle_plan(spec, need_bytes, total_bytes, free):
    """(True, None) when the bundle is the way to fill need_bytes of total_bytes, else (False, why):
    switched off, no bundle in the manifest, no extractor, less than BUNDLE_MIN_FRACTION missing
    (file by file is cheaper), or not enough disk for the archive's remaining bytes + the unpacked
    files + the reserve. The caller logs the reason once."""
    if not HOTPATCH_BUNDLES:
        return False, "bundles are switched off (GIO_HOTPATCH_BUNDLES=0)"
    if hotpatch_source() != "archive":
        return False, "the admin chose the official CDN as the hotpatch mirror source"
    if spec is None:
        return False, "this manifest names no bundle"
    if find_extractor() is None:
        return False, ("no 7-Zip / bsdtar tool on this box (install the 7zip or p7zip-full / libarchive-tools "
                       "package; Windows: 7-Zip or tar.exe)")
    if need_bytes < total_bytes * BUNDLE_MIN_FRACTION:
        return False, "only %.1f MB missing: file by file is cheaper" % (need_bytes / 1e6)
    if free is not None and free < hotpatch_bundle_remaining(spec) + need_bytes + HOTPATCH_DISK_RESERVE:
        return False, "not enough free disk for the bundle plus its unpacked files"
    return True, None


def hotpatch_bundle_fetch(spec, job, should_stop=None):
    """The bundle's local archive path, downloaded (resumable .part, md5 + size pinned, two tries)
    or reused when a verified copy is already there. AgentError 409 on should_stop, 507 when the
    LOCAL disk failed; everything the upstream did wrong (404, refused, cut, md5 mismatch -- the
    .part of a mismatch is already gone) is BundleUnavailable: the caller falls back to the CDN.
    A network cut keeps its .part for the next run."""
    dst, name, size = hotpatch_bundle_path(spec), spec["name"], spec["size"]
    st = _stat_or_none(dst)
    if st is not None:
        same = False
        if st.st_size == size:
            try:
                same = _file_md5(dst) == spec["md5"]
            except OSError:
                same = False
        if same:
            job.log("The %s bundle is already on disk and verified (%.1f MB) -- no download." % (name, size / 1e6))
            return dst
        _remove_quiet(dst)  # a stray file of the same name: not the bundle
    part = _stat_or_none(dst + ".part")
    job.log("Downloading the %s bundle (%.1f MB) from %s ..." % (name, size / 1e6, spec["url"]))
    if part is not None and 0 < part.st_size < size:
        job.log("  resuming at %.1f MB (the kept part is verified first)" % (part.st_size / 1e6))
    last = [0]

    def progress(n):
        if n - last[0] >= (64 << 20):
            last[0] = n
            job.log("  %.1f / %.1f MB" % (n / 1e6, size / 1e6))

    errors = []
    for attempt in (1, 2):
        if should_stop is not None and should_stop():
            raise AgentError(409, "%s: stopped on request" % name)
        try:
            _fetch_to_file(spec["url"], dst, timeout=HOTPATCH_TIMEOUT, expect_md5=spec["md5"], expect_size=size,
                           resume=True, should_stop=should_stop, progress=progress)
            return dst
        except FetchStopped:
            raise AgentError(409, "%s: stopped on request" % name)
        except MirrorWriteError as e:
            raise AgentError(507, "%s: writing the bundle failed: %s" % (name, _short_err(e)))
        except (OSError, ValueError, http.client.HTTPException) as e:
            errors.append("try %d: %s" % (attempt, _short_err(e)))
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                break  # not on archive.org (any more): asking again will not make it appear
            if isinstance(e, (FetchRefused, MirrorBusyError, ValueError, TlsVerifyError)):
                break  # a declared length / md5 / certificate that cannot pass: the same would come again
    raise BundleUnavailable("%s: %s" % (name, "; ".join(errors)))


def _move_into_mirror(src, dst):
    """rename, or copy + delete when the bundle folder sits on another volume. PermissionError
    (Windows: dst is being served right now) is the caller's; a failed copy is the local disk."""
    try:
        replace_file(src, dst)
        return
    except PermissionError:
        raise
    except OSError as e:
        if getattr(e, "errno", None) != errno.EXDEV:
            raise MirrorWriteError(str(e))
    try:
        shutil.copyfile(src, dst)
        os.remove(src)
    except OSError as e:
        raise MirrorWriteError(str(e))


def hotpatch_bundle_import(archive, expect, job, should_stop=None, alt=None):
    """Unpack archive into a staging folder under HOTPATCH_BUNDLE_DIR and move into the mirror ONLY
    the files whose md5 + size match `expect` ({cdn_rel: {"md5", "size"}}); `alt` maps a trailing
    "<Lang>/<file>.pck" onto its cdn_rel so a voice bundle laid out flat imports as well as one in
    the CDN layout. Everything else is discarded and counted; a staged file that is a symlink or
    resolves outside the staging folder is never looked at. The staging folder always goes, the
    archive only on success (a failed extraction keeps it for the retry). Returns the number of
    files imported. ExtractStopped -> 409, an extractor failure -> BundleUnavailable."""
    name = os.path.basename(archive)
    stage = os.path.join(HOTPATCH_BUNDLE_DIR, "stage-%s-%d" % (name, os.getpid()))
    try:
        _rmtree_retry(stage)
    except OSError:
        pass
    imported = unlisted = mismatched = busy = 0
    try:
        try:
            extract_archive(archive, stage, job, should_stop=should_stop)
        except ExtractStopped:
            raise AgentError(409, "%s: stopped on request while unpacking" % name)
        except ExtractError as e:
            raise BundleUnavailable("%s: %s" % (name, e))
        stage_real = os.path.realpath(stage)
        for dp, _dn, fns in os.walk(stage):
            for fn in sorted(fns):
                if should_stop is not None and should_stop():
                    raise AgentError(409, "%s: stopped on request while importing" % name)
                p = os.path.join(dp, fn)
                if os.path.islink(p) or not os.path.realpath(p).startswith(stage_real + os.sep):
                    unlisted += 1
                    continue
                rel = os.path.relpath(p, stage).replace(os.sep, "/")
                key = rel if rel in expect else None
                if key is None and alt:
                    key = alt.get("/".join(rel.split("/")[-2:]))
                    if key is not None and key not in expect:
                        key = None
                if key is None:
                    unlisted += 1
                    continue
                e = expect[key]
                st = _stat_or_none(p)
                if st is None or st.st_size != int(e["size"]) or _file_md5(p) != str(e["md5"]).lower():
                    mismatched += 1
                    continue
                dst = hotpatch_local(key)
                prev = _stat_or_none(dst)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _move_into_mirror(p, dst)
                except PermissionError:
                    busy += 1  # the per-file path reports "the file is in use" for it, as today
                    continue
                except MirrorWriteError as err:
                    raise AgentError(507, "%s: writing to the mirror folder failed: %s" % (name, _short_err(err)))
                _restore_owner_mode(dst, prev)
                imported += 1
    finally:
        try:
            _rmtree_retry(stage)
        except OSError as err:
            job.log("WARNING: could not remove the staging folder %s (%s) -- delete it by hand." % (stage, err))
    _remove_quiet(archive)
    job.log("Unpacked %s: %d files imported and verified, %d ignored (not in the manifest), %d mismatched "
            "(re-fetched from the CDN)%s." % (name, imported, unlisted, mismatched,
                                              (", %d in use (left to the per-file path)" % busy) if busy else ""))
    return imported


def hotpatch_fill_word(source, nothing_needed):
    """The state's one-word record of where a run actually got its files (not the configured
    preference -- that is hotpatch_source())."""
    if nothing_needed:
        return "cached"
    b, c = int(source.get("bundle") or 0), int(source.get("cdn") or 0)
    if b and c:
        return "bundle+cdn"
    return "bundle" if b else "cdn"


def hotpatch_miss_policy(rel):
    """What the token-less route may fetch on demand for a path that is not cached:
    (manifest, manifest entry or None, per-file cap) -- or None (a plain 404). A file the manifest
    LISTS is fetched with its md5/size from any known manifest; an UNLISTED file only when it lies
    under one of the three ADVERTISED outputs (a client asking for another tier/alias of the same
    output), with the small per-file cap -- never the rest of the branch, and of the BASE output
    only the voice packs the verified index lists for a language the admin selected (md5/size
    known, hotpatch_voice_expect): the official index files enumerate the whole game (28 GB for
    1.6) there and the route is anonymous."""
    for v in VERSIONS:
        man = read_hotpatch_manifest(v)
        if man is None:
            continue
        expect = next((f for f in man["files"] if f["path"] == rel), None)
        if expect is not None:
            return man, expect, None
        roots = ("client_game_res/%s/%s/" % (man["branch"], man["res"]["output"]),
                 "client_design_data/%s/%s/" % (man["branch"], man["data"]["output"]),
                 "client_design_data/%s/%s/" % (man["branch"], man["silence"]["output"]))
        if rel.startswith(roots):
            return man, None, HOTPATCH_ONDEMAND_FILE_CAP
    voice = hotpatch_voice_expect(rel)
    if voice is not None:
        return voice[0], voice[1], None
    return None


def _manifest_paths():
    out = set()
    for v in VERSIONS:
        man = read_hotpatch_manifest(v)
        if man is not None:
            out.update(f["path"] for f in man["files"])
    return out


def ondemand_bytes(add=0, reset=False):
    """Running total of the UNLISTED files under HOTPATCH_DIR (what anonymous misses have made the
    box cache): walked once, then kept per fetch; reset=True forgets it (a purge). This is what
    HOTPATCH_ONDEMAND_MAX bounds -- the manifest's own files never count, and neither do the
    voice packs under a base output's AudioAssets (the admin chose those languages; one of them
    alone is bigger than the whole budget)."""
    global _ondemand_bytes
    with _ondemand_lock:
        if reset:
            _ondemand_bytes = None
            return 0
        if _ondemand_bytes is None:
            listed = _manifest_paths()
            voice_roots = tuple(r for r in (hotpatch_base_audio_root(m) for m in
                                            (read_hotpatch_manifest(v) for v in VERSIONS) if m) if r)
            total = 0
            root = HOTPATCH_DIR
            if os.path.isdir(root):
                for dp, _dn, fns in os.walk(root):
                    for fn in fns:
                        if fn.endswith(".part"):
                            continue
                        p = os.path.join(dp, fn)
                        rel = os.path.relpath(p, root).replace(os.sep, "/")
                        if rel in listed or (voice_roots and rel.startswith(voice_roots)):
                            continue
                        st = _stat_or_none(p)
                        if st is not None:
                            total += st.st_size
            _ondemand_bytes = total
        _ondemand_bytes += add
        return _ondemand_bytes


def _short_path(rel):
    """Log form of a CDN path: drop the constant "client_game_res/<branch>/" prefix."""
    return rel.split("/", 2)[-1]


def _hotpatch_unverified(files):
    """The manifest files that are missing or wrong on disk (size, then md5)."""
    need = []
    for f in files:
        dst = hotpatch_local(f["path"])
        st = _stat_or_none(dst)
        if st is None or st.st_size != int(f["size"]) or _file_md5(dst) != f["md5"].lower():
            need.append(f)
    return need


def hotpatch_download(man, job):
    """Mirror every manifest file that is missing or wrong (size, then md5): the archive.org bundle
    first when hotpatch_bundle_plan says so, then the official CDN file by file for whatever is
    still missing (or everything, when the bundle cannot be used). Returns (fetched, failed,
    source) with source = {"bundle": n, "cdn": m} -- the good files stay whatever happens to the
    rest, so a retry only fetches the rest."""
    upstreams = man.get("upstreams") or []
    if not upstreams:
        raise AgentError(500, "The hotpatch manifest names no upstream.")
    files = man["files"]
    need = _hotpatch_unverified(files)
    total_b = sum(int(f["size"]) for f in files)
    done_b = total_b - sum(int(f["size"]) for f in need)
    job.log("Mirror %s: %d of %d files already cached and verified (%.1f of %.1f MB)."
            % (HOTPATCH_DIR, len(files) - len(need), len(files), done_b / 1e6, total_b / 1e6))
    source = {"bundle": 0, "cdn": 0}
    if need:
        spec = hotpatch_bundle_spec(man)
        usable, why = hotpatch_bundle_plan(spec, total_b - done_b, total_b, _mirror_free_bytes())
        if usable:
            try:
                archive = hotpatch_bundle_fetch(spec, job)
                source["bundle"] = hotpatch_bundle_import(archive, {f["path"]: f for f in need}, job)
            except BundleUnavailable as e:
                job.log("The bundle could not be used (%s) -- fetching from the official CDN file by file." % e)
            except AgentError as e:
                if e.status == 507:  # the local disk: every file would fail the same way
                    raise AgentError(507, "%s -- stopped; the files fetched so far stay." % e.message)
                raise
            # only what the bundle did not deliver is re-checked (cheap: the imported files are verified)
            need = _hotpatch_unverified(need)
            done_b = total_b - sum(int(f["size"]) for f in need)
        elif spec is not None or not HOTPATCH_BUNDLES:
            job.log("Bundle skipped: %s -- fetching from the official CDN file by file." % why)
    if need:
        job.log("Fetching %d files from %s ..." % (len(need), " then ".join(upstreams)))
    fetched, failed = 0, []
    for i, f in enumerate(need, 1):
        try:
            n = hotpatch_fetch(upstreams, f["path"], expect=f, force=True)
            fetched += 1
            done_b += n
            job.log("  [%d/%d] %s  %.1f MB  -- %.1f / %.1f MB"
                    % (i, len(need), _short_path(f["path"]), n / 1e6, done_b / 1e6, total_b / 1e6))
        except AgentError as e:
            if e.status == 507:  # the local disk, not the CDN: every other file would fail the same way
                raise AgentError(507, "%s -- stopped; the files fetched so far stay." % e.message)
            failed.append(f["path"])
            job.log("  [%d/%d] FAILED %s" % (i, len(need), e.message))
    source["cdn"] = fetched
    return source["bundle"] + fetched, failed, source


def _server_data_revision(version):
    """DataVersionConfig.server: the server data revision the stack ships. The flat layout keeps it
    at server/data/server_data_version.txt; the 2.8 archive nests it one level deeper
    (server/data/<branch>-output_<n>-server-data/server_data_version.txt). 0 is only a dispatch
    WARNING ("server version is 0"), never a failure."""
    d = stack_dir(version)
    candidates = [os.path.join(d, SERVER_DATA_VERSION_TXT)]
    nested = os.path.join(d, "server", "data")
    try:
        for name in sorted(os.listdir(nested)):
            sub = os.path.join(nested, name, "server_data_version.txt")
            if os.path.isdir(os.path.join(nested, name)) and os.path.isfile(sub):
                candidates.append(sub)
    except OSError:
        pass
    for path in candidates:
        try:
            value = int(read_text(path).strip() or 0)
        except (OSError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _write_stack_text(path, text):
    """Atomic write; a NEW file gets 0644 + its directory's owner (same contract as payload installs
    -- the container reads it, and write_text_atomic can only preserve what already exists)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    was_new = not os.path.isfile(path)
    write_text_atomic(path, text)
    if was_new:
        _restore_owner_mode(path, None)


def write_hotpatch_configs(version, job, man):
    """Write the two dispatch/gameserver config files. Returns how many changed (0 = the stack
    already holds exactly these). version.txt gets a one-time .relic-orig backup for disable."""
    d = stack_dir(version)
    changed = 0
    pc = os.path.join(d, PC_VERSION_TXT)
    text = render_pc_version(man)
    if not os.path.isfile(pc) or read_text(pc) != text:
        _write_stack_text(pc, text)
        job.log("  wrote %s (res %s)" % (PC_VERSION_TXT, man["res"]["output"]))
        changed += 1
    vt = os.path.join(d, DATA_VERSION_TXT)
    text = render_version_txt(man, _server_data_revision(version))
    cur = read_text(vt) if os.path.isfile(vt) else None
    if cur != text:
        backup = vt + ".relic-orig"
        if cur is not None and not os.path.isfile(backup):
            shutil.copy2(vt, backup)  # the vendor "{}" (or whatever the admin had) comes back on disable
        _write_stack_text(vt, text)
        job.log("  wrote %s (data %s, silence %s)" % (DATA_VERSION_TXT, man["data"]["output"],
                                                     man["silence"]["output"]))
        changed += 1
    return changed


def remove_hotpatch_configs(version, job):
    """Undo write_hotpatch_configs: drop PC_version.txt, put the backed-up version.txt back (the
    vendor "{}" when there is no backup). A version.txt the agent never touched is left alone."""
    d = stack_dir(version)
    changed = 0
    pc = os.path.join(d, PC_VERSION_TXT)
    had_pc = os.path.isfile(pc)
    if had_pc:
        os.remove(pc)
        job.log("  removed " + PC_VERSION_TXT)
        changed += 1
    vt = os.path.join(d, DATA_VERSION_TXT)
    backup = vt + ".relic-orig"
    has_backup = os.path.isfile(backup)
    want = read_text(backup) if has_backup else "{}"
    cur = read_text(vt) if os.path.isfile(vt) else None
    if cur != want:
        if not has_backup and not had_pc and cur is not None and '"client_silence_md5"' not in cur:
            job.log("  %s is not the agent's -- leaving it alone." % DATA_VERSION_TXT)
        else:
            _write_stack_text(vt, want)
            job.log("  restored %s%s" % (DATA_VERSION_TXT, " from the backup" if has_backup
                                         else " (vendor default '{}')"))
            changed += 1
    return changed


def _region_urls(version):
    """The four URL columns as the DB holds them, or None when MySQL does not answer / no row."""
    r = mysql_exec(version, "SELECT resource_url, resource_url_bak, data_url, data_url_bak FROM "
                            "t_region_config WHERE name='%s'" % _sql_q(REGION),
                   db="hk4e_db_deploy_config", timeout=30)
    if r.returncode != 0:
        return None
    # Keep whitespace-only lines: the vendor/disabled row is "\t\t\t" (four empty columns) and
    # dropping it would make every SELECT-then-UPDATE report a change (and restart the services).
    lines = [l for l in (r.stdout or "").splitlines() if l != ""]
    if len(lines) < 2:
        return None
    cols = lines[-1].split("\t")
    return (cols + [""] * 4)[:4]


def hotpatch_sql(version, job, url, man):
    """Point the four t_region_config URL columns at the mirror (url=None -> back to ''). The
    dispatch appends "/<branch>" to resource_url itself; data_url is used verbatim. Returns True
    when the columns changed. Raises (run_sql) when the UPDATE fails."""
    if url:
        res, data = url + "/client_game_res", url + "/client_design_data/" + man["branch"]
    else:
        res = data = ""
    if _region_urls(version) == [res, res, data, data]:
        job.log("  t_region_config already advertises %s." % (res or "no hotpatch URL"))
        return False
    q = _sql_q
    run_sql(version, job, [
        "UPDATE hk4e_db_deploy_config.t_region_config SET resource_url='%s', resource_url_bak='%s', "
        "data_url='%s', data_url_bak='%s' WHERE name='%s'" % (q(res), q(res), q(data), q(data), q(REGION)),
    ])
    return True


def stack_up(version):
    """(up, err): whether this version's compose project is listed; err = docker is mute."""
    projects, err = running_projects()
    return (err is None and VERSIONS[version]["project"] in (projects or [])), err


def _restart_config_services(version, job):
    job.log("Restarting the services that read the config at boot (connected players are kicked)...")
    rc = _compose_stream(stack_dir(version), job, "restart", *HOTPATCH_SERVICES, prefix="  ")
    if rc != 0:
        raise AgentError(500, "The hotpatch configs were written, but restarting the services failed "
                              "(code %d) -- stop and start the version to apply them." % rc)
    verify_stack(version, job)


def hotpatch_probe(version, job, man, url):
    """Non-fatal: ask the running dispatch what it now tells clients (the same query_cur_region
    the client sends, through the sdk's /query_region/<name> route) and WARN when the mirror URL
    or the release index line is missing from the reply. 1.6 answers unencrypted protobuf in
    base64; a reply that does not decode is only reported."""
    try:
        host = read_env(version).get("OUTER_IP", "").strip() or "127.0.0.1"
        path = "/query_region/" + REGION
        r = mysql_exec(version, "SELECT dispatch_url FROM t_region_config WHERE name='%s'"
                       % _sql_q(REGION), db="hk4e_db_deploy_config", timeout=30)
        m = re.match(r"https?://[^/]+(/\S*)", _scalar(r)) if r.returncode == 0 else None
        if m:
            path = m.group(1)
        probe = "http://%s:21000%s?version=OSRELWin%s.0&lang=1&platform=3&binary=1&time=%d" \
                "&channel_id=1&sub_channel_id=0&account_type=1" % (host, path, version, int(time.time()))
        with urllib.request.urlopen(probe, timeout=15) as resp:
            body = resp.read()
        try:
            raw = base64.b64decode(body, validate=True)
        except Exception:  # noqa: BLE001
            job.log("WARNING: dispatch check: %s did not answer base64 (%d bytes: %s) -- cannot "
                    "verify what clients are told." % (probe.split("?")[0], len(body),
                                                       body[:80].decode("utf-8", "replace")))
            return
        missing = [w for w in ((url + "/client_game_res/" + man["branch"]).encode(),
                               b"release_res_versions_external") if w not in raw]
        if missing:
            job.log("WARNING: dispatch check: the query_cur_region reply (%d bytes) does not contain %s "
                    "-- clients may not be told about the hotpatch. Check server/res/PC_version.txt, "
                    "server/data/version.txt and the t_region_config URL columns, then restart dispatch."
                    % (len(raw), ", ".join(repr(w.decode()) for w in missing)))
        else:
            job.log("Dispatch check OK: query_cur_region advertises %s/%s and the release index."
                    % (url + "/client_game_res", man["branch"]))
    except Exception as e:  # noqa: BLE001 -- a failed check must not fail an applied hotpatch
        job.log("WARNING: dispatch check skipped (%s)." % e)


def do_hotpatch(version, job, enabled, purge=False):
    """POST /server/hotpatch. ENABLE: mirror + verify every manifest file, write the two config
    files, and -- when the stack is up -- the SQL + restart; otherwise mark it pending for the next
    start. DISABLE: the reverse; purge=True also deletes the mirrored branch."""
    man = read_hotpatch_manifest(version)
    if man is None:
        raise AgentError(404, "No hotpatch manifest for version %s (payloads/%s/hotpatch.json)."
                         % (version, version))
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                         % (version, stack_dir(version)))
    # The URL check first: it is the cheap refusal (409), before any docker/MySQL probe.
    url = hotpatch_public_url(version, man, strict=True)[0] if enabled else None
    up, ls_err = stack_up(version)
    if ls_err is not None:
        job.log("Cannot read the docker state (%s) -- treating the stack as not running; the "
                "database half is applied at the next start." % ls_err)
    can_sql = up and is_bootstrapped(version) and mysql_reachable(version)
    st = dict(version_state(version).get("hotpatch") or {})
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if enabled:
        job.log("Enabling the official %s hotpatch for %s: res %s, data %s, silence %s."
                % (man["branch"], version, man["res"]["output"], man["data"]["output"],
                   man["silence"]["output"]))
        job.log("Mirror URL players will use: %s" % url)
        fetched, failed, source = hotpatch_download(man, job)
        if failed:
            raise AgentError(502, "%d of %d files could not be fetched (neither the archive.org bundle nor the "
                                  "official CDN delivered %s%s). The %d good files stay cached -- retry later."
                             % (len(failed), len(man["files"]),
                                ", ".join(_short_path(p) for p in failed[:4]),
                                " ..." if len(failed) > 4 else "", len(man["files"]) - len(failed)))
        job.log("Every file is cached and verified. Writing the dispatch config files...")
        changed = write_hotpatch_configs(version, job, man)
        # The decision is persisted BEFORE the database/restart half, as pending: whatever fails
        # from here on (MySQL, a service that does not survive the restart), the stack is never
        # left half-applied with a state that says "disabled" -- the next start re-asserts it
        # (apply_hotpatch) or the admin's Disable reverts it.
        owed = bool(st.get("pending"))  # a previous attempt left the restart owed
        st.update(enabled=True, pending=True, appliedAt=now, url=url,
                  res=man["res"]["revision"], data=man["data"]["revision"],
                  silence=man["silence"]["revision"],
                  source=hotpatch_fill_word(source, fetched == 0))
        version_state_set(version, hotpatch=st)
        pending = True
        if can_sql:
            job.log("Pointing t_region_config at the mirror...")
            if hotpatch_sql(version, job, url, man):
                changed += 1
            if changed or owed:
                _restart_config_services(version, job)
            else:
                job.log("Nothing changed (the files and the URL columns were already in place) -- "
                        "no restart, nobody is kicked.")
            pending = False
            st.update(pending=False)
            version_state_set(version, hotpatch=st)
        else:
            job.log("The stack is not running -- the database update and the service restart are "
                    "deferred to the next start.")
        if not pending:
            hotpatch_probe(version, job, man, url)
        job.log("Clients that log in from now on download the fixes once (up to about %.0f MB each; "
                "a changed voice pack only by clients using that voice language)."
                % (sum(int(f["size"]) for f in man["files"]) / 1e6))
        _log_voice_coverage(version, job, man)
        return {"enabled": True, "pending": pending, "fetched": fetched, "url": url, "source": source}

    job.log("Disabling the hotpatch for %s..." % version)
    # Only something the agent applied (or still owes) can sit in the database: a version that
    # was never enabled, or already fully reverted, owes nothing.
    owed = bool(st.get("enabled") or st.get("pending"))
    st.update(enabled=False, pending=owed, disabledAt=now)
    version_state_set(version, hotpatch=st)  # persisted first: a failed revert stays owed
    changed = remove_hotpatch_configs(version, job)
    pending = owed
    if can_sql:
        if hotpatch_sql(version, job, None, man):
            changed += 1
        if changed or owed:
            _restart_config_services(version, job)
        else:
            job.log("Nothing changed (nothing was advertised) -- no restart, nobody is kicked.")
        pending = False
        st.update(pending=False)
        version_state_set(version, hotpatch=st)
    elif owed:
        job.log("The stack is not running -- the database columns are cleared at the next start.")
    purge_failed = False
    if purge:
        for sub in ("client_game_res", "client_design_data"):
            p = os.path.join(HOTPATCH_DIR, sub, man["branch"])
            if os.path.isdir(p):
                try:
                    _rmtree_retry(p)
                    job.log("  purged %s" % p)
                except OSError as err:  # Windows: a client is being served one of the files right now
                    purge_failed = True
                    job.log("  %s: some files are in use (a client is downloading one) -- not everything "
                            "was deleted; purge again later (%s)." % (p, _short_err(err)))
        ondemand_bytes(reset=True)
        # The bundle leftovers of this branch too: a kept archive / .part (up to 9 GB) and any
        # staging folder a killed agent left behind. Nothing here is served, so never fatal.
        try:
            for n in sorted(os.listdir(HOTPATCH_BUNDLE_DIR) if os.path.isdir(HOTPATCH_BUNDLE_DIR) else []):
                p = os.path.join(HOTPATCH_BUNDLE_DIR, n)
                if n.startswith(man["branch"] + "_") and ".7z" in n and os.path.isfile(p):
                    _remove_quiet(p)
                    job.log("  purged the bundle file %s" % p)
                elif n.startswith("stage-" + man["branch"] + "_") and os.path.isdir(p):
                    _rmtree_retry(p)
                    job.log("  purged the stale staging folder %s" % p)
        except OSError as err:
            job.log("  (bundle leftovers under %s not fully removed: %s)" % (HOTPATCH_BUNDLE_DIR, _short_err(err)))
        if st.get("voice"):
            job.log("  (the mirrored voice packs went with it; the selection %s is kept -- mirror them "
                    "again before the next Enable.)" % ", ".join(st["voice"]))
    job.log("Clients keep what they already downloaded into Persistent; nothing new is fetched.")
    return {"enabled": False, "pending": pending, "purged": bool(purge) and not purge_failed}


def _log_voice_coverage(version, job, man):
    """The closing lines of an Enable: which voice languages the mirror serves, and what happens
    to a player of any other one. Never fails the job."""
    try:
        voice = hotpatch_voice_summary(version, man)
        if voice["base"] is None or not voice["languages"]:
            return
        rows = [r for r in voice["languages"] if r["selected"]]
        for r in rows:
            if r["cached"] < r["files"]:
                job.log("WARNING: voice packs: %s is selected but %d of %d files are not mirrored yet. Each "
                        "is fetched when a player first asks for it, but the game only waits briefly (2.8: "
                        "30 s per try, 6 tries) and a big pack does not arrive in that time -- run 'Mirror "
                        "voice packs' to fetch them now." % (r["name"], r["files"] - r["cached"], r["files"]))
            else:
                job.log("Voice packs: %s is mirrored (%d files, %.1f GB)." % (r["name"], r["files"], r["bytes"] / 1e9))
        kept = [r["name"] for r in voice["languages"] if not r["selected"] and r["files"] and r["cached"] >= r["files"]]
        if kept:
            job.log("Voice packs served from the cache although not selected: %s (complete on disk; a pack "
                    "that goes missing would not be fetched again)." % ", ".join(kept))
        others = [r["name"] for r in voice["languages"] if not r["selected"] and r["name"] not in kept]
        if others:
            job.log("Voice packs NOT served: %s. With the hotpatch advertised, the game checks the voice "
                    "language it uses on disk -- on a fresh profile that is the WINDOWS display language "
                    "(Japanese / Korean / Chinese pick that voice, everything else English) -- and asks "
                    "this mirror for every missing pack. Relic installs the English voices only, so such "
                    "a player gets a download error at login until the language is mirrored here "
                    "(hotpatch panel > Voice packs) or installed in the game folder." % ", ".join(others))
    except Exception as e:  # noqa: BLE001 -- a report, never a reason to fail an applied hotpatch
        job.log("(voice pack report skipped: %s)" % e)


def running_job_of_kind(kind):
    with _jobs_lock:
        for jid in reversed(JOBS_ORDER):
            if JOBS[jid].state == "running" and JOBS[jid].kind == kind:
                return JOBS[jid]
    return None


def do_hotpatch_voice(version, job, languages, purge=False):
    """POST /server/hotpatch/voice: select the voice languages whose BASE-build packs this mirror
    serves and fetch what is missing (md5 + size from the verified release index, resumed by
    Range). Touches only the mirror folder and the state -- never the stack, no restart.
    Two phases. PREPARE, under _op_lock like every job: index, 400 unknown language, 409 disk
    check, the selection saved, purge=True deletes the cached packs of every language NOT selected.
    DOWNLOAD, with the lock released and job.yielding set: it can run for an hour, so the
    watchdog's jobs and player signups start beside it (start_job / start_public_job); it ends
    early on POST {"cancel": true}, a full disk, or five failed files in a row. The selection is
    saved before the download, so a pack a failed or stopped run left out can still be fetched
    when a player asks for it (a fallback: enabled hotpatch + on-demand fetching only, and the 2.8
    client rarely waits long enough) -- running it again continues where it stopped (.part + Range)."""
    _voice_cancel.clear()
    with _op_lock:
        man = read_hotpatch_manifest(version)
        if man is None:
            raise AgentError(404, "No hotpatch manifest for version %s (payloads/%s/hotpatch.json)."
                             % (version, version))
        if not is_present(version):
            raise AgentError(404, "Version %s does not exist on this server (%s is missing)."
                             % (version, stack_dir(version)))
        base_root = hotpatch_base_audio_root(man)
        if base_root is None:
            raise AgentError(409, "The hotpatch manifest of %s names no base output (res.base) -- its voice "
                                  "packs cannot be located." % version)
        st = dict(version_state(version).get("hotpatch") or {})  # 503 on an unreadable state: before any download
        upstreams = man.get("upstreams") or []
        index = hotpatch_voice_index(man)
        if not index:
            job.log("Fetching the release index that lists (and authenticates) the voice packs...")
            root, errors = hotpatch_res_root(man), []
            for name in VOICE_INDEX_FILES:
                entry = next((f for f in man["files"] if f["path"] == root + name), None)
                if entry is None:
                    continue
                try:
                    hotpatch_fetch(upstreams, entry["path"], expect=entry, force=True)
                except AgentError as e:
                    if e.status == 507:
                        raise
                    errors.append(e.message)
                    continue
                index = hotpatch_voice_index(man)
                if index:
                    break
            if not index:
                raise AgentError(502, "The release index of %s could not be mirrored, so its voice packs cannot "
                                      "be verified (%s). Try again later."
                                 % (version, "; ".join(errors) or "no index file in the manifest"))
        unknown = [x for x in languages if x not in index]
        if unknown:
            raise AgentError(400, "Unknown voice language(s) %s -- this version has: %s."
                             % (", ".join(repr(x) for x in unknown), ", ".join(index)))
        selected = [lang for lang in index if lang in languages]  # the index's own order, no duplicates
        need = []
        for lang in selected:
            for e in index[lang]:
                dst = hotpatch_local(base_root + e["name"])
                fst = _stat_or_none(dst)
                if fst is None or fst.st_size != e["size"]:
                    part = _stat_or_none(dst + ".part")
                    need.append((lang, e, part.st_size if part is not None and part.st_size < e["size"] else 0))
        need_b = sum(e["size"] - have for _l, e, have in need)
        total_b = sum(e["size"] for lang in selected for e in index[lang])
        doomed = [(lang, hotpatch_local(base_root + lang)) for lang in index
                  if lang not in selected and os.path.isdir(hotpatch_local(base_root + lang))]
        doomed = [(lang, p, _dir_bytes(p)) for lang, p in doomed]
        loose_b = sum(size for _l, _p, size in doomed)
        free = _mirror_free_bytes()
        # Per language: the archive.org bundle when at least half of its packs are missing (and a
        # tool + disk exist); its remaining archive bytes join the disk check below, because the
        # archive and the unpacked packs coexist until the import is done.
        plans, bundle_b = {}, 0
        for lang in selected:
            lang_need_b = sum(e["size"] - have for l_, e, have in need if l_ == lang)
            if not lang_need_b:
                continue
            spec = hotpatch_bundle_spec(man, lang)
            usable, why = hotpatch_bundle_plan(spec, lang_need_b, sum(e["size"] for e in index[lang]), free)
            plans[lang] = (spec, usable, why)
            if usable:
                bundle_b += hotpatch_bundle_remaining(spec)
        if need_b and free is not None and free + (loose_b if purge else 0) < need_b + bundle_b + HOTPATCH_DISK_RESERVE:
            hint = ""
            if not purge and free + loose_b >= need_b + bundle_b + HOTPATCH_DISK_RESERVE:
                hint = (" Deleting the cached packs of the languages that are not selected (%.1f GB, the "
                        "purge option) would make it fit." % (loose_b / 1e9))
            raise AgentError(409, "Not enough free disk space for the voice packs: %.1f GB to download, %.1f GB "
                                  "free on the mirror's volume, and %d MiB must stay free "
                                  "(GIO_HOTPATCH_DISK_RESERVE). Select fewer languages or free some space.%s"
                             % ((need_b + bundle_b) / 1e9, free / 1e9, HOTPATCH_DISK_RESERVE >> 20, hint))
        st["voice"] = selected
        saved = version_state_set(version, hotpatch=st)
        job.log("Voice languages selected for the %s mirror: %s." % (version, ", ".join(selected) or "none"))
        purged, purge_failed = [], []
        for lang, p, size in (doomed if purge else []):
            try:
                _rmtree_retry(p)
                purged.append(lang)
                job.log("  deleted the cached %s packs (%.1f GB)." % (lang, size / 1e9))
            except OSError as err:  # Windows: a client is being served one of them right now
                purge_failed.append(lang)
                job.log("  the cached %s packs are in use (a client is downloading one) -- not all of them "
                        "were deleted; purge again later (%s)." % (lang, _short_err(err)))
        if not saved:
            # Deleting re-fetchable packs unrecorded is harmless (and may be what frees the disk);
            # downloading GBs -- or reporting success -- with the selection unrecorded is not.
            version_state_set(version, strict=True, hotpatch=st)
    result = {"voice": selected, "fetched": 0, "bytes": 0, "purged": purged, "purgeFailed": purge_failed,
              "source": {"bundle": 0, "cdn": 0}, "bundles": []}
    def complete(lang):
        sizes = ((_stat_or_none(hotpatch_local(base_root + e["name"])), e["size"]) for e in index[lang])
        return all(fst is not None and fst.st_size == size for fst, size in sizes)

    kept = [lang for lang in index if lang not in selected and complete(lang)]
    if kept:
        job.log("Still served from the cache although not selected: %s (complete on disk; the purge "
                "option deletes them)." % ", ".join(kept))
    if not selected:
        job.log("Nothing to mirror. A request for a voice pack that is not cached gets a plain 404.")
        return result
    n_files = sum(len(index[lang]) for lang in selected)
    job.log("%d of %d files already mirrored (%.1f of %.1f GB)."
            % (n_files - len(need), n_files, (total_b - need_b) / 1e9, total_b / 1e9))
    if need:
        job.log("Fetching %d files (%.1f GB) from %s -- the game server keeps running; admin operations on "
                "this server wait until this finishes or is stopped (it continues where it stopped)."
                % (len(need), need_b / 1e9, " then ".join(upstreams)))
    job.yielding = True
    done_b = total_b - need_b
    n_need = len(need)
    # The bundles first, one per language whose plan said so: download (resumable), unpack in the
    # staging folder, import only the packs whose md5 + size the verified index lists. What a
    # bundle delivered leaves `need`; the per-pack CDN loop below takes the rest. A bundle that
    # cannot be used is one line and never counts towards the five-failures streak.
    per_lang = {lang: {"bundle": 0, "cdn": 0} for lang in selected}
    for lang in selected:
        spec, usable, why = plans.get(lang, (None, False, None))
        if not any(l_ == lang for l_, _e, _h in need):
            continue
        if not usable:
            if spec is not None or not HOTPATCH_BUNDLES:
                job.log("%s: bundle skipped: %s -- fetching from the official CDN file by file." % (lang, why))
            continue
        stopped = "Stopped on request: %d of %d files fetched in this run -- run it again to continue where " \
                  "it stopped." % (result["fetched"], n_need)
        if _voice_cancel.is_set():
            raise AgentError(409, stopped)
        try:
            archive = hotpatch_bundle_fetch(spec, job, should_stop=_voice_cancel.is_set)
            imported = hotpatch_bundle_import(archive, {base_root + e["name"]: e for e in index[lang]}, job,
                                              should_stop=_voice_cancel.is_set,
                                              alt={e["name"]: base_root + e["name"] for e in index[lang]})
        except BundleUnavailable as err:
            job.log("The %s bundle could not be used (%s) -- fetching the %s packs from the official CDN file "
                    "by file." % (lang, err, lang))
            continue
        except AgentError as err:
            if err.status == 409:
                raise AgentError(409, stopped)
            if err.status == 507:
                raise AgentError(507, "%s -- stopped; what was fetched stays." % err.message)
            raise
        if imported:
            result["bundles"].append(lang)
            per_lang[lang]["bundle"] += imported
            result["source"]["bundle"] += imported
            result["fetched"] += imported
        # drop what the bundle delivered (re-stat: only complete packs count)
        left = []
        for l_, e, have in need:
            fst = _stat_or_none(hotpatch_local(base_root + e["name"]))
            if l_ == lang and fst is not None and fst.st_size == e["size"]:
                result["bytes"] += e["size"] - have
                done_b += e["size"] - have
                continue
            left.append((l_, e, have))
        need = left
    if need and result["source"]["bundle"]:
        job.log("%d files left for the official CDN (%.2f GB)." % (len(need), sum(e["size"] - h for _l, e, h in need) / 1e9))
    failed, streak = [], 0
    for i, (lang, e, have) in enumerate(need, 1):
        rel = base_root + e["name"]
        stopped = "Stopped on request: %d of %d files fetched in this run -- run it again to continue where " \
                  "it stopped." % (result["fetched"], n_need)
        if _voice_cancel.is_set():
            raise AgentError(409, stopped)
        free = _mirror_free_bytes()
        if free is not None and free < (e["size"] - have) + HOTPATCH_DISK_RESERVE:
            raise AgentError(409, "Stopped before %s: the mirror's volume is down to %.1f GB free and %d MiB "
                                  "must stay free (GIO_HOTPATCH_DISK_RESERVE). What was fetched stays."
                             % (e["name"], free / 1e9, HOTPATCH_DISK_RESERVE >> 20))
        try:
            hotpatch_fetch(upstreams, rel, expect={"path": rel, "md5": e["md5"], "size": e["size"]},
                           resume=True, should_stop=_voice_cancel.is_set)
        except AgentError as err:
            if err.status == 409:
                raise AgentError(409, stopped)
            if err.status == 507:  # the local disk: no point in going on
                raise AgentError(507, "%s -- stopped; what was fetched stays." % err.message)
            failed.append(e["name"])
            streak += 1
            job.log("  [%d/%d] FAILED %s" % (i, len(need), err.message))
            if streak >= 5:
                raise AgentError(502, "Five files in a row could not be fetched (%s ...) -- the CDN is not "
                                      "delivering right now; stopped. The %d fetched files stay and the selection "
                                      "is saved; run it again later." % (", ".join(failed[-5:][:3]), result["fetched"]))
            continue
        streak = 0
        result["fetched"] += 1
        result["source"]["cdn"] += 1
        per_lang[lang]["cdn"] += 1
        result["bytes"] += e["size"] - have
        done_b += e["size"] - have
        job.log("  [%d/%d] %s  %.1f MB  -- %.2f / %.2f GB"
                % (i, len(need), e["name"], e["size"] / 1e6, done_b / 1e9, total_b / 1e9))
    # Where each language's packs came from this run (the panel's per-row "source"): merged into
    # the record, a language that needed nothing keeps its previous word. Non-strict: a lost note
    # costs nothing (the packs are on disk and verified either way).
    words = {lang: hotpatch_fill_word(c, False) for lang, c in per_lang.items() if c["bundle"] or c["cdn"]}
    if words:
        try:
            cur = dict(version_state(version).get("hotpatch") or {})
            merged = dict(cur.get("voiceSource") or {}) if isinstance(cur.get("voiceSource"), dict) else {}
            merged.update(words)
            cur["voiceSource"] = merged
            version_state_set(version, hotpatch=cur)
        except AgentError:
            pass
    if not st.get("enabled"):
        job.log("The hotpatch of %s is not enabled: the packs wait here and are served once it is." % version)
    if failed:
        raise AgentError(502, "%d of %d voice pack files could not be fetched (%s%s). The %d good ones stay "
                              "and the selection is saved: a missing pack is fetched when a player asks "
                              "for it (the game only waits briefly, so run this again rather than rely on it)."
                         % (len(failed), len(need), ", ".join(failed[:4]), " ..." if len(failed) > 4 else "",
                            len(need) - len(failed)))
    job.log("Every selected voice pack is mirrored and verified.")
    return result


def apply_hotpatch(version, job, with_sql=True):
    """Re-assert the persisted hotpatch decision on the stack: enabled => the two config files and
    the four URL columns present; disabled with a revert still pending => revert once. Idempotent,
    never downloads, never restarts -- the caller folds the returned change count into its own
    restart decision. Called before every boot (provision, start) and by netfix."""
    st = version_state(version).get("hotpatch") or {}
    if not st.get("enabled") and not st.get("pending"):
        return 0
    man = read_hotpatch_manifest(version)
    if man is None:
        job.log("WARNING: the hotpatch is enabled in state but payloads/%s/hotpatch.json is gone -- "
                "leaving the stack alone." % version)
        return 0
    touched = 0
    try:
        if st.get("enabled"):
            url, problem = hotpatch_public_url(version, man, strict=False)
            if problem:
                # Advertising revisions with no reachable URL would break every login: keep the
                # client in the vendor state until the admin fixes the address, loudly.
                job.log("WARNING: the hotpatch mirror URL is not usable (%s) -- NOT advertising the "
                        "hotpatch until it is; it stays pending." % problem)
                touched += remove_hotpatch_configs(version, job)
                if with_sql and hotpatch_sql(version, job, None, man):
                    touched += 1
                version_state_set(version, hotpatch=dict(st, pending=True))
                return touched
            job.log("Hotpatch enabled -- checking the dispatch config files%s (%s)..."
                    % (" and the URL columns" if with_sql else "", url))
            touched += write_hotpatch_configs(version, job, man)
            if with_sql:
                if hotpatch_sql(version, job, url, man):
                    touched += 1
                if st.get("pending") or st.get("url") != url:
                    version_state_set(version, hotpatch=dict(st, pending=False, url=url))
        else:
            job.log("Hotpatch disabled with a revert pending -- checking the dispatch config files%s..."
                    % (" and the URL columns" if with_sql else ""))
            touched += remove_hotpatch_configs(version, job)
            if with_sql:
                if hotpatch_sql(version, job, None, man):
                    touched += 1
                version_state_set(version, hotpatch=dict(st, pending=False))
        if touched:
            job.log("  %d hotpatch config change(s) applied." % touched)
        else:
            job.log("  nothing to change.")
    except AgentError as e:
        job.log("WARNING: the hotpatch could not be fully re-applied (%s) -- it stays pending for "
                "the next start." % e.message)
        version_state_set(version, hotpatch=dict(st, pending=True))
    return touched


# -- account copy --
# A player's save (t_player_data_(uid%10).bin_data, proto PlayerDataBin, magic "ZLIB"+zlib) has NO
# uid field, but EVERY persistent guid is (uid<<32)|seq: items (5.1.1.3, fixed64), avatars (2.1.3,
# varint), worn equipment (2.1.101.2, packed list), teams (2.5.2.x) etc. A naive copy onto another
# uid leaves two accounts with hundreds of IDENTICAL guids and co-op goes haywire (the
# entity-per-guid maps collide -- diagnosed and the fix validated live). So the copy decodes the
# generic wire format and rewrites ANY varint/fixed64 value with high32==src. t_block_data /
# t_home_data carry no uid/guid and are copied server-side; redis/t_player_uid/sdk.db are never
# touched. Evidence and history: docs/derisk/ACCOUNT-COPY-HANDOFF.md.

# -- server package download (agent 3.4): the ready-made vendor stack from the Internet Archive --
# A box with no stack yet used to be the admin's problem ("extract the vendor package into the
# folder first"). payloads/<version>/stack.json now names the archive.org copy of the vendor's
# all-in-one .7z (size + md5 from the item's <item>_files.xml) and the `fetch` job pulls it:
# <dir>.7z.part next to the target (Range resume, a NETWORK cut keeps it), verified, extracted into
# a sibling temp folder by the shared extraction layer, and the archive's top folder renamed onto
# GIO_DIR_xx in one step -- `present` flips only there, so status and the watchdogs never see a
# half stack. The job YIELDS (like hotpatch-voice) for the 10-45 min a 2.5 GB archive.org download
# takes: only its short prepare + finish phases hold _op_lock. A verified archive already next to
# the dir (scp by hand) is adopted without a download.

def archive_path(version):
    """<stack dir>.7z -- the archive lives beside its target (same volume: the final rename of the
    extracted tree is atomic), .part appended by _fetch_to_file while it downloads."""
    return stack_dir(version).rstrip("/\\") + ".7z"


def extract_tmp(version):
    """<parent>/.relic-extract-<basename>/ -- the temp folder the archive is unpacked into."""
    d = os.path.abspath(stack_dir(version).rstrip("/\\"))
    return os.path.join(os.path.dirname(d), ".relic-extract-" + os.path.basename(d))


def _dir_absent_or_empty(d):
    if not os.path.lexists(d):
        return True
    try:
        return os.path.isdir(d) and not os.listdir(d)
    except OSError:
        return False


def fetch_preflight(version, force=False):
    """Every refusal a POST /server/fetch gets synchronously (and the job repeats under _op_lock):
    404 unconfigured / no catalogue, 500 invalid catalogue, 409 already present (force skips this
    only over an absent or empty dir -- a present stack is never overwritten), 409 a non-empty dir
    that is not a stack, 409 no extractor, 409 not enough disk. Returns (catalogue, extractor path)."""
    d = stack_dir(version)  # 400 unknown / 404 unconfigured
    cat = read_stack_catalog(version)
    if cat is None:
        raise AgentError(404, "No download is known for version %s (payloads/%s/stack.json)." % (version, version))
    try:
        validate_stack_catalog(cat)
    except ValueError as e:
        raise AgentError(500, "stack.json of %s is invalid: %s" % (version, e))
    if is_present(version) and not (force and _dir_absent_or_empty(d)):
        raise AgentError(409, "Version %s is already on this server (%s) -- nothing to download." % (version, d))
    if os.path.lexists(d) and not _dir_absent_or_empty(d):
        raise AgentError(409, "%s contains files but is not a server stack (no docker-compose.yml.tmpl): move "
                              "them away or point GIO_DIR_%s elsewhere." % (d, version.replace(".", "")))
    found = find_extractor()
    if found is None:
        raise AgentError(409, "No 7z extractor on this box (7zz/7z/7za/7zr/bsdtar -- Linux; 7-Zip or tar.exe -- "
                              "Windows). Install one and run this again.")
    size = int(cat["size"])
    have = 0
    for p in (archive_path(version), archive_path(version) + ".part"):
        st = _stat_or_none(p)
        if st is not None and st.st_size <= size:
            have = max(have, st.st_size)
    extracted = int(cat.get("extractedSize") or 0) or 3 * size
    parent = os.path.dirname(os.path.abspath(d.rstrip("/\\"))) or "."
    free = _free_bytes_at(parent)
    need = (size - have) + extracted + FETCH_DISK_RESERVE
    if free is not None and free < need:
        raise AgentError(409, "Not enough free disk space: %.1f GB needed (%.1f GB archive + %.1f GB extracted + "
                              "%d MiB reserve), %.1f GB free on %s."
                         % (need / 1e9, (size - have) / 1e9, extracted / 1e9, FETCH_DISK_RESERVE >> 20,
                            free / 1e9, parent))
    return cat, found[0]


def _fetch_stack_archive(cat, archive, job):
    """The DOWNLOAD phase: adopt a complete verified archive, else download with Range resume and a
    backoff over network cuts. Returns True when bytes were downloaded, False when adopted."""
    size, md5, url = int(cat["size"]), cat["md5"].lower(), cat["url"]
    st = _stat_or_none(archive)
    if st is not None:
        if st.st_size == size:
            job.log("An archive is already here (%s) -- verifying its md5 instead of downloading ..." % archive)
            try:
                same = _file_md5(archive) == md5
            except OSError:
                same = False
            if same:
                job.log("Archive already here (pre-seeded) and verified -- skipped the download.")
                return False
            job.log("It is not the catalogue's file (md5 differs) -- deleting it and downloading afresh.")
        else:
            job.log("An archive of the wrong size is here (%d bytes) -- deleting it." % st.st_size)
        _remove_quiet(archive)
    part = _stat_or_none(archive + ".part")
    if part is not None and 0 < part.st_size < size:
        job.log("  resuming at %d MB (a previous run left %s; the kept part is verified first)"
                % (part.st_size // 1000000, archive + ".part"))
    last = [0]

    def progress(n):
        if n - last[0] >= 100 * 1000 * 1000:
            last[0] = n
            job.log("  %d / %d MB (%d%%)" % (n // 1000000, size // 1000000, n * 100 // size))

    def kept():
        p = _stat_or_none(archive + ".part")
        return p.st_size if p is not None else 0

    cuts, since_progress = 0, 0
    while True:
        if _fetch_cancel.is_set():
            raise AgentError(409, "Stopped on request: %d of %d MB kept -- run it again to continue where it "
                                  "stopped." % (kept() // 1000000, size // 1000000))
        before = kept()
        try:
            _fetch_to_file(url, archive, timeout=HOTPATCH_TIMEOUT, expect_md5=md5, expect_size=size, resume=True,
                           should_stop=_fetch_cancel.is_set, progress=progress)
            return True
        except FetchStopped:
            raise AgentError(409, "Stopped on request: %d of %d MB kept -- run it again to continue where it "
                                  "stopped." % (kept() // 1000000, size // 1000000))
        except TlsVerifyError as e:
            # Before the ValueError / OSError branches: not a checksum mismatch and not a network cut to
            # wait out -- the certificate will not verify on the 30th try either.
            raise AgentError(502, "The server package could not be downloaded: %s. Turn on GIO_TLS_PINNED_FALLBACK "
                                  "(Agent settings) or add the missing root with GIO_CA_FILE; check the PC clock "
                                  "and any antivirus that inspects HTTPS." % e)
        except MirrorWriteError as e:
            raise AgentError(507, "Writing %s failed: %s -- the download stopped (a full disk or a folder that "
                                  "is not writable)." % (archive, _short_err(e)))
        except FetchRefused as e:
            raise AgentError(502, "The archive on %s changed (%s; the catalogue says %d bytes) -- "
                                  "payloads/%s/stack.json needs updating."
                             % (urllib.parse.urlsplit(url).hostname, e, size, cat.get("version", "?")))
        except MirrorBusyError as e:
            raise AgentError(500, "%s: %s" % (archive, _short_err(e)))
        except ValueError as e:
            # size / md5 mismatch after a complete transfer: _fetch_to_file already discarded it
            raise AgentError(502, "Checksum mismatch (%s) -- the downloaded archive was discarded; run it again "
                                  "to download it from scratch (if it repeats, the file on %s changed and the "
                                  "catalogue needs updating)." % (e, urllib.parse.urlsplit(url).hostname))
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404, 410):
                raise AgentError(502, "The archive is not available on %s (HTTP %d) -- the catalogue may be "
                                      "outdated. Nothing was changed." % (urllib.parse.urlsplit(url).hostname, e.code))
            cuts += 1
            since_progress = 0 if kept() > before else since_progress + 1
            if since_progress >= FETCH_RETRIES:
                raise AgentError(502, "The download keeps failing (%s; %d tries without progress) -- %d of %d MB "
                                      "kept; run it again later to continue where it stopped."
                                 % (_short_err(e), since_progress, kept() // 1000000, size // 1000000))
            wait = min(5 * (2 ** min(since_progress, 6)), 60)
            job.log("  connection lost (%s) -- retrying in %d s (%d of %d MB kept)"
                    % (_short_err(e), wait, kept() // 1000000, size // 1000000))
            for _ in range(wait * 2):
                if _fetch_cancel.is_set():
                    break
                time.sleep(0.5)


def do_fetch(version, job, force=False):
    """POST /server/fetch (job `fetch`, lock=False). PREPARE under _op_lock: the preflight again
    (the busy window closed). DOWNLOAD + EXTRACT with job.yielding (the watchdogs' jobs and player
    signups run beside it; every admin-started job waits): archive to <dir>.7z, verified; unpacked
    into <parent>/.relic-extract-<basename>/. FINISH under _op_lock (milliseconds): the target
    re-checked absent/empty, the top folder renamed onto it (present flips here), temp + strays
    dropped, the nested 2.8 layout healed, exec bits repaired, the archive deleted."""
    _fetch_cancel.clear()
    with _op_lock:
        cat, tool = fetch_preflight(version, force)
        d = stack_dir(version)
        archive = archive_path(version)
        size, host = int(cat["size"]), urllib.parse.urlsplit(cat["url"]).hostname
        job.log("Downloading the %s server package (%.2f GB) from %s into %s ..." % (version, size / 1e9, host, archive))
        job.log("The game server keeps running; admin operations on this server wait until this finishes or is "
                "stopped (it continues where it stopped).")
    job.yielding = True
    downloaded = _fetch_stack_archive(cat, archive, job)
    job.log("Archive verified (size + md5).")
    # EXTRACT -- still yielding: it touches only the temp folder, and 26k files take minutes.
    tmp = extract_tmp(version)
    try:
        _rmtree_retry(tmp)
    except OSError as e:
        raise AgentError(500, "The temp folder %s of a previous run cannot be removed (%s) -- delete it by hand; the "
                              "verified archive is kept at %s." % (tmp, e, archive))
    job.log("Extracting with %s ..." % os.path.basename(tool))
    try:
        secs = extract_archive(archive, tmp, job, should_stop=_fetch_cancel.is_set)
    except ExtractStopped:
        _rmtree_quiet(tmp)
        raise AgentError(409, "Stopped on request during the extraction -- the verified archive is kept at %s; run "
                              "it again to extract it." % archive)
    except ExtractError as e:
        _rmtree_quiet(tmp)
        raise AgentError(500, "%s failed (%s) -- the verified archive is kept at %s for a retry."
                         % (os.path.basename(tool), e, archive))
    top = os.path.join(tmp, cat["topDir"])
    if not os.path.isfile(os.path.join(top, "docker-compose.yml.tmpl")):
        _rmtree_quiet(tmp)
        raise AgentError(500, "The archive does not contain %s/docker-compose.yml.tmpl -- the catalogue's topDir "
                              "does not match the archive; the verified archive is kept at %s."
                         % (cat["topDir"], archive))
    job.log("Extracted in %d s." % secs)
    # FINISH -- under _op_lock, so the other version's watchdog is never held by a multi-minute
    # extraction and no job sees the stack appear half-way.
    with _op_lock:
        if is_present(version) or not _dir_absent_or_empty(d):
            _rmtree_quiet(tmp)
            raise AgentError(409, "%s was filled while the download ran (someone extracted it by hand?) -- nothing "
                                  "replaced; the verified archive is kept at %s." % (d, archive))
        try:
            if os.path.isdir(d):
                os.rmdir(d)  # the empty target: the rename below wants the name free
            os.makedirs(os.path.dirname(os.path.abspath(d.rstrip("/\\"))), exist_ok=True)
            os.rename(top, d)  # present flips here
        except OSError as e:
            raise AgentError(500, "Could not move the extracted %s into place at %s (%s). The extracted tree is "
                                  "at %s and the archive at %s." % (cat["topDir"], d, e, top, archive))
        job.log("Installed into %s." % d)
        try:
            _rmtree_retry(tmp)  # drops the strays beside the top folder (2.8's data.txt)
        except OSError as e:
            job.log("WARNING: the temp folder %s could not be removed (%s) -- delete it by hand." % (tmp, e))
        healed = ensure_data_layout(version, job)
        exec_bits = ensure_exec_bits(version, job)
        _remove_quiet(archive)
        version_state_set(version, best_effort=True, fetchedAt=time.strftime("%Y-%m-%d %H:%M:%S"), fetchedFrom=host)
    job.log("Next: Prepare the server (launcher -> Server page, or POST /server/setup).")
    return {"fetched": True, "downloaded": downloaded, "bytes": size, "healed": healed, "execBits": exec_bits, "dir": d}


def _rmtree_quiet(path):
    try:
        _rmtree_retry(path)
    except OSError:
        pass


def fetch_brief(version, vs=None):
    """status() versions.<v>.fetch: is a download known, how big, from where, what is already on
    disk (.part bytes / a complete archive), which tool would unpack it, when it was fetched.
    Admin only -- build_public_status never carries it (the selftest asserts). Cheap: stats only."""
    cat = read_stack_catalog(version)
    if cat is not None:
        try:
            validate_stack_catalog(cat)
        except ValueError:
            cat = None  # an invalid catalogue is "no download known" for the card
    if vs is None:
        try:
            vs = version_state(version)
        except AgentError:
            vs = {}
    found = find_extractor()
    out = {"available": cat is not None, "size": int(cat["size"]) if cat else 0,
           "host": urllib.parse.urlsplit(cat["url"]).hostname if cat else None,
           "topDir": cat.get("topDir") if cat else None, "part": 0, "archive": False,
           "tool": os.path.basename(found[0]) if found else None, "fetchedAt": (vs or {}).get("fetchedAt")}
    if cat is not None and is_configured(version):
        ap = archive_path(version)
        st = _stat_or_none(ap + ".part")
        out["part"] = st.st_size if st is not None and st.st_size < int(cat["size"]) else 0
        st = _stat_or_none(ap)
        out["archive"] = st is not None and st.st_size == int(cat["size"])
    return out


def probe_authority(listen):
    """Where an agent bound to GIO_AGENT_LISTEN answers from THIS box: 127.0.0.1 for the wildcards
    (0.0.0.0, ::, empty), the literal otherwise, an IPv6 literal bracketed -- the same rule as
    install_agent.sh listen_host_port and the launcher's AgentInstallPlan.ProbeAuthority. Pure."""
    host, port = parse_listen(listen)
    h = (host or "").strip().strip("[]")
    if h in ("", "0.0.0.0", "::"):
        h = "127.0.0.1"
    elif ":" in h:
        h = "[%s]" % h
    return "%s:%d" % (h, port)


def _cli_list(name, argv=None):
    """The values after `--name` up to the next --option (e.g. --fetch 1.6 2.8)."""
    argv = sys.argv[1:] if argv is None else argv
    out, take = [], False
    for a in argv:
        if a == name:
            take = True
            continue
        if a.startswith("--"):
            take = False
            continue
        if take:
            out.append(a)
    return out


def fetch_cli(versions):
    """`gio_agent.py --config FILE --fetch 1.6 [2.8]`: ask the RUNNING agent (token + listen from the
    config) to download each version's server package in turn and stream the job log. Exit 0 all
    done / 1 a job failed / 2 usage / 3 the agent is busy / 4 the agent is unreachable."""
    if not versions or any(v not in VERSIONS for v in versions):
        print("usage: gio_agent.py [--config FILE] --fetch %s" % " | ".join(VERSIONS))
        return 2
    if not TOKEN:
        print("GIO_AGENT_TOKEN is empty -- pass --config /etc/gio-agent/config (or export it).")
        return 2
    base = "http://" + probe_authority(os.environ.get("GIO_AGENT_LISTEN", "127.0.0.1:18080"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through this shell's proxy

    def call(method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                return e.code, {"error": "HTTP %d" % e.code}

    try:
        code, health = call("GET", "/health", timeout=10)
        if code != 200:
            raise urllib.error.URLError("HTTP %d" % code)
    except (OSError, ValueError) as e:
        print("The agent does not answer at %s (%s) -- start the service first (systemctl start gio-agent)."
              % (base, _short_err(e)))
        return 4
    print("agent %s at %s" % (health.get("version", "?"), base))
    for v in versions:
        # Same guard as the health check and the follow loop below: the agent can go away between
        # calls (a systemd restart, a second install_agent.sh run, a `docker compose ls` that outlives
        # the timeout). Without it a socket timeout / reset propagates out of sys.exit(fetch_cli(...))
        # as a raw traceback in the middle of the installer's output, and the documented exit 4 --
        # "the agent is unreachable" -- is never returned.
        try:
            code, st = call("GET", "/status", timeout=90)
        except (OSError, ValueError) as e:
            print("%s: lost the connection to the agent (%s) -- is it still running? "
                  "(systemctl status gio-agent)" % (v, _short_err(e)))
            return 4
        if code == 401:
            print("The agent refused the token (401) -- is GIO_AGENT_TOKEN in the config the one it runs with?")
            return 4
        entry = ((st.get("versions") or {}).get(v) or {}) if code == 200 else {}
        if entry.get("present"):
            print("%s: already on this box (%s) -- skipped." % (v, entry.get("dir")))
            continue
        try:
            code, reply = call("POST", "/server/fetch", {"version": v}, timeout=120)
        except (OSError, ValueError) as e:
            print("%s: lost the connection to the agent while starting the download (%s) -- it may have "
                  "started anyway; run this command again to follow it." % (v, _short_err(e)))
            return 4
        if code == 409 and "Another operation" in str(reply.get("error", "")):
            print("%s: %s" % (v, reply.get("error")))
            return 3
        if code != 202:
            print("%s: the agent refused the download (HTTP %d): %s" % (v, code, reply.get("error") or reply))
            return 1
        jid = reply.get("job")
        print("%s: job %s started -- following it (Ctrl+C stops following, not the job; rerun this command or "
              "GET %s/jobs/%s?since=0 to watch again)" % (v, jid, base, jid))
        nxt = 0
        try:
            while True:
                code, snap = call("GET", "/jobs/%s?since=%d" % (jid, nxt), timeout=30)
                if code != 200:
                    print("%s: lost the job (HTTP %d: %s) -- was the agent restarted?" % (v, code, snap.get("error")))
                    return 1
                for line in snap.get("lines") or []:
                    print("  " + line)
                nxt = snap.get("next", nxt)
                if snap.get("state") == "done":
                    break
                if snap.get("state") == "error":
                    print("%s: FAILED -- %s" % (v, snap.get("error")))
                    return 1
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nstopped following; the job keeps running on the agent (see the command above).")
            return 1
        except (OSError, ValueError) as e:
            print("%s: lost the connection to the agent while following the job (%s) -- it keeps running; watch it "
                  "with GET %s/jobs/%s?since=0." % (v, _short_err(e), base, jid))
            return 4
    print("Next: Prepare the server (launcher -> Server page) or POST /server/setup.")
    return 0


class _PbParseError(Exception):
    pass


# Largest valid protobuf field number (2^29-1). Essential: a guid misread as a tag ALWAYS gives
# fn = guid>>3 >= 2^29 (guid >= 2^32), so the limit makes it impossible to "swallow" a guid into a
# tag -- without it a packed list of 2 guids parses as a message and the first guid escapes
# unrewritten (seen on the first dry-run on the box).
_PB_MAX_FIELD = (1 << 29) - 1

# 7.2 cur_scene_owner_uid, 28.5 recent_mp_player_uid_list, 28.14 friend_remark_name_map -- uids of
# OTHER players; dropped at copy time (empty in a solo save, but no chances taken).
_GUID_DROP_PATHS = {(7, 2), (28, 5), (28, 14)}
# Known packed guid lists -- treated as packed directly, never "guessed" as messages.
_GUID_PACKED_PATHS = {(2, 1, 101, 2), (2, 5, 2, 1), (2, 17)}


def _pb_varint(buf, i):
    r = 0
    s = 0
    while True:
        if i >= len(buf) or s > 63:
            raise _PbParseError("invalid varint at %d" % i)
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, i
        s += 7


def _pb_enc(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_packed(data, offs=None):
    """The values, or None when the bytes are not a clean run of varints. `offs` (a list) collects
    the offset each value starts at -- only meaningful when the result is not None."""
    vals = []
    i = 0
    try:
        while i < len(data):
            if offs is not None:
                offs.append(i)
            v, i = _pb_varint(data, i)
            vals.append(v)
    except _PbParseError:
        return None
    return vals


# -- settled event records (agent 3.7) --
# PlayerDataBin.activity_bin (field 20) = PlayerActivityCompBin, whose activity_bin_map (field 1)
# maps activity id -> ActivityBin {1 schedule_id, 3 cond_state_map, 5 is_settled, 9 is_started,
# 15 is_notify_mail_sent, 21 expired_cond_list, ...} -- names read from the descriptor the 2.8
# gameserver embeds. A record whose run is SETTLED is kept as long as the schedule id stays the same,
# and the server then never offers that run again: the shipped 2.8 pre-GAA save (the community's)
# carries Reminiscent Regimen (5083001) settled from an older run, so every account made from it --
# the default account, both templates -- never got the event's intro quest "Any Unsolved Mysteries?"
# (7051701, accepted on activity cond 5083001) once the agent opened the event (2026-09-29). An event
# the agent keeps open runs until 2050 and never settles, so a settled record of one is always a
# leftover of another run: removing it makes the server build a fresh record at the next login, as it
# does for a player who never saw the event.

PLAYER_ACTIVITY_FIELD = 20


def _pb_spans(buf):
    """[(field, wiretype, start, end, value_start, value_end)] of a message, or raise _PbParseError."""
    out, i, n = [], 0, len(buf)
    while i < n:
        start = i
        tag, i = _pb_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0 or fn > (1 << 29) - 1:
            raise _PbParseError("bad field number at %d" % start)
        if wt == 0:
            vs = i
            _, i = _pb_varint(buf, i)
        elif wt == 1:
            vs, i = i, i + 8
        elif wt == 5:
            vs, i = i, i + 4
        elif wt == 2:
            ln, vs = _pb_varint(buf, i)
            i = vs + ln
        else:
            raise _PbParseError("unsupported wire type %d at %d" % (wt, start))
        if i > n:
            raise _PbParseError("truncated field at %d" % start)
        out.append((fn, wt, start, i, vs, i))
    return out


def _pb_uint(buf, spans, field):
    """The first varint value of `field` in a parsed message, else None."""
    for fn, wt, _s, _e, vs, _ve in spans:
        if fn == field and wt == 0:
            return _pb_varint(buf, vs)[0]
    return None


def strip_settled_activities(payload, schedule_ids):
    """PlayerDataBin bytes -> (bytes, [(activity_id, schedule_id)] removed): drop every activity
    record whose schedule is in `schedule_ids` and whose run is settled; every other byte stays as
    it was. Pure; raises _PbParseError on bytes that are not a PlayerDataBin."""
    top = _pb_spans(payload)
    removed, out, changed = [], bytearray(), False
    for fn, wt, s, e, vs, ve in top:
        if fn != PLAYER_ACTIVITY_FIELD or wt != 2:
            out += payload[s:e]
            continue
        comp = payload[vs:ve]
        kept = bytearray()
        for cfn, cwt, cs, ce, cvs, cve in _pb_spans(comp):
            if cfn == 1 and cwt == 2:
                entry = comp[cvs:cve]
                es = _pb_spans(entry)
                key = _pb_uint(entry, es, 1)
                val = [(a, b, c, d, x, y) for a, b, c, d, x, y in es if a == 2 and b == 2]
                if val:
                    vb = entry[val[0][4]:val[0][5]]
                    vsp = _pb_spans(vb)
                    sched = _pb_uint(vb, vsp, 1)
                    if sched in schedule_ids and _pb_uint(vb, vsp, 5):
                        removed.append((key, sched))
                        continue
            kept += comp[cs:ce]
        if len(kept) == len(comp):
            out += payload[s:e]
            continue
        changed = True
        out += _pb_enc((fn << 3) | 2) + _pb_enc(len(kept)) + kept
    return (bytes(out) if changed else payload), removed


def strip_settled_blob(blob, schedule_ids):
    """The same on a stored bin_data (magic "ZLIB" + zlib, or raw): (blob, removed). A blob that
    does not parse is returned untouched with nothing removed."""
    wrapped = blob[:4] == b"ZLIB"
    try:
        payload = zlib.decompress(blob[4:]) if wrapped else blob
        new, removed = strip_settled_activities(payload, schedule_ids)
    except (zlib.error, _PbParseError):
        return blob, []
    if not removed:
        return blob, []
    return ((b"ZLIB" + zlib.compress(new, 6)) if wrapped else new), removed


def open_schedule_ids(version, man=None):
    """The schedule ids the manifest keeps open ('gaa'), as ints."""
    man = read_manifest(version) if man is None else man
    ev = (man or {}).get("events") or {}
    return {int(k) for k in (ev.get("gaa") or {}) if str(k).isdigit()}


def repair_settled_activities(version, job):
    """Remove the settled record of every event this version keeps open from every player save on the
    stack (strip_settled_blob). The CALLER guarantees the gameserver is not running (a cold start
    before the game services boot, a provision, /server/events between its stop and start): the
    gameserver holds its players in memory and would write the old save back. Each write is guarded
    by the md5 of the save it replaces (a save that moved meanwhile is left for the next pass) and
    the old rows go to the backups folder first. Never raises -- a start or a provision goes on."""
    try:
        ids = open_schedule_ids(version)
        if not ids:
            return 0
        rows = []
        for shard in range(10):
            r = mysql_exec(version, "SELECT uid, MD5(bin_data), TO_BASE64(bin_data) FROM t_player_data_%d"
                           % shard, db=PLAYER_DB, timeout=300)
            if r.returncode != 0:
                if _SQL_MISSING_OBJECT_RE.search(r.stderr or ""):
                    continue
                raise AgentError(500, _cmd_err(r, 300))
            for line in (r.stdout or "").splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) == 3 and cols[0].strip().isdigit():
                    rows.append((shard, int(cols[0]), cols[1].strip(),
                                 base64.b64decode(cols[2].replace("\\n", "").strip())))
        fixes = []
        for shard, uid, md5, blob in rows:
            new, removed = strip_settled_blob(blob, ids)
            if removed:
                fixes.append((shard, uid, md5, blob, new, removed))
        if not fixes:
            return 0
        bdir = os.path.join(os.path.dirname(STATE_PATH) or ".", "backups")
        os.makedirs(bdir, exist_ok=True)
        protect_path(bdir, 0o700)
        bpath = os.path.join(bdir, "settled_%s_%s.sql" % (version.replace(".", ""), time.strftime("%Y%m%d_%H%M%S")))
        with open(bpath, "w", encoding="utf-8") as f:
            for shard, uid, _md5, blob, _new, _rm in fixes:
                f.write("UPDATE t_player_data_%d SET bin_data = 0x%s WHERE uid = %d;\n" % (shard, blob.hex(), uid))
        protect_path(bpath, 0o600)
        done = 0
        import tempfile
        for shard, uid, md5, blob, new, removed in fixes:
            what = ", ".join("%s (schedule %s)" % (a, s) for a, s in removed)
            # Through stdin, like copy_save: a save is ~100 KB of hex, past a Windows command line.
            fd, tmp = tempfile.mkstemp(suffix=".sql")
            os.close(fd)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("UPDATE t_player_data_%d SET bin_data = 0x%s WHERE uid = %d AND MD5(bin_data) = '%s';\n"
                            "SELECT ROW_COUNT();\n" % (shard, new.hex(), uid, md5))
                r = mysql_import(version, tmp, PLAYER_DB, timeout=300)
            finally:
                os.remove(tmp)
            if r.returncode == 0 and _scalar(r) == "1":
                done += 1
                job.log("  uid %d: the settled record of %s removed -- the next login starts it afresh." % (uid, what))
            else:
                job.log("  uid %d: not rewritten (%s) -- the next pass retries."
                        % (uid, "its save changed meanwhile" if r.returncode == 0 else _cmd_err(r, 200)))
        job.log("Settled event records of open events: %d save(s) repaired (old rows kept in %s)." % (done, bpath))
        return done
    except Exception as e:  # noqa: BLE001 -- never fails the start / provision / events run
        job.log("WARNING: could not check the player saves for settled event records (%s)." % _short_err(e))
        return 0


class _GuidSpans:
    """What the walk did with each byte, in the coordinates of the buffer it was handed. Filled by
    _guid_rewrite when one is passed in; because a changing pass moves bytes around, the offsets
    only mean anything on a pass that changes NOTHING -- which is exactly the second pass copy_save
    runs over its own result. `vvals`/`fvals` are the offsets of the values the walk COMPARED
    against the source uid (varint values and packed elements / fixed64 values); `opaque` are the
    length-delimited payloads it could neither parse as a message nor decode as a packed list, and
    therefore copied verbatim -- the only bytes a guid can hide in."""

    __slots__ = ("opaque", "vvals", "fvals")

    def __init__(self):
        self.opaque = []   # [(start, end)) copied verbatim -- never decoded
        self.vvals = []    # offsets of varint values / packed elements the walk compared
        self.fvals = []    # offsets of fixed64 values the walk compared

    def mark(self):
        return len(self.opaque), len(self.vvals), len(self.fvals)

    def rewind(self, mark):
        """Forget the half-walk of a buffer that turned out not to be a message."""
        del self.opaque[mark[0]:]
        del self.vvals[mark[1]:]
        del self.fvals[mark[2]:]

    def is_opaque(self, off):
        return any(s <= off < e for s, e in self.opaque)


def _guid_rewrite(buf, src, dst, path, log, smap=None, base=0):
    """(new_bytes, changed). Whatever does not change is copied byte-for-byte, so the inverse
    transformation applied to the result reproduces the original EXACTLY -- the self-test run
    before any write. `smap`/`base` only RECORD where the walk went (see _GuidSpans); they never
    change what comes out."""
    out = bytearray()
    i = 0
    changed = False
    while i < len(buf):
        start = i
        tag, i = _pb_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0 or fn > _PB_MAX_FIELD:
            raise _PbParseError("invalid field number %d" % fn)
        p = path + (fn,)
        tag_end = i
        if wt == 0:
            v, i = _pb_varint(buf, i)
            if p in _GUID_DROP_PATHS:
                changed = True
                log.append(("drop", p, 1))
                continue
            if smap is not None:
                smap.vvals.append(base + tag_end)
            if v >> 32 == src:
                changed = True
                log.append(("varint", p, 1))
                out += buf[start:tag_end] + _pb_enc((dst << 32) | (v & 0xFFFFFFFF))
                continue
            out += buf[start:i]
        elif wt == 1:
            if i + 8 > len(buf):
                raise _PbParseError("truncated fixed64")
            v = int.from_bytes(buf[i:i + 8], "little")
            i += 8
            if p in _GUID_DROP_PATHS:
                changed = True
                log.append(("drop", p, 1))
                continue
            if smap is not None:
                smap.fvals.append(base + tag_end)
            if v >> 32 == src:
                changed = True
                log.append(("fixed64", p, 1))
                out += buf[start:tag_end] + ((dst << 32) | (v & 0xFFFFFFFF)).to_bytes(8, "little")
                continue
            out += buf[start:i]
        elif wt == 2:
            ln, ds = _pb_varint(buf, i)
            de = ds + ln
            if de > len(buf):
                raise _PbParseError("truncated length-delimited field")
            data = buf[ds:de]
            i = de
            if p in _GUID_DROP_PATHS:
                changed = True
                log.append(("drop", p, 1))
                continue
            new = None
            if p in _GUID_PACKED_PATHS:
                offs = [] if smap is not None else None
                packed = _pb_packed(data, offs)
                if packed is not None:
                    if smap is not None:
                        smap.vvals.extend(base + ds + o for o in offs)
                    if any(v >> 32 == src for v in packed):
                        log.append(("packed", p, sum(1 for v in packed if v >> 32 == src)))
                        new = b"".join(_pb_enc((dst << 32) | (v & 0xFFFFFFFF)) if v >> 32 == src
                                       else _pb_enc(v) for v in packed)
                    if new is None:
                        out += buf[start:de]
                    else:
                        changed = True
                        out += buf[start:tag_end] + _pb_enc(len(new)) + new
                    continue
            sublog = []
            mark = smap.mark() if smap is not None else None
            try:
                sub, subchanged = _guid_rewrite(data, src, dst, p, sublog, smap, base + ds)
                if subchanged:
                    new = sub
                    log.extend(sublog)
            except _PbParseError:
                # Not a message -- maybe an unknown packed guid list (the field-number limit
                # guarantees a guid-in-tag fails HERE instead of passing as a valid message).
                if smap is not None:
                    smap.rewind(mark)
                offs = [] if smap is not None else None
                packed = _pb_packed(data, offs)
                if smap is not None:
                    if packed is None:
                        smap.opaque.append((base + ds, base + de))
                    else:
                        smap.vvals.extend(base + ds + o for o in offs)
                if packed is not None and any(v >> 32 == src for v in packed):
                    log.append(("packed-new", p, sum(1 for v in packed if v >> 32 == src)))
                    new = b"".join(_pb_enc((dst << 32) | (v & 0xFFFFFFFF)) if v >> 32 == src
                                   else _pb_enc(v) for v in packed)
            if new is None:
                out += buf[start:de]
            else:
                changed = True
                out += buf[start:tag_end] + _pb_enc(len(new)) + new
        elif wt == 5:
            if i + 4 > len(buf):
                raise _PbParseError("truncated fixed32")
            out += buf[start:i + 4]
            i += 4
        else:
            raise _PbParseError("wiretype %d" % wt)
    return bytes(out), changed


def _guid_scan(buf):
    """{high32: count} over the varint/fixed64 values, recursive (best effort). DIAGNOSTIC ONLY --
    it is a LAXER walk than _guid_rewrite (it has no _GUID_PACKED_PATHS, no packed fallback, and it
    recurses into every length-delimited field), so it must never be used to judge the rewriter's
    output: it used to read a fixed64 PAST the end of an opaque bytes field, which on a small source
    uid invented a guid out of the short slice and made copy_save refuse a correct save (2026-09-21,
    a 2.8 template signup). The bounds checks below now make it exactly as strict about truncation
    as _guid_rewrite is."""
    counts = {}
    i = 0
    while i < len(buf):
        tag, i = _pb_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0 or fn > _PB_MAX_FIELD:
            raise _PbParseError("invalid field")
        if wt == 0:
            v, i = _pb_varint(buf, i)
            counts[v >> 32] = counts.get(v >> 32, 0) + 1
        elif wt == 1:
            if i + 8 > len(buf):
                raise _PbParseError("truncated fixed64")
            v = int.from_bytes(buf[i:i + 8], "little")
            i += 8
            counts[v >> 32] = counts.get(v >> 32, 0) + 1
        elif wt == 2:
            ln, ds = _pb_varint(buf, i)
            i = ds + ln
            if i > len(buf):
                raise _PbParseError("truncated")
            try:
                for k, n in _guid_scan(buf[ds:i]).items():
                    counts[k] = counts.get(k, 0) + n
            except _PbParseError:
                pass
        elif wt == 5:
            if i + 4 > len(buf):
                raise _PbParseError("truncated fixed32")
            i += 4
        else:
            raise _PbParseError("wiretype %d" % wt)
    return counts


def _guid_raw_scan(buf, uid):
    """Look for the uid in high32 straight on the bytes, independent of the parser: (a) a varint
    >= 5 bytes at any offset, (b) a LE u64 window. Catches what the parser would miss; has rare
    false positives (float bytes), which is why the result is judged, not taken blindly."""
    varint_hits = []
    fixed_hits = []
    for i in range(len(buf)):
        if buf[i] & 0x80:
            try:
                v, end = _pb_varint(buf, i)
                if v >> 32 == uid and end - i >= 5:
                    varint_hits.append(i)
            except _PbParseError:
                pass
        if i + 8 <= len(buf) and int.from_bytes(buf[i:i + 8], "little") >> 32 == uid:
            fixed_hits.append(i)
    return varint_hits, fixed_hits


def _raw_scan_in_spans(buf, uid, spans):
    """_guid_raw_scan counted only where a hit STARTS inside one of `spans` (the byte ranges the
    walk copied verbatim). Cheap enough to repeat for several control uids."""
    n = 0
    for s, e in spans:
        for i in range(s, min(e, len(buf))):
            if buf[i] & 0x80:
                try:
                    v, end = _pb_varint(buf, i)
                    if v >> 32 == uid and end - i >= 5:
                        n += 1
                except _PbParseError:
                    pass
            if i + 8 <= len(buf) and int.from_bytes(buf[i:i + 8], "little") >> 32 == uid:
                n += 1
    return n


# The source uid's high32 is a BYTE PATTERN, and for a small uid a very common one: every template
# signup copies from uid 1, whose 64-bit footprint is the window `.. .. .. .. 01 00 00 00` -- plain
# zero padding, which turns up all over a protobuf payload. A FIXED residual budget therefore
# cannot be right for both uid 1 and uid 10437, so the budget is MEASURED on the very bytes about
# to be written, with control uids that cannot own a guid in this save.
_RAW_NOISE_CONTROLS = 6
_RAW_NOISE_MARGIN = 2


def _raw_noise_controls(src, dst, n=_RAW_NOISE_CONTROLS):
    """Uids no guid in this save can belong to, as close to `src` as possible so their high32 keeps
    the same byte width -- i.e. the same chance of turning up in the bytes by accident."""
    width = max(1, (src.bit_length() + 7) // 8)
    lo = 1 if width == 1 else 1 << (8 * (width - 1))
    hi = (1 << (8 * width)) - 1
    out, up, down = [], src, src
    while len(out) < n and (up < hi or down > lo):
        if up < hi:
            up += 1
            if up not in (src, dst):
                out.append(up)
        if len(out) < n and down > lo:
            down -= 1
            if down not in (src, dst):
                out.append(down)
    return out


def _guid_residual_check(job, buf, src, dst, smap):
    """Judge what the byte-level scan still finds in the rewritten save, using the map the walk
    just built. Three kinds of hit, and only one of them is evidence of a missed guid:
      * on a value the walk COMPARED, of the same kind it read there (a varint value / a packed
        element, or a fixed64 value at that exact offset): the walk decoded those very bytes the
        very same way and concluded "not the source uid", so a hit here is a flat contradiction --
        refused with no threshold at all;
      * inside a span the walk copied VERBATIM (a bytes field it could not decode): the rewriter
        cannot reach into those, so this is the one place a source guid can genuinely survive --
        judged against a noise budget measured on the same spans with control uids;
      * anywhere else: a window straddling fields the walk DID account for -- the float bytes of a
        position vector and friends. Noise by construction (after a clean pass no compared value
        has high32 == src any more), so it is logged and never counted against anything."""
    av, af = _guid_raw_scan(buf, src)
    vset, fset = set(smap.vvals), set(smap.fvals)
    onval, opaque, noise = [], [], []
    for off, seen in [(o, vset) for o in av] + [(o, fset) for o in af]:
        (onval if off in seen else opaque if smap.is_opaque(off) else noise).append(off)
    for off in (onval + opaque + noise)[:6]:
        job.log("  raw trace at offset %d: %s" % (off, buf[max(0, off - 8):off + 16].hex()))
    if onval:
        raise AgentError(500, "The source uid is still in a value the rewrite decoded (offset %d) -- "
                              "refusing to write (see ACCOUNT-COPY-HANDOFF.md)." % onval[0])
    floor = max([_raw_scan_in_spans(buf, c, smap.opaque)
                 for c in _raw_noise_controls(src, dst)] or [0])
    if len(opaque) > floor + _RAW_NOISE_MARGIN:
        raise AgentError(500, "%d traces of the source uid are left in bytes fields the rewrite cannot "
                              "decode, where a uid that owns nothing here shows at most %d -- refusing "
                              "to write (possibly an unknown field; see ACCOUNT-COPY-HANDOFF.md)."
                         % (len(opaque), floor))
    if opaque or noise:
        job.log("(%d raw matches left: %d inside undecodable bytes fields (noise budget %d), %d "
                "straddling decoded fields -- accepted)"
                % (len(opaque) + len(noise), len(opaque), floor + _RAW_NOISE_MARGIN, len(noise)))


PLAYER_DB = "hk4e_db_user"


def _mysql_q(version, sql, db=PLAYER_DB):
    """Small SELECT/INSERT against `db` (the player database by default), no header (-N). Raises
    AgentError on failure."""
    r = _run(_mysql_args(version, db) + ["-N", "-e", sql], timeout=120)
    if r.returncode != 0:
        raise AgentError(500, "MySQL: %s" % _cmd_err(r, 300, fallback="error"))
    return (r.stdout or "").strip()


def redis_password(version):
    path = os.path.join(stack_dir(version), "docker-compose.yml")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r"requirepass\s+(\S+)", f.read())
    except FileNotFoundError:
        m = None
    return m.group(1) if m else ""


def _uid_online(version, uid):
    """The PlayerStatus:{uid} key exists in redis db7 only while the player is in game (the
    gameserver writes it at login and deletes it at logout). The name is derived from the
    PlayerStatusRedisData class -- the last_save_time check below stays as the safety net."""
    pw = redis_password(version)
    args = ["docker", "compose", "--project-directory", stack_dir(version), "exec", "-T", "redis",
            "redis-cli"] + (["-a", pw, "--no-auth-warning"] if pw else []) + \
           ["-n", "7", "EXISTS", "PlayerStatus:{%d}" % uid]
    r = _run(args, timeout=60)
    if r.returncode != 0:
        raise AgentError(500, "Cannot check in redis whether uid %d is online: %s"
                         % (uid, _cmd_err(r, 200)))
    return (r.stdout or "").strip() == "1"


def _sdk_db_path(version):
    return os.path.join(stack_dir(version), "sdk", "data", "sdk.db")


def _sdk_db_lookup(version, name):
    """(sdk_uid, exact_name) or None. Reads a COPY of sdk.db (SQLite) so the sdk service's live
    file is never touched; exact match first, then case-insensitive (SQLite compares names
    case-sensitively and the game needs the exact form -- 'aetherr' vs 'Aetherr' on 2.8)."""
    src = _sdk_db_path(version)
    if not os.path.isfile(src):
        raise AgentError(500, "The stack's account database is missing (%s)." % src)
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        con = sqlite3.connect(tmp)
        try:
            row = con.execute("SELECT uid, name FROM accounts WHERE name = ?", (name,)).fetchone()
            if row is None:
                row = con.execute("SELECT uid, name FROM accounts WHERE name = ? COLLATE NOCASE",
                                  (name,)).fetchone()
            return (int(row[0]), row[1]) if row else None
        finally:
            con.close()
    finally:
        os.remove(tmp)


def parse_sdk_register_reply(html):
    """Classify the vendor register page's answer: "created" | "taken" | "refused:<why>" | None
    (no recognisable flash message -- the page text is the only signal the vendor gives)."""
    text = html or ""
    if "Account created" in text:
        return "created"
    if "already exists" in text:
        return "taken"
    for marker, why in (("Invalid email", "invalid e-mail"),
                        ("do not match", "passwords do not match"),
                        ("Password must", "password too short"),
                        ("currently unavailable", "service unavailable")):
        if marker in text:
            return "refused:" + why
    return None


def _sdk_register(version, name, job, password=None):
    """Create the account through the sdk service's normal register page (port 21000, published on
    OUTER_IP, not on loopback). Without password verification the password is irrelevant -- the
    server never checks it -- so one is generated to pass the form's validation; with it on, the
    caller passes the player's own. Raises 409 when the sdk says the name is taken, 503 when the
    sdk is unreachable, 400 when the form refuses the input."""
    ip = read_env(version).get("OUTER_IP", "").strip() or "127.0.0.1"
    pwd = password or "".join(random.choice(string.ascii_letters + string.digits) for _ in range(12))
    email = "%s.%s@relic.local" % (re.sub(r"[^A-Za-z0-9]", "", name).lower() or "account",
                                   "".join(random.choice(string.digits) for _ in range(4)))
    data = urllib.parse.urlencode({"username": name, "email": email,
                                   "password": pwd, "passwordv2": pwd}).encode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request("http://%s:21000/account/register" % ip, data=data),
                timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        raise AgentError(503, "Could not reach the register page (%s:21000): %s" % (ip, e))
    verdict = parse_sdk_register_reply(body)
    if verdict == "taken":
        raise AgentError(409, "name taken", code="name_taken")
    if verdict and verdict.startswith("refused:"):
        raise AgentError(400, "the server refused the registration (%s)" % verdict[len("refused:"):])
    if verdict != "created":
        job.log("WARNING: the register page gave no recognisable answer -- checking sdk.db.")
    job.log("Account '%s' registered%s." % (
        name, "" if password else " (generated password -- the server does not verify it)"))


def _resolve_player(version, job, spec, create):
    """Account name (or, as an escape hatch, a numeric game uid) -> game uid.
    create=True: the sdk account is created when missing, and the game uid is allocated through
    t_player_uid (the same INSERT dbgate would do at first login -- AUTO_INCREMENT yields the uid)."""
    spec = str(spec).strip()
    if not spec:
        raise AgentError(400, "The account name is empty.")
    acc = _sdk_db_lookup(version, spec)
    if acc is None and spec.isdigit():
        if _mysql_q(version, "SELECT uid FROM t_player_uid WHERE uid=%d" % int(spec)):
            job.log("'%s' is not an account name -- using it directly as a game uid." % spec)
            return int(spec)
    if acc is None:
        if not create:
            raise AgentError(404, "The source account '%s' does not exist on the server." % spec)
        job.log("Account '%s' does not exist -- creating it..." % spec)
        _sdk_register(version, spec, job)
        acc = _sdk_db_lookup(version, spec)
        if acc is None:
            raise AgentError(500, "Account '%s' did not appear after registration -- name refused by "
                                  "the sdk?" % spec)
    sdk_uid, exact = acc
    if exact != spec:
        job.log("Note: the exact name on the server is '%s' -- that is the form used to log in." % exact)
    row = _mysql_q(version, "SELECT uid FROM t_player_uid WHERE account_type=1 AND account_uid='%d'" % sdk_uid)
    if row:
        return int(row.split()[0])
    if not create:
        raise AgentError(409, "Account '%s' exists but has never entered the game -- no progress to copy." % exact)
    _mysql_q(version, "INSERT INTO t_player_uid (account_type, account_uid, ext) "
                      "VALUES (1, '%d', '{\"reg_platform\":3}\\n')" % sdk_uid)
    row = _mysql_q(version, "SELECT uid FROM t_player_uid WHERE account_type=1 AND account_uid='%d'" % sdk_uid)
    uid = int(row.split()[0])
    job.log("Game uid allocated for '%s': %d." % (exact, uid))
    return uid


def build_copy_sql(src_uid, dst_uid, new_blob_hex, n_home, src_db=PLAYER_DB):
    """The transaction that writes a rewritten save over the destination uid. Pure. The SOURCE
    side of every INSERT ... SELECT is qualified with `src_db` so a progress template
    (relic_tpl_<id>) can feed the player database; the destination is always the current
    database (hk4e_db_user) -- unqualified, exactly as before."""
    s_shard, d_shard = src_uid % 10, dst_uid % 10
    # The default source stays UNQUALIFIED: the admin copy's statements are byte-for-byte the
    # ones validated live; only a template source names its database.
    src = "" if src_db == PLAYER_DB else "`%s`." % src_db
    sqls = ["START TRANSACTION;",
            "DELETE FROM t_player_data_%d WHERE uid=%d;" % (d_shard, dst_uid),
            "INSERT INTO t_player_data_%d (uid,nickname,level,exp,vip_point,json_data,bin_data,"
            "extra_bin_data,data_version,tag_list,before_login_bin_data) "
            "SELECT %d,nickname,level,exp,vip_point,json_data,0x%s,extra_bin_data,data_version,"
            "tag_list,before_login_bin_data FROM %st_player_data_%d WHERE uid=%d;"
            % (d_shard, dst_uid, new_blob_hex, src, s_shard, src_uid),
            "DELETE FROM t_block_data_%d WHERE uid=%d;" % (d_shard, dst_uid),
            "INSERT INTO t_block_data_%d (uid,block_id,data_version,bin_data) "
            "SELECT %d,block_id,data_version,bin_data FROM %st_block_data_%d WHERE uid=%d;"
            % (d_shard, dst_uid, src, s_shard, src_uid)]
    # The destination's teapot rows go whatever the source holds: with the DELETE inside the gate, a
    # source without a Serenitea Pot left the destination's own realm rows next to the copied save.
    sqls.append("DELETE FROM t_home_data_%d WHERE uid=%d;" % (d_shard, dst_uid))
    if n_home:
        sqls.append("INSERT INTO t_home_data_%d (uid,bin_data,data_version) "
                    "SELECT %d,bin_data,data_version FROM %st_home_data_%d WHERE uid=%d;"
                    % (d_shard, dst_uid, src, s_shard, src_uid))
    sqls.append("COMMIT;")
    return sqls


def _write_copy_sql(version, job, sql_path):
    """Run the copy transaction, surviving a database service that dies under it. MariaDB 10.9
    segfaulted inside ha_write_row on a Windows bind-mounted data dir (2026-09-23: an admin account
    creation left the account without its progress, and every rollback statement failed too because
    docker was still restarting the service). The transaction never committed, so the destination is
    exactly as it was: wait for the service and write once more. Raises AgentError on a real failure,
    with the tool's own notices stripped -- they used to be the whole message."""
    r = mysql_import(version, sql_path, PLAYER_DB)
    if r.returncode != 0 and not mysql_reachable(version):
        job.log("The database service went away during the write -- waiting for it to come back.")
        try:
            wait_for_mysql(version, job, timeout=180)
        except AgentError:
            # It never came back: the rollback that follows must not spend its own wait on the same
            # dead service -- the job slot is held all this time and the watchdog cannot revive it.
            job.mysql_wait_expired = True
            raise
        job.log("Writing the progress again.")
        r = mysql_import(version, sql_path, PLAYER_DB)
    if r.returncode != 0:
        raise AgentError(500, "Writing the progress failed: %s" % _cmd_err(r, 300))


def copy_save(version, job, src_uid, dst_uid, src_db=PLAYER_DB, dry_run=False):
    """Copy the save of `src_uid` (read from `src_db`) over `dst_uid` in the player database, on
    the same stack, rewriting every guid from (src<<32) to (dst<<32). Does not stop the server:
    the save is loaded from MySQL at login, so it is enough that BOTH players are offline. The
    destination's progress so far is lost (backed up on the box first). A non-default `src_db`
    is a progress template nobody can be logged into, so the online/recency gates are skipped
    for the source side only."""
    scratch_src = src_db != PLAYER_DB
    if src_uid == dst_uid and not scratch_src:
        raise AgentError(400, "Source and destination are the same player (uid %d)." % src_uid)
    job.log("Copying progress: uid %d%s -> uid %d." % (src_uid, " (from %s)" % src_db if scratch_src else "", dst_uid))

    for uid, what in ((src_uid, "source"), (dst_uid, "destination")):
        if what == "source" and scratch_src:
            continue
        if _uid_online(version, uid):
            raise AgentError(409, "The %s player (uid %d) is ONLINE -- they must leave the game first." % (what, uid))
    s_shard, d_shard = src_uid % 10, dst_uid % 10
    age = _mysql_q(version, "SELECT TIMESTAMPDIFF(SECOND, last_save_time, NOW()) "
                            "FROM t_player_data_%d WHERE uid=%d" % (s_shard, src_uid), db=src_db)
    if not age:
        raise AgentError(409, "The source account (uid %d) has no save on the server yet." % src_uid)
    d_age = _mysql_q(version, "SELECT TIMESTAMPDIFF(SECOND, last_save_time, NOW()) "
                              "FROM t_player_data_%d WHERE uid=%d" % (d_shard, dst_uid))
    for label, a in (("source", age), ("destination", d_age)):
        if label == "source" and scratch_src:
            continue
        # The gameserver keeps the player in RAM and saves every 60-120s: a very recent save means
        # "probably still in game" even if the presence key is gone.
        if a and int(a) < 180:
            raise AgentError(409, "The %s account saved %ss ago -- it seems to be in game still; "
                                  "wait ~3 minutes after logout and retry." % (label, a))

    b64 = _mysql_q(version, "SELECT TO_BASE64(bin_data) FROM t_player_data_%d WHERE uid=%d"
                   % (s_shard, src_uid), db=src_db).replace("\\n", "").replace("\n", "")
    blob = base64.b64decode(b64)
    wrapped = blob[:4] == b"ZLIB"
    payload = zlib.decompress(blob[4:]) if wrapped else blob
    job.log("Source save: %d B (%s), %d B decompressed." % (len(blob), "ZLIB" if wrapped else "raw", len(payload)))

    entries = []
    new_payload, _ = _guid_rewrite(payload, src_uid, dst_uid, (), entries)
    n_guid = sum(n for k, _, n in entries if k != "drop")
    n_drop = sum(n for k, _, n in entries if k == "drop")
    for k, p, n in entries:
        if k == "packed-new":
            job.log("WARNING: UNKNOWN packed guid list at path %s (%d values) -- rewritten; note it "
                    "in _GUID_PACKED_PATHS." % (".".join(map(str, p)), n))
    if not n_drop:
        back, _ = _guid_rewrite(new_payload, dst_uid, src_uid, (), [])
        if back != payload:
            raise AgentError(500, "The roundtrip self-test failed -- writing nothing.")
    # The gate is the rewriter run a SECOND time over its own output: a pass that changes nothing
    # is the proof that no source guid is left anywhere the walk can see, and using one parser for
    # both the work and the check is the whole point -- the old gate was a laxer parser (_guid_scan)
    # and refused saves the rewriter had handled correctly (2026-09-21). The same pass hands back
    # the map of where it went, which is what the byte-level residual check is judged against.
    smap = _GuidSpans()
    again, changed_again = _guid_rewrite(new_payload, src_uid, dst_uid, (), [], smap=smap)
    if changed_again or again != new_payload:
        raise AgentError(500, "Source guids remain after the transformation -- writing nothing.")
    try:
        left = _guid_scan(new_payload).get(src_uid)
    except _PbParseError:
        left = None
    if left:
        job.log("NOTE: the loose scan still counts %d value(s) with high32 == %d; the rewriter's own "
                "pass sees none, so they sit in bytes it does not decode." % (left, src_uid))
    _guid_residual_check(job, new_payload, src_uid, dst_uid, smap)

    new_blob = (b"ZLIB" + zlib.compress(new_payload, 6)) if wrapped else new_payload
    n_blocks = int(_mysql_q(version, "SELECT COUNT(*) FROM t_block_data_%d WHERE uid=%d" % (s_shard, src_uid), db=src_db) or 0)
    n_home = int(_mysql_q(version, "SELECT COUNT(*) FROM t_home_data_%d WHERE uid=%d" % (s_shard, src_uid), db=src_db) or 0)
    job.log("%d guids rewritten, %d co-op fields dropped; to copy: the main save, %d scene blocks, "
            "%d teapot rows." % (n_guid, n_drop, n_blocks, n_home))
    if dry_run:
        job.log("DRY-RUN: everything checks out, but nothing is written.")
        return {"srcUid": src_uid, "dstUid": dst_uid, "guids": n_guid, "blocks": n_blocks, "dryRun": True}

    bdir = os.path.join(os.path.dirname(STATE_PATH) or ".", "backups")
    os.makedirs(bdir, exist_ok=True)
    protect_path(bdir, 0o700)  # full player saves live here -- not for every local user's eyes
    bpath = os.path.join(bdir, "acctcopy_%s_uid%d_%s.sql"
                         % (version.replace(".", ""), dst_uid, time.strftime("%Y%m%d_%H%M%S")))
    r = _run(["docker", "compose", "--project-directory", stack_dir(version), "exec", "-T",
              "-e", "MYSQL_PWD=" + mysql_password(version), "mysql", "mysqldump", "-uroot",
              "--no-create-info", "--hex-blob", "--where=uid=%d" % dst_uid, PLAYER_DB,
              "t_player_data_%d" % d_shard, "t_block_data_%d" % d_shard, "t_home_data_%d" % d_shard],
             timeout=300)
    if r.returncode != 0:
        raise AgentError(500, "Backing up the destination's progress failed: %s" % _cmd_err(r, 200))
    with open(bpath, "w", encoding="utf-8") as f:
        f.write(r.stdout or "")
    protect_path(bpath, 0o600)
    job.log("Destination progress backup: %s" % bpath)

    sqls = build_copy_sql(src_uid, dst_uid, new_blob.hex(), n_home, src_db=src_db)
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".sql")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(sqls))
        _write_copy_sql(version, job, tmp)
    finally:
        os.remove(tmp)

    vb64 = _mysql_q(version, "SELECT TO_BASE64(bin_data) FROM t_player_data_%d WHERE uid=%d"
                    % (d_shard, dst_uid)).replace("\\n", "").replace("\n", "")
    if base64.b64decode(vb64) != new_blob:
        raise AgentError(500, "Verification failed: the save read back differs from the one written!")
    vb = int(_mysql_q(version, "SELECT COUNT(*) FROM t_block_data_%d WHERE uid=%d" % (d_shard, dst_uid)) or 0)
    if vb != n_blocks:
        raise AgentError(500, "Verification failed: %d/%d blocks copied." % (vb, n_blocks))
    job.log("Verified: the save is identical on uid %d and all %d blocks are there." % (dst_uid, n_blocks))
    job.log("Both accounts now share the in-game nickname; mails and friends are NOT copied (they "
            "live per uid). The destination logs in and finds the progress.")
    return {"srcUid": src_uid, "dstUid": dst_uid, "guids": n_guid, "blocks": n_blocks,
            "backup": bpath, "dryRun": False}


def do_account_copy(version, job, src_spec, dst_spec, dry_run=False):
    """Copy the source account's progress over the destination account (created when missing),
    on the same stack. Admin path -- behaviour unchanged: both accounts live in the player DB."""
    if not is_bootstrapped(version):
        raise AgentError(409, "The %s stack is not installed on the box." % version)
    if not mysql_reachable(version):
        raise AgentError(409, "Server %s is not running (MySQL does not answer). Start it first -- "
                              "the copy does NOT stop the stack and does not disturb other players."
                         % version)

    job.log("Resolving the accounts...")
    src_uid = _resolve_player(version, job, src_spec, create=False)
    dst_uid = _resolve_player(version, job, dst_spec, create=True)
    return copy_save(version, job, src_uid, dst_uid, dry_run=dry_run)


# -- admin policy (what players may do without a token) --

MAX_PER_PLAYER_DEFAULT = 5  # accounts one launcher may create per version (agent 3.7)
MAX_PER_PLAYER_MAX = 1000


def default_version_policy(v):
    """A version's signup defaults: every starting point the manifest ships -- the new account first,
    then the templates in manifest order (agent 3.7; before, the new account only) -- 50 accounts a
    day server-wide, MAX_PER_PLAYER_DEFAULT per launcher. A template that is not imported on the
    stack is still left out of /public/status (build_public_status), so offering it costs nothing."""
    return {"templates": ["fresh"] + [t["id"] for t in manifest_templates(v) if t["id"] != "fresh"],
            "maxPerDay": 50, "maxPerPlayer": MAX_PER_PLAYER_DEFAULT}


def default_policy():
    # Player signup and player GM commands are ON by default (agent 3.4): a friends server works out
    # of the box; the admin turns either off from the Player accounts card. A stored policy
    # (set_policy persists the full object) keeps its own values across upgrades -- a key it does not
    # hold (maxPerPlayer, for one written before 3.7) takes the default.
    return {
        "name": "",
        "signup": {"enabled": True,
                   "versions": {v: default_version_policy(v) for v in VERSIONS}},
        "playerCommands": True,
        # A /hotpatch/<path> miss under a known branch may be fetched from the official CDN once
        # (GIO_HOTPATCH_UPSTREAM=0 in the environment overrides this to off).
        "hotpatchUpstream": True,
        # Where the mirror is filled FROM: the archive.org bundles (default) or the official CDN,
        # file by file. Seeded from GIO_HOTPATCH_SOURCE; changed from the launcher at any time.
        "hotpatchSource": HOTPATCH_SOURCE,
    }


def merge_policy(current, partial, allowed_templates):
    """current (full) + partial (anything the admin sent) -> a new full policy, validated.
    `allowed_templates` maps version -> template ids the manifest provides ('fresh' is always
    allowed). Pure; raises AgentError(400) on invalid input."""
    if not isinstance(partial, dict):
        raise AgentError(400, "policy must be a JSON object")
    out = json.loads(json.dumps(current))  # deep copy
    if "name" in partial:
        name = partial["name"]
        if name is None:
            name = ""
        if not isinstance(name, str) or len(name) > 64:
            raise AgentError(400, "policy.name must be a string of at most 64 characters")
        out["name"] = name.strip()
    if "playerCommands" in partial:
        if not isinstance(partial["playerCommands"], bool):
            raise AgentError(400, "policy.playerCommands must be true or false")
        out["playerCommands"] = partial["playerCommands"]
    if "hotpatchUpstream" in partial:
        if not isinstance(partial["hotpatchUpstream"], bool):
            raise AgentError(400, "policy.hotpatchUpstream must be true or false")
        out["hotpatchUpstream"] = partial["hotpatchUpstream"]
    if "hotpatchSource" in partial:
        src = partial["hotpatchSource"]
        if not isinstance(src, str) or src.strip().lower() not in HOTPATCH_SOURCES:
            raise AgentError(400, "policy.hotpatchSource must be one of %s" % ", ".join(HOTPATCH_SOURCES))
        out["hotpatchSource"] = src.strip().lower()
    signup = partial.get("signup")
    if signup is not None:
        if not isinstance(signup, dict):
            raise AgentError(400, "policy.signup must be an object")
        if "enabled" in signup:
            if not isinstance(signup["enabled"], bool):
                raise AgentError(400, "policy.signup.enabled must be true or false")
            out["signup"]["enabled"] = signup["enabled"]
        versions = signup.get("versions")
        if versions is not None:
            if not isinstance(versions, dict):
                raise AgentError(400, "policy.signup.versions must be an object")
            for v, vp in versions.items():
                if v not in VERSIONS:
                    raise AgentError(400, "unknown version in policy: %s" % v)
                if not isinstance(vp, dict):
                    raise AgentError(400, "policy.signup.versions.%s must be an object" % v)
                cur = out["signup"]["versions"].setdefault(
                    v, {"templates": ["fresh"], "maxPerDay": 50, "maxPerPlayer": MAX_PER_PLAYER_DEFAULT})
                if "templates" in vp:
                    tpls = vp["templates"]
                    if not isinstance(tpls, list) or not all(isinstance(t, str) for t in tpls):
                        raise AgentError(400, "templates must be a list of ids")
                    ok = set(allowed_templates.get(v) or []) | {"fresh"}
                    bad = [t for t in tpls if t not in ok]
                    if bad:
                        raise AgentError(400, "unknown template(s) for %s: %s" % (v, ", ".join(bad)))
                    seen = []
                    for t in tpls:
                        if t not in seen:
                            seen.append(t)
                    cur["templates"] = seen
                if "maxPerDay" in vp:
                    m = vp["maxPerDay"]
                    if isinstance(m, bool) or not isinstance(m, int) or m < 0 or m > 100000:
                        raise AgentError(400, "maxPerDay must be an integer between 0 and 100000")
                    cur["maxPerDay"] = m
                if "maxPerPlayer" in vp:
                    m = vp["maxPerPlayer"]
                    if isinstance(m, bool) or not isinstance(m, int) or m < 0 or m > MAX_PER_PLAYER_MAX:
                        raise AgentError(400, "maxPerPlayer must be an integer between 0 and %d"
                                         % MAX_PER_PLAYER_MAX)
                    cur["maxPerPlayer"] = m
    return out


def get_policy():
    base = default_policy()
    try:
        saved = load_state().get("policy")
    except StateUnreadable:
        # Fail closed while the admin's choices cannot be read: the admin may have switched signup
        # or player commands OFF, so neither is offered until the file reads again (the public
        # routes read this directly, not only the state_ok-ANDed /public/status); the on-demand
        # upstream fetch is switched off too.
        base["signup"]["enabled"] = False
        base["playerCommands"] = False
        base["hotpatchUpstream"] = False
        return base
    if isinstance(saved, dict):
        allowed = {v: [t["id"] for t in manifest_templates(v)] for v in VERSIONS}
        try:
            return merge_policy(base, saved, allowed)
        except AgentError as first:
            # A manifest that lost a template id (redeployed payloads, a box without the payload
            # folder) must not brick the policy -- but it must not silently REOPEN the server
            # either: the defaults say signup + player commands are ON, so falling back to them
            # would re-enable exactly what the admin switched off. Retry with only the part that
            # cannot be validated dropped (the template lists), and fail closed if even that is
            # refused.
            safe = json.loads(json.dumps(saved))
            for vp in ((safe.get("signup") or {}).get("versions") or {}).values():
                if isinstance(vp, dict):
                    vp.pop("templates", None)
            try:
                out = merge_policy(base, safe, allowed)
                log_line("policy: the stored template list was refused (%s) -- keeping every other "
                         "stored choice and offering 'fresh' only." % first)
                return out
            except AgentError as e:
                log_line("policy: the stored policy cannot be applied (%s) -- signups and player "
                         "commands stay OFF until it is fixed." % e)
                base["signup"]["enabled"] = False
                base["playerCommands"] = False
                return base
    return base


def set_policy(partial):
    allowed = {v: [t["id"] for t in manifest_templates(v)] for v in VERSIONS}
    with _state_lock:
        try:
            data = load_state()
        except StateUnreadable:
            raise state_unreadable_error()
        current = default_policy()
        if isinstance(data.get("policy"), dict):
            try:
                current = merge_policy(current, data["policy"], allowed)
            except AgentError:
                pass
        new = merge_policy(current, partial, allowed)
        data["policy"] = new
        # strict: the save IS the operation -- answering the new policy for a write that failed
        # would show the admin a setting the next agent restart silently takes back.
        save_state(data, strict=True)
    _invalidate_public_status()
    return new


# -- /public/status: a cached, minimal snapshot (never a docker call per request) --

PUBLIC_STATUS_TTL = 10
_public_cache = {"at": 0.0, "data": None, "gen": 0}
_public_lock = threading.Lock()


def _invalidate_public_status():
    # gen: a refresh that was already building when this ran (it read the OLD policy) must not
    # file its snapshot as fresh -- players were shown the previous policy for another 10 s.
    _public_cache["gen"] += 1
    _public_cache["at"] = 0.0


def build_public_status(full, policy):
    """Project the admin status + policy onto what a token-less player may see. Pure."""
    # An unreadable state file: no generation (null = "unknown" to the launcher, never "changed")
    # and no signup, whatever policy object was passed in.
    state_ok = (full.get("state") or {}).get("ok", True)
    out = {"ok": True, "agent": AGENT_VERSION.split(".")[0],
           "name": policy.get("name") or SERVER_NAME,
           "platform": full.get("platform"),
           "versions": {}, "signup": {"enabled": bool(policy["signup"]["enabled"]) and state_ok, "versions": {}},
           "playerCommands": bool(policy.get("playerCommands")) and state_ok}
    # docker compose ls failed / timed out: every up:false below is fabricated. A fixed flag, never the
    # raw error text (docker output stays off the token-less endpoint); the per-version fields keep
    # their shape for older launchers, and present stays true to fact (a filesystem check).
    if full.get("error"):
        out["statusUnknown"] = True
    for v, entry in (full.get("versions") or {}).items():
        if not entry.get("present"):
            out["versions"][v] = {"present": False}
            continue
        out["versions"][v] = {
            "present": True,
            "up": bool(entry.get("up")),
            "healthy": entry.get("healthy") if entry.get("up") else False,
            "defaultAccount": entry.get("account") or "",
            "passwordVerify": bool(entry.get("passwordVerify")),
            "generation": entry.get("provisionedAt"),
        }
        hp = entry.get("hotpatch") or {}
        if hp.get("available"):
            # Enough for the launcher's "official 2021 fixes served here" badge; the revisions are
            # public knowledge (the client sends them to every dispatch), never a path or an IP.
            # "enabled" here means ADVERTISED: an enabled-but-pending mirror (database half owed,
            # or an unusable URL) serves nobody yet and must not be announced to players.
            out["versions"][v]["hotpatch"] = {"enabled": bool(hp.get("enabled")) and not bool(hp.get("pending")),
                                              "res": hp.get("res"), "data": hp.get("data"),
                                              "silence": hp.get("silence"),
                                              # voice languages whose base packs the mirror serves (names)
                                              "voice": list(hp.get("voice") or [])}
        vp = (policy["signup"]["versions"] or {}).get(v)
        if vp is not None and entry.get("present"):
            labels = {t["id"]: t["label"] for t in manifest_templates(v)}
            labels["fresh"] = "New account"
            # agent 3.5: a progress template is offered only once it is imported on THIS stack (the
            # template_ready test, read from the status entry's record so this stays pure) -- one
            # the policy lists but the stack lacks only ever answered template_not_ready on Create.
            recs = entry.get("templates") or {}
            offered = [{"id": t, "label": labels.get(t, t)} for t in vp.get("templates", [])
                       if t == "fresh" or template_record_ready(recs.get(t))]
            # An empty list is left out, never published: launchers read a missing version as "no
            # signup here", while the shipped v1.0 launcher's account card reads templates[0]
            # unguarded (a policy without 'fresh' whose templates are not imported yet, or an
            # explicit `templates: []`, would crash it).
            if offered:
                # maxPerPlayer (agent 3.7): how many accounts one launcher may create on this version
                # -- the launcher shows "2 of 5" from its own list; the agent enforces it.
                out["signup"]["versions"][v] = {"templates": offered, "maxPerDay": vp.get("maxPerDay", 50),
                                                "maxPerPlayer": vp.get("maxPerPlayer", MAX_PER_PLAYER_DEFAULT)}
    return out


def public_status():
    """The cached snapshot. Refreshed lazily, at most every PUBLIC_STATUS_TTL seconds, by one
    caller at a time; everyone else gets the last snapshot (a stale 10 s view beats a docker
    call per request -- /public/status is what every player's launcher polls)."""
    now = time.time()
    data = _public_cache["data"]
    if data is not None and now - _public_cache["at"] < PUBLIC_STATUS_TTL:
        return data
    if not _public_lock.acquire(blocking=data is None):
        return data  # someone else is refreshing; serve the previous snapshot meanwhile
    try:
        if _public_cache["data"] is not None and time.time() - _public_cache["at"] < PUBLIC_STATUS_TTL:
            return _public_cache["data"]
        gen = _public_cache["gen"]
        fresh = build_public_status(status(), get_policy())
        # Invalidated while it was being built: served to this caller, but left stale for the next.
        _public_cache["data"], _public_cache["at"] = fresh, (time.time() if gen == _public_cache["gen"] else 0.0)
        return fresh
    finally:
        _public_lock.release()


# -- rate limiting (token buckets per client IP, LRU-bounded) --

class RateLimiter:
    def __init__(self, cap=10000):
        self._buckets = OrderedDict()
        self._lock = threading.Lock()
        self.cap = cap

    @staticmethod
    def _rule(rule):
        """(key, limit, per[, code]) -> (key, limit, per, code)."""
        return rule[0], rule[1], rule[2], (rule[3] if len(rule) > 3 else None)

    def _level(self, key, limit, per, now):
        """Caller holds the lock. The bucket's tokens refilled up to `now` (a new bucket is full)."""
        tokens, last = self._buckets.get(key, (float(limit), now))
        return min(float(limit), tokens + max(0.0, now - last) * limit / float(per))

    def _evict(self):
        """Caller holds the lock."""
        while len(self._buckets) > self.cap:
            # LRU-evict per-address buckets only: a global quota ("create-d:*") must not be
            # reset by a caller churning through thousands of source addresses.
            victim = next((k for k in self._buckets if "*" not in k), None)
            if victim is None:
                break
            del self._buckets[victim]

    def take(self, key, limit, per, now=None):
        """Take one token from bucket `key` (capacity `limit`, refilled evenly over `per`
        seconds). Returns (allowed, retry_after_seconds). A refused call consumes nothing."""
        ok, wait, _code = self.take_all([(key, limit, per)], now=now)
        return ok, wait

    def take_all(self, rules, now=None):
        """All or nothing over rules = [(key, limit, per[, code]), ...]: when every bucket holds a
        token, take one from each and return (True, 0, None); otherwise take from NONE and return
        (False, wait, code) of the refusing bucket with the longest wait. A drained global bucket
        must not also burn the caller's own per-address tokens on every refused attempt."""
        now = time.time() if now is None else now
        rules = [self._rule(r) for r in rules]
        with self._lock:
            refusal, levels = None, []
            for key, limit, per, code in rules:
                if limit <= 0:
                    wait = int(per)  # a zero quota (maxPerDay=0) never admits anyone
                else:
                    tokens = self._level(key, limit, per, now)
                    levels.append((key, tokens))
                    if tokens >= 1.0:
                        continue
                    wait = max(1, int((1.0 - tokens) * per / float(limit) + 0.999))
                if refusal is None or wait > refusal[0]:
                    refusal = (wait, code)
            if refusal is not None:
                for key, _tokens in levels:
                    if key in self._buckets:
                        self._buckets.move_to_end(key)  # recency only: a refused caller stays tracked
                return False, refusal[0], refusal[1]
            for key, tokens in levels:
                self._buckets.pop(key, None)
                self._buckets[key] = (tokens - 1.0, now)
            self._evict()
            return True, 0, None

    def give(self, rules, now=None):
        """Hand back the tokens a successful take_all(rules) took, for work that then never ran:
        +1 per bucket, never above its limit. A bucket that is gone (LRU-evicted) already starts
        full again -- nothing to give back to."""
        now = time.time() if now is None else now
        with self._lock:
            for key, limit, per, _code in (self._rule(r) for r in rules):
                if limit <= 0 or key not in self._buckets:
                    continue
                self._buckets[key] = (min(float(limit), self._level(key, limit, per, now) + 1.0), now)


LIMITER = RateLimiter()
# The token-less mirror has its own limiter: at 600 hits/min per address it is the cheapest way to
# churn buckets, and it must never share an LRU with the signup quotas.
HOTPATCH_LIMITER = RateLimiter(cap=4096)


def enforce_limits(rules, limiter=None):
    """rules = [(key, limit, per_seconds[, code]), ...], all or nothing (RateLimiter.take_all): a
    refusal spends no bucket and raises 429 with the longest wait and that rule's public code."""
    ok, wait, code = (limiter or LIMITER).take_all(rules)
    if not ok:
        raise AgentError(429, "too many requests, try again later", retry_after=wait, code=code)


# -- player self-service accounts --

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,19}$")
PASSWORD_RE = re.compile(r"^[\x21-\x7e]{8,64}$")
RESERVED_PREFIXES = ("relic", "tpl", "admin", "gm")


def validate_account_name(name, reserved=()):
    """None when the name is acceptable, else (code, reason): a PUBLIC_ERROR_CODES code and a
    short English phrase."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return ("name_invalid",
                "name must be 3-20 characters: letters, digits, '_', '.', '-' (starting with a letter or digit)")
    low = name.lower()
    if any(low.startswith(p) for p in RESERVED_PREFIXES) or low in {str(r).lower() for r in reserved if r}:
        return "name_reserved", "that name is reserved"
    return None


def reserved_names(version):
    man = read_manifest(version) or {}
    return [man.get("account") or ""]


def template_record_ready(rec):
    """One `templates` record ({md5, createdAt, error}) says the database was created and its last
    creation did not fail. Pure: build_public_status applies it to the /status entry's record."""
    return isinstance(rec, dict) and bool(rec.get("createdAt")) and not rec.get("error")


def template_ready(version, template):
    """A progress template database was created on this stack and its last creation did not fail."""
    return template_record_ready((version_state(version).get("templates") or {}).get(template))


def signup_quota_rules(ip, version, vp):
    """The signup quotas one request takes (per address, then server-wide), as enforce_limits rules."""
    return [("create-h:" + ip, 3, 3600, "quota_ip_hour"), ("create-d:" + ip, 5, 86400, "quota_ip_day"),
            ("create-h:*", 20, 3600, "quota_server_hour"),
            ("create-d:*:" + version, max(0, int(vp.get("maxPerDay", 50))), 86400, "quota_server_day")]


def namecheck_rules(ip):
    """The small budget of the pre-quota 'is this name taken' lookup (see Handler._public_create)."""
    return [("namecheck:" + ip, 6, 600, "rate_limited"), ("namecheck:*", 60, 600, "rate_limited_server")]


# -- accounts per launcher (agent 3.7) --
# A player may create up to maxPerPlayer accounts on a version (policy, 5 by default). "A player" is
# the launcher that asks: it sends `client`, a random id of its own, derived per server so two servers
# cannot match one launcher -- there is no per-player authentication on this friends-server API, and a
# new id (a reinstalled launcher) starts from zero, exactly like its own list of accounts does. An
# older launcher sends none and is counted by its address. The names are kept per version in
# state.signupClients, tagged with the generation (provisionedAt) they were created in: a re-provision
# with the default save wipes the accounts, and with them what they counted. The admin's own account
# creation (do_account_create) never counts.

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SIGNUP_CLIENTS_MAX = 20000  # launchers remembered per version; the least recently seen goes first


def signup_client_key(raw, ip):
    """'c:<id>' for a well-formed launcher id, else 'ip:<address>'. Pure."""
    if isinstance(raw, str) and CLIENT_ID_RE.match(raw):
        return "c:" + raw
    return "ip:" + str(ip)


def _signup_gen(data, version):
    return ((data.get("versions") or {}).get(version) or {}).get("provisionedAt")


def signup_book_names(data, version, key):
    """The names `key` created on `version` in the current generation, from a loaded state. Pure."""
    books = data.get("signupClients")
    book = books.get(version) if isinstance(books, dict) else None
    if not isinstance(book, dict) or book.get("gen") != _signup_gen(data, version):
        return []
    names = (book.get("clients") or {}).get(key) if isinstance(book.get("clients"), dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def signup_book_add(data, version, key, name, cap=SIGNUP_CLIENTS_MAX):
    """Count `name` against `key` inside a loaded state (a book of another generation starts over;
    the key moves to the end, and past `cap` keys the least recently used goes). Pure but for
    mutating `data`. Returns the key's names."""
    books = data.get("signupClients")
    if not isinstance(books, dict):
        books = data["signupClients"] = {}
    gen = _signup_gen(data, version)
    book = books.get(version)
    if not isinstance(book, dict) or book.get("gen") != gen or not isinstance(book.get("clients"), dict):
        book = books[version] = {"gen": gen, "clients": {}}
    clients = book["clients"]
    names = clients.pop(key, None)
    names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
    if name.lower() not in {n.lower() for n in names}:
        names.append(name)
    clients[key] = names
    while len(clients) > cap:
        clients.pop(next(iter(clients)))
    return names


def check_player_limit(version, key, vp):
    """403 account_limit once `key` has created the version's maxPerPlayer accounts. The state is
    readable here: an unreadable one already closed signups (get_policy fails closed)."""
    limit = vp.get("maxPerPlayer", MAX_PER_PLAYER_DEFAULT)
    try:
        used = len(signup_book_names(load_state(), version, key))
    except StateUnreadable:
        raise state_unreadable_error()
    if used >= limit:
        raise AgentError(403, "this launcher has already created %d account(s) on version %s -- the "
                              "server allows %d" % (used, version, limit), code="account_limit")
    return used


def record_signup(version, key, name):
    """Count a created account (best effort: one that exists but went uncounted only makes the limit
    looser -- it must never fail the signup that created it)."""
    try:
        with _state_lock:
            data = load_state()
            signup_book_add(data, version, key, name)
            save_state(data)
    except Exception as e:  # noqa: BLE001
        log_line("gio-agent: could not count the signup of '%s' against its launcher (%s) -- the "
                 "per-player limit does not see it." % (name, e))


def generated_password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))


def _signup_rollback(version, job, uid, name=None):
    """Best effort: undo the GAME-side half of a signup whose progress copy failed, so the player is
    left with a clean fresh account instead of a half-written save. The sdk account itself STAYS --
    the agent never writes the stack's sdk.db (the sdk service holds it open, see _sdk_db_lookup), so
    the name remains registered and the player can log in and play; only the chosen progress is
    missing. `name` is the account's exact name, for the closing line only (the admin reads it live
    in the accountcreate job). Nothing here may raise: the caller is already failing and the
    original reason wins."""
    shard = uid % 10
    if getattr(job, "mysql_wait_expired", False):
        # The copy already waited its full budget for this service and it never came back: waiting
        # again would only hold the job slot (and the watchdog that could revive the stack) longer.
        job.log("Rollback: the database service is still down -- uid %d stays as it is." % uid)
        return
    if not mysql_reachable(version):
        # Whatever failed may have taken the database service with it (see copy_save): every DELETE
        # below would fail too, and the log would read as if the rollback itself were broken.
        try:
            wait_for_mysql(version, job, timeout=120)
        except AgentError as e:
            job.log("Rollback: the database service did not come back (%s) -- uid %d stays as it is."
                    % (e.message, uid))
            return
    for table in ("t_player_data_%d", "t_block_data_%d", "t_home_data_%d"):
        try:
            _mysql_q(version, "DELETE FROM %s WHERE uid=%d" % (table % shard, uid))
        except Exception as e:  # noqa: BLE001
            job.log("Rollback: could not clear %s for uid %d (%s)."
                    % (table % shard, uid, _short_err(e)))
    try:
        _mysql_q(version, "DELETE FROM t_player_uid WHERE uid=%d" % uid)
    except Exception as e:  # noqa: BLE001
        job.log("Rollback: could not release uid %d (%s) -- it stays allocated, which only means the "
                "account keeps that uid at first login." % (uid, _short_err(e)))
        return
    job.log("Rolled back: uid %d released and no save written, so %s is a plain fresh account."
            % (uid, ("'%s'" % name) if name else "the account"))


def do_signup(version, job, name, password, template, client_ip, refund=None, client=None):
    """Create a player account and, unless `template` is 'fresh', give it a template's progress.
    Everything is re-checked INSIDE the job (the request-time checks only filter the obvious):
    the stack must be up, the name must still be free, the template must still be allowed, the
    launcher must still be under the version's maxPerPlayer (`client` = its signup_client_key; the
    created account is counted against it the moment it is registered).
    `refund` = the signup quota rules the request took: handed back when the job fails BEFORE the
    registration (nothing was created) for any reason but a taken name -- a refunded 'name taken'
    would turn take + refund into a free name oracle -- or version_down: that probe is a docker exec
    of up to 30 s holding the job slot, so a free one would let failing signups keep admin jobs out
    while MySQL hangs. Never refunded once registration started."""
    job.log("Signup request from %s: name=%s version=%s template=%s" % (client_ip, name, version, template))
    try:
        policy = get_policy()
        vp = (policy["signup"]["versions"] or {}).get(version)
        if not policy["signup"]["enabled"] or vp is None:
            raise AgentError(403, "account creation is disabled on this server", code="signup_disabled")
        if template not in (vp.get("templates") or []):
            raise AgentError(400, "that template is not available on this server", code="template_unavailable")
        if client:
            check_player_limit(version, client, vp)
        why = validate_account_name(name, reserved_names(version))
        if why:
            raise AgentError(400, why[1], code=why[0])
        if not is_bootstrapped(version) or not mysql_reachable(version):
            raise AgentError(503, "this server version is not running right now", code="version_down")
        if _sdk_db_lookup(version, name) is not None:
            raise AgentError(409, "name taken", code="name_taken")
        if template != "fresh" and not template_ready(version, template):
            raise AgentError(503, "that progress template is not ready on this server", code="template_not_ready")
        verify = bool(version_state(version).get("passwordVerify"))
        if verify and not (isinstance(password, str) and PASSWORD_RE.match(password)):
            raise AgentError(400, "a password of 8-64 printable characters is required", code="password_required")
    except Exception as e:
        if refund and not (isinstance(e, AgentError) and e.code in ("name_taken", "version_down")):
            LIMITER.give(refund)
            job.log("Nothing was created -- the signup quota was handed back.")
        raise
    pwd = password if (isinstance(password, str) and PASSWORD_RE.match(password)) else generated_password()
    counted = (lambda exact: record_signup(version, client, exact)) if client else None
    return _register_and_seed(version, job, name, pwd, template, on_registered=counted)


def _register_and_seed(version, job, name, pwd, template, on_registered=None):
    """The half of an account creation that WRITES: register `name` with `pwd` through the sdk,
    then, unless `template` is 'fresh', allocate its game uid and copy the template's progress
    onto it. Shared by the player signup (do_signup) and the admin's creation (do_account_create);
    each caller has done its own checks first. `on_registered(exact_name)` runs as soon as the sdk
    account exists -- before the progress copy, which may fail and still leave the account there.
    Returns {name, uid, template, nickname}."""
    _sdk_register(version, name, job, password=pwd)
    acc = _sdk_db_lookup(version, name)
    if acc is None:
        raise AgentError(500, "the account did not appear after registration")
    exact = acc[1]
    if on_registered is not None:
        on_registered(exact)
    result = {"name": exact, "uid": None, "template": template, "nickname": None}
    if template == "fresh":
        job.log("Fresh account -- the game allocates the uid at first login.")
        return result
    # Past this point the sdk account EXISTS, so a failure may not leave the game database half
    # written: roll the game side back and say plainly that the account is there without the
    # progress. The name cannot be handed back (sdk.db is not ours to write), so it is never
    # reported as free again -- and a signup's quota stays spent, because something WAS created.
    uid = _resolve_player(version, job, exact, create=True)
    try:
        copy_save(version, job, 1, uid, src_db=template_db_name(template))
        nick = _mysql_q(version, "SELECT nickname FROM t_player_data_%d WHERE uid=%d" % (uid % 10, uid))
    except Exception as e:  # noqa: BLE001
        job.log("ERROR: the starting progress was NOT applied to '%s': %s" % (exact, _short_err(e)))
        _signup_rollback(version, job, uid, name=exact)
        raise AgentError(500, "the account was created, but the starting progress could not be "
                              "applied to it", code="progress_failed")
    result.update({"uid": uid, "nickname": nick.strip() or None})
    job.log("Account '%s' (uid %d) created from template '%s'." % (exact, uid, template))
    return result


# -- admin account creation (agent 3.5) --

def admin_create_preflight(b):
    """POST /server/account/create's synchronous refusals, before a job exists. Returns
    (version, name, password or None, template). Every refusal is a 400 with a public code, NEVER a
    404: on this route a 404 must keep meaning "an agent older than 3.5, no such route" to the
    launcher. The name obeys a signup's rules (reserved names and prefixes included); the template
    may be any the manifest ships -- whether it is imported on the stack is the job's 409."""
    version = str(b.get("version", "")).strip()
    check_known(version)
    name = b.get("name")
    name = name.strip() if isinstance(name, str) else ""
    why = validate_account_name(name, reserved_names(version))
    if why:
        raise AgentError(400, why[1], code=why[0])
    template = str(b.get("template") or "fresh").strip() or "fresh"
    if template != "fresh" and template not in {t["id"] for t in manifest_templates(version)}:
        raise AgentError(400, "that template is not available on this server", code="template_unavailable")
    password = b.get("password")
    if password is None or password == "":
        password = None
    elif not (isinstance(password, str) and PASSWORD_RE.match(password)):
        raise AgentError(400, "the password must be 8-64 printable characters", code="password_invalid")
    return version, name, password, template


def do_account_create(version, job, name, password, template):
    """The ADMIN creates a player account -- for themselves or a friend -- starting from 'fresh' or
    any progress template imported on this stack. No signup policy, no LIMITER, no quota: the bearer
    token is the authorisation. Everything is re-checked here, inside the job and under _op_lock
    (the preflight only refused a malformed request). The password never reaches a log line; the
    job result carries it only while the server verifies passwords (the admin must hand it to the
    player) -- admin jobs never enter PUBLIC_JOBS, and public_snapshot whitelists its fields anyway.
    With verification off a typed password is still stored (valid if verification is switched on
    later) but not returned."""
    require_state_readable()
    job.log("Admin account creation: name=%s version=%s template=%s" % (name, version, template))
    if not is_bootstrapped(version):
        raise AgentError(409, "version %s is not installed on this server" % version)
    if not mysql_reachable(version):
        raise AgentError(409, "version %s is not running -- start it first" % version, code="version_down")
    if _sdk_db_lookup(version, name) is not None:
        raise AgentError(409, "name taken", code="name_taken")
    if template != "fresh" and not template_ready(version, template):
        # Only NON-destructive ways out: a `default` prepare would also import the template, but it
        # wipes every player account on the stack.
        raise AgentError(409, "the progress template '%s' is not imported on the %s stack (never imported, "
                              "or its last import failed) -- import it (the launcher's Import button on "
                              "Server status -> Create a player account, or POST /server/templates/ensure; "
                              "nothing is stopped) and retry" % (template, version),
                         code="template_not_ready")
    verify = bool(version_state(version).get("passwordVerify"))
    pwd = password or generated_password()
    result = _register_and_seed(version, job, name, pwd, template)
    result.update(passwordVerify=verify, passwordGenerated=bool(verify and password is None),
                  password=(pwd if verify else None))
    return result


# -- secrets (stack passwords and the MUIP sign key) --

SECRET_KNOBS = ("mysqlRoot", "flask", "internal", "muip")
SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# GIO_MUIP_KEY obeys the same rule as a key set through /server/secrets. A bad value is dropped with
# a WARNING (a random key applies), never a refusal to boot: systemd's Restart=always would loop.
if MUIP_KEY and not SECRET_VALUE_RE.match(MUIP_KEY):
    log_line("gio-agent: WARNING: GIO_MUIP_KEY ignored -- it must be 8-128 characters (letters, digits, "
             "'_' or '-'); a random key will be used.")
    MUIP_KEY = ""


def replace_sql_identified_by(text, old, new):
    return re.subn(r"(IDENTIFIED BY ')%s(')" % re.escape(old), r"\g<1>%s\g<2>" % new, text)


def replace_requirepass(text, old, new):
    return re.subn(r"(--requirepass[ \t]+)%s(?=[ \t\r\n]|$)" % re.escape(old), r"\g<1>%s" % new, text)


def replace_sh_password(text, old, new):
    # prepare-db.sh: `mysql ... -u hk4e_work -p<literal> < data.sql`
    return re.subn(r"((?<=\s)-p)%s(?=[ \t\r\n]|$)" % re.escape(old), r"\g<1>%s" % new, text)


def replace_xml_pwd(text, old, new):
    return re.subn(r'(\bpwd=")%s(")' % re.escape(old), r"\g<1>%s\g<2>" % new, text)


def replace_sign_key(text, new):
    return re.subn(r'(\bsign_key=")[^"]*(")', r"\g<1>%s\g<2>" % new, text)


def _rewrite(path, fn):
    """Apply fn(text) -> (new_text, n) to a file in place (line endings preserved). Returns n; a
    missing file counts as 0."""
    if not os.path.isfile(path):
        return 0
    text = read_text(path)
    new, n = fn(text)
    if n and new != text:
        write_text_atomic(path, new)
    return n


# -- pathfinding server on/off: the compose profile `donotstart` on pathfindingserver (agent 3.4) --
# Pure text editors over a compose file, in the vendor's own syntax (oaserver ships
# "    profiles:\n      - donotstart" under a 2-space service key): the key line's indent decides
# everything, line endings are the file's, the block form is inserted/removed byte-exact so that
# disable -> enable round-trips to the original bytes, and no other service block is ever touched.
# The inline form (`profiles: [donotstart]`) is understood when READING and appended to when it is
# what the file already uses.

def _line_indent(line):
    return line[:len(line) - len(line.lstrip(" \t"))]


def _compose_services_indent(text):
    """The indent of the service keys under the top-level `services:` (None when there is no such
    section, or it has no children)."""
    m = re.search(r"^services:[ \t]*(?:#[^\r\n]*)?\r?$", text, re.M)
    if not m:
        return None
    for line in text[m.end():].splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ind = _line_indent(line)
        return ind or None
    return None


def _service_block(text, service):
    """(start, end, indent, child_indent, eol) of the `<indent>service:` block, or None when the
    service is absent. Under a `services:` section only a key at the services' indent counts (a
    same-named key nested under another service's depends_on/networks never does); the block runs
    to the next non-blank, non-comment line indented no deeper than the key."""
    want = _compose_services_indent(text)
    best = None
    for m in re.finditer(r"^(?P<i>[ \t]+)%s:[ \t]*(?:#[^\r\n]*)?\r?$" % re.escape(service), text, re.M):
        if want is not None:
            if m.group("i") == want:
                best = m
                break
            continue
        if best is None or len(m.group("i")) < len(best.group("i")):
            best = m  # no services: section (a fragment): the shallowest occurrence
    if best is None:
        return None
    indent = best.group("i")
    eol = "\r\n" if "\r\n" in text else "\n"
    nl = text.find("\n", best.end())
    i = len(text) if nl < 0 else nl + 1
    end, child_indent = len(text), None
    while i < len(text):
        j = text.find("\n", i)
        nxt = len(text) if j < 0 else j + 1
        line = text[i:nxt]
        s = line.strip()
        if s and not s.startswith("#"):
            ind = _line_indent(line)
            if len(ind) <= len(indent):
                end = i
                break
            if child_indent is None:
                child_indent = ind
        i = nxt
    return best.start(), end, indent, child_indent or (indent + "  "), eol


_PROFILE_ITEM_RE = re.compile(r"^(?P<ii>[ \t]+)-[ \t]*(?P<q>[\"']?)(?P<n>[^\"'\r\n#]*?)(?P=q)[ \t]*(?:#[^\r\n]*)?\r?$")


def _profiles_section(body, ci):
    """Inside one service block: its `profiles:` key at the child indent -- {"start", "end" (the
    char span of the key line plus its list items), "inline", "names", "items": [(s, e, name)]} --
    or None. The block-form items are the consecutive `- name` lines right under the key."""
    m = re.search(r"^%sprofiles:[ \t]*(?P<rest>[^\r\n]*?)[ \t]*\r?$" % re.escape(ci), body, re.M)
    if not m:
        return None
    nl = body.find("\n", m.end())
    line_end = len(body) if nl < 0 else nl + 1
    rest = m.group("rest")
    if rest and not rest.startswith("#"):
        names = []
        if rest.startswith("[") and rest.endswith("]"):
            names = [x.strip().strip("\"'") for x in rest[1:-1].split(",") if x.strip()]
        return {"start": m.start(), "end": line_end, "inline": True, "names": names, "items": []}
    items = []
    i = line_end
    while i < len(body):
        j = body.find("\n", i)
        nxt = len(body) if j < 0 else j + 1
        mi = _PROFILE_ITEM_RE.match(body[i:nxt])
        if not mi or len(mi.group("ii")) <= len(ci):
            break
        items.append((i, nxt, mi.group("n").strip()))
        i = nxt
    return {"start": m.start(), "end": items[-1][1] if items else line_end, "inline": False,
            "names": [n for _s, _e, n in items], "items": items}


def compose_service_profiled(text, service, profile=DONOTSTART_PROFILE):
    """True / False whether the service's block carries `profile`; None when there is no such service."""
    blk = _service_block(text, service)
    if blk is None:
        return None
    start, end, _indent, ci, _eol = blk
    sec = _profiles_section(text[start:end], ci)
    return bool(sec and profile in sec["names"])


def set_pathfinding_text(text, enabled):
    """(new_text, n): n = 1 when the pathfindingserver block changed, 0 when it is already in the
    wanted state or absent. Disable inserts exactly `<ci>profiles:<eol><ci>  - donotstart<eol>` right
    under the service key (or appends the item to an existing list); enable removes that item and the
    key it leaves empty. Everything else stays byte-identical."""
    blk = _service_block(text, PATHFINDING_SERVICE)
    if blk is None:
        return text, 0
    start, end, _indent, ci, eol = blk
    body = text[start:end]
    sec = _profiles_section(body, ci)
    have = bool(sec and DONOTSTART_PROFILE in sec["names"])
    if have == (not enabled):
        return text, 0
    if not enabled:
        if sec is None:
            nl = body.find("\n")
            key_end = len(body) if nl < 0 else nl + 1
            ins = "%sprofiles:%s%s  - %s%s" % (ci, eol, ci, DONOTSTART_PROFILE, eol)
            if nl < 0:
                ins = eol + ins[:-len(eol)]  # the key line ends the file without a newline: keep that
            new_body = body[:key_end] + ins + body[key_end:]
        elif sec["inline"]:
            line = body[sec["start"]:sec["end"]]
            new_line = line.replace("]", (", %s]" if sec["names"] else "%s]") % DONOTSTART_PROFILE, 1)
            new_body = body[:sec["start"]] + new_line + body[sec["end"]:]
        else:
            ii = _line_indent(body[sec["items"][0][0]:sec["items"][0][1]]) if sec["items"] else ci + "  "
            new_body = body[:sec["end"]] + "%s- %s%s" % (ii, DONOTSTART_PROFILE, eol) + body[sec["end"]:]
    elif sec["inline"]:
        names = [n for n in sec["names"] if n != DONOTSTART_PROFILE]
        if names:
            line = body[sec["start"]:sec["end"]]
            new_line = re.sub(r"\[[^\]]*\]", "[" + ", ".join(names) + "]", line, count=1)
            new_body = body[:sec["start"]] + new_line + body[sec["end"]:]
        else:
            new_body = body[:sec["start"]] + body[sec["end"]:]
    elif any(n != DONOTSTART_PROFILE for _s, _e, n in sec["items"]):
        new_body = body
        for s, e, n in reversed(sec["items"]):
            if n == DONOTSTART_PROFILE:
                new_body = new_body[:s] + new_body[e:]
    else:
        new_body = body[:sec["start"]] + body[sec["end"]:]  # the item and the key it leaves empty
    return text[:start] + new_body + text[end:], 1


def _compose_service_names(text):
    """The keys at the services' indent (with no `services:` section: the shallowest indented keys)."""
    want = _compose_services_indent(text)
    found = [(m.group("i"), m.group("n")) for m in
             re.finditer(r"^(?P<i>[ \t]+)(?P<n>[A-Za-z0-9_.-]+):[ \t]*(?:#[^\r\n]*)?\r?$", text, re.M)]
    if want is None and found:
        want = min((ind for ind, _n in found), key=len)
    names = []
    for ind, n in found:
        if ind == want and n not in names:
            names.append(n)
    return names


def compose_disabled_services(text):
    """Every service whose block carries the donotstart profile (the vendor's oaserver, a switched-off
    pathfindingserver): what compose's own `config --services` leaves out."""
    return {n for n in _compose_service_names(text) if compose_service_profiled(text, n)}


def muip_key_file(version):
    d = stack_dir(version)
    tmpl = os.path.join(d, "server", "muipserver", "conf", "muipserver.xml.tmpl")
    return tmpl if os.path.isfile(tmpl) else os.path.join(d, "server", "muipserver", "conf", "muipserver.xml")


def _read_sign_key_file(path):
    try:
        m = re.search(r'sign_key\s*=\s*"([^"]*)"', read_text(path))
    except OSError:
        return ""
    return m.group(1) if m else ""


def is_vendor_muip_key(key):
    """The archive's published key -- or none at all: both mean anyone can sign GM commands."""
    return (not key) or key in VENDOR_MUIP_KEYS


def random_muip_key():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))


def muip_fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()[:8] if key else None


def ensure_muip_key(version, job):
    """Replace the vendor's published sign_key (or a missing one) in muipserver.xml.tmpl + the
    rendered muipserver.xml with GIO_MUIP_KEY or a random key (agent 3.4). A key that is not a
    vendor one is never touched (chosen here earlier, or rotated through /server/secrets). The
    value goes to creds.txt on the box and into the state as secrets.muipChangedAt/muipSource;
    the log line carries the fingerprint only. Returns "config" | "random" | None (nothing done)."""
    key_file = muip_key_file(version)
    cur = _read_sign_key_file(key_file)
    if not is_vendor_muip_key(cur):
        return None
    source = "config" if MUIP_KEY else "random"
    new = MUIP_KEY or random_muip_key()
    if new == cur:
        return None
    if _rewrite(key_file, lambda t: replace_sign_key(t, new)) == 0:
        # A stack the agent cannot secure must not be prepared: the bootstrap would render the
        # published key (or nothing) into a muipserver that accepts anyone's commands.
        raise AgentError(500, "sign_key not found in muipserver.xml.tmpl -- the vendor's MUIP key "
                              "could not be replaced. Check %s." % key_file)
    rendered = os.path.join(stack_dir(version), "server", "muipserver", "conf", "muipserver.xml")
    if os.path.isfile(rendered) and rendered != key_file:
        _rewrite(rendered, lambda t: replace_sign_key(t, new))
    env = read_env(version)  # tolerant: {} when unreadable -- creds.txt is a note, not a source
    write_creds_file(version, {"mysqlRoot": env.get("MYSQL_ROOT_PASSWORD", ""),
                               "flask": env.get("FLASK_SECRET_KEY", ""),
                               "internal": internal_literal(version), "muip": new})
    sec = dict(version_state(version).get("secrets") or {})
    sec["muipChangedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    sec["muipSource"] = source
    version_state_set(version, secrets=sec)
    job.log("MUIP sign key: the vendor's published key replaced with a %s one (fingerprint %s) -- only "
            "this agent can sign GM commands from now on; the value is in creds.txt on the box."
            % (source, muip_fingerprint(new)))
    return source


def internal_literal(version):
    """The one password shared by redis requirepass, hk4e_work and hk4e_readonly -- read from the
    compose template, falling back to bootstrap.sql."""
    d = stack_dir(version)
    for path, rx in ((os.path.join(d, "docker-compose.yml.tmpl"), r"--requirepass[ \t]+(\S+)"),
                     (os.path.join(d, "docker-compose.yml"), r"--requirepass[ \t]+(\S+)"),
                     (os.path.join(d, "bootstrap.sql"), r"IDENTIFIED BY '([^']+)'")):
        try:
            m = re.search(rx, read_text(path))
        except OSError:
            continue
        if m:
            return m.group(1)
    return ""


def secrets_info(version):
    """What GET /server/secrets answers. The MUIP key itself never leaves the box: only whether it
    is set, a short fingerprint and when the agent last changed it."""
    check_known(version)
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server." % version)
    env = read_env(version)
    key = _read_sign_key_file(muip_key_file(version))
    try:
        # Readable over a broken state file on purpose: the current passwords are what an admin
        # needs to repair things by hand. Only the agent's own records become unknown.
        vs = (load_state().get("versions") or {}).get(version) or {}
    except StateUnreadable:
        vs = {}
    sec = vs.get("secrets") or {}
    return {
        "mysqlRoot": env.get("MYSQL_ROOT_PASSWORD", ""),
        "flask": env.get("FLASK_SECRET_KEY", ""),
        "internal": internal_literal(version),
        "muip": {"set": bool(key),
                 "fingerprint": muip_fingerprint(key),
                 "changedAt": sec.get("muipChangedAt"),
                 # agent 3.4: still the archive's published key? and who chose the current one
                 # (config = GIO_MUIP_KEY, random = the agent, admin = /server/secrets)
                 "vendorDefault": is_vendor_muip_key(key) if key else None,
                 "source": sec.get("muipSource")},
        "bootstrapped": is_bootstrapped(version),
        "changedAt": sec.get("changedAt"),
        "pending": vs.get("secretsPending"),
    }


def write_creds_file(version, values):
    """creds.txt is the vendor's plaintext note of every password; rewrite it so it never holds a
    stale copy, mark it as managed, keep it private."""
    path = os.path.join(stack_dir(version), "creds.txt")
    nl = "\n"
    try:
        if "\r\n" in read_text(path):
            nl = "\r\n"
    except OSError:
        pass
    lines = ["# Managed by the Relic GIO agent -- rewritten whenever a secret changes.",
             "mysql/root: %s" % values.get("mysqlRoot", ""),
             "Flask: %s" % values.get("flask", ""),
             "MUIP_KEY: %s" % values.get("muip", ""),
             "h4ke pass: %s" % values.get("internal", "")]
    write_text(path, nl.join(lines) + nl)
    protect_path(path, 0o600)


def do_secrets(version, job, values):
    """Rotate any of the four secrets. On a stack that is not bootstrapped yet only the SOURCES are
    edited (the bootstrap renders them). On a bootstrapped stack the MySQL users are altered with
    the OLD root password BEFORE .env changes, then the sources are edited, the configs re-rendered,
    the advertised IP re-applied (the render reverts the XML IP pass) and the stack restarted
    (players are dropped). A `secretsPending` marker in state tells a diagnosing admin where a
    run that died in the middle got to."""
    require_state_readable()
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server." % version)
    d = stack_dir(version)
    wanted = {}
    for k in SECRET_KNOBS:
        v = values.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, str) or not SECRET_VALUE_RE.match(v):
            raise AgentError(400, "%s must be 8-128 characters: letters, digits, '_' or '-'." % k)
        wanted[k] = v
    if not wanted:
        raise AgentError(400, "Nothing to change (accepted keys: %s)." % ", ".join(SECRET_KNOBS))
    env = read_env(version)
    old_root = env.get("MYSQL_ROOT_PASSWORD", "")
    old_internal = internal_literal(version)
    old_muip = _read_sign_key_file(muip_key_file(version))
    if wanted.get("mysqlRoot") == old_root:
        wanted.pop("mysqlRoot")
    if wanted.get("flask") == env.get("FLASK_SECRET_KEY", ""):
        wanted.pop("flask")
    if wanted.get("internal") == old_internal:
        wanted.pop("internal")
    if wanted.get("muip") == old_muip:
        wanted.pop("muip")
    if not wanted:
        job.log("Every requested value is already in place -- nothing to do.")
        return {"changed": []}
    if "internal" in wanted and not old_internal:
        raise AgentError(500, "Cannot find the current shared password (requirepass in "
                              "docker-compose.yml.tmpl / IDENTIFIED BY in bootstrap.sql) -- the "
                              "stack layout is not the one this agent knows.")
    bootstrapped = is_bootstrapped(version)
    was_up = False
    started_db = False
    job.log("Changing: %s (stack %s)." % (", ".join(sorted(wanted)),
                                           "bootstrapped" if bootstrapped else "not bootstrapped yet"))
    if bootstrapped:
        projects, ls_err = running_projects()
        if ls_err is not None:
            raise AgentError(500, "Cannot read the docker state (%s) -- retry." % ls_err)
        was_up = VERSIONS[version]["project"] in (projects or [])
        # strict, both: the admin's only recovery path for a run that dies in the middle is this
        # marker -- 507 before the down / ALTER USER when it cannot be recorded.
        version_state_set(version, strict=True,
                          secretsPending={"startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "knobs": sorted(wanted), "stage": "start"})
        version_state_set(version, strict=True, runningOk=False)
        if "mysqlRoot" in wanted or "internal" in wanted:
            if not was_up:
                down_all(job, keep=version)
                job.log("Starting mysql to alter the database users...")
                rc = _compose_stream(d, job, "up", "-d", "mysql", "redis", prefix="  ")
                if rc != 0:
                    raise AgentError(500, "Could not start mysql (code %d)." % rc)
                started_db = True
            wait_for_mysql(version, job)
            stmts = []
            if "mysqlRoot" in wanted:
                stmts += ["ALTER USER IF EXISTS 'root'@'%%' IDENTIFIED BY '%s'" % wanted["mysqlRoot"],
                          "ALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED BY '%s'" % wanted["mysqlRoot"]]
            if "internal" in wanted:
                stmts += ["ALTER USER IF EXISTS 'hk4e_work'@'172.10.%%' IDENTIFIED BY '%s'" % wanted["internal"],
                          "ALTER USER IF EXISTS 'hk4e_readonly'@'172.10.%%' IDENTIFIED BY '%s'" % wanted["internal"]]
            stmts.append("FLUSH PRIVILEGES")
            job.log("Altering the MySQL users with the current root password...")
            for s in stmts:
                r = mysql_exec(version, s, password=old_root)
                if r.returncode != 0:
                    raise AgentError(500, "ALTER USER failed: %s" % _cmd_err(r, 300))
                job.log("  ok: " + s.split(" IDENTIFIED")[0])
            if "mysqlRoot" in wanted:
                if mysql_exec(version, "SELECT 1", password=wanted["mysqlRoot"]).returncode != 0:
                    raise AgentError(500, "The new root password does not work after ALTER USER -- "
                                          ".env was NOT changed; the old password still applies.")
                job.log("New root password verified.")
            # strict: 507 before .env changes -- "start" on record would claim the users were not
            # altered yet, while the old root password no longer works.
            version_state_set(version, strict=True,
                              secretsPending={"startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                                              "knobs": sorted(wanted), "stage": "users-altered"})

    changed = []
    if "mysqlRoot" in wanted:
        write_env_value(version, "MYSQL_ROOT_PASSWORD", wanted["mysqlRoot"])
        changed.append("mysqlRoot")
        job.log(".env MYSQL_ROOT_PASSWORD updated.")
    if "flask" in wanted:
        write_env_value(version, "FLASK_SECRET_KEY", wanted["flask"])
        changed.append("flask")
        job.log(".env FLASK_SECRET_KEY updated.")
    if "internal" in wanted:
        new = wanted["internal"]
        total = 0
        total += _rewrite(os.path.join(d, "bootstrap.sql"),
                          lambda t: replace_sql_identified_by(t, old_internal, new))
        total += _rewrite(os.path.join(d, "docker-compose.yml.tmpl"),
                          lambda t: replace_requirepass(t, old_internal, new))
        total += _rewrite(os.path.join(d, "dockerfiles", "prepare-db", "prepare-db.sh"),
                          lambda t: replace_sh_password(t, old_internal, new))
        xml_n = 0
        for tmpl in stack_templates(version):
            if tmpl.endswith(".xml.tmpl"):
                xml_n += _rewrite(tmpl, lambda t: replace_xml_pwd(t, old_internal, new))
        job.log("Shared password replaced: %d in bootstrap.sql/compose/prepare-db, %d pwd=\"...\" in "
                "the server XML templates." % (total, xml_n))
        if total == 0 and xml_n == 0:
            raise AgentError(500, "The current shared password was not found in any source file -- "
                                  "nothing replaced.")
        changed.append("internal")
    if "muip" in wanted:
        n = _rewrite(muip_key_file(version), lambda t: replace_sign_key(t, wanted["muip"]))
        if n == 0:
            raise AgentError(500, "sign_key not found in muipserver.xml.tmpl -- the MUIP key was not changed.")
        rendered = os.path.join(d, "server", "muipserver", "conf", "muipserver.xml")
        if os.path.isfile(rendered):
            _rewrite(rendered, lambda t: replace_sign_key(t, wanted["muip"]))
        changed.append("muip")
        job.log("MUIP sign key replaced (fingerprint %s)." % hashlib.sha256(wanted["muip"].encode()).hexdigest()[:8])
    if bootstrapped:
        job.log("Re-rendering the stack configs from the templates...")
        render_stack_configs(version, job)
        # The render reverts the XML half of the advertised-IP pass -- 4206 for everyone outside
        # the LAN otherwise.
        apply_advertised_ip(version, job, with_sql=False)
    final = {
        "mysqlRoot": wanted.get("mysqlRoot", old_root),
        "flask": wanted.get("flask", env.get("FLASK_SECRET_KEY", "")),
        "internal": wanted.get("internal", old_internal),
        "muip": wanted.get("muip", old_muip),
    }
    write_creds_file(version, final)
    job.log("creds.txt rewritten (private).")
    sec = dict(version_state(version).get("secrets") or {})
    sec["changedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if "muip" in wanted:
        sec["muipChangedAt"] = sec["changedAt"]
        sec["muipSource"] = "admin"
    version_state_set(version, secrets=sec)
    if bootstrapped:
        version_state_set(version, secretsPending={"startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   "knobs": sorted(wanted), "stage": "restart"})
        if was_up:
            job.log("Restarting the stack so every service picks the new secrets up (players are "
                    "dropped)...")
            rc = _compose_stream(d, job, "down", "--remove-orphans", prefix="  ")
            if rc != 0:
                raise AgentError(500, "docker compose down failed (code %d)." % rc)
            rc = _compose_stream(d, job, "up", "-d", "mysql", "redis", prefix="  ")
            if rc != 0:
                raise AgentError(500, "docker compose up mysql/redis failed (code %d)." % rc)
            wait_for_mysql(version, job)
            rc = _compose_stream(d, job, "up", "-d", prefix="  ")
            if rc != 0:
                raise AgentError(500, "docker compose up failed (code %d)." % rc)
            verify_stack(version, job)
            version_state_set(version, runningOk=True)
        elif started_db:
            job.log("Stopping mysql again (the stack was down before)...")
            _compose_stream(d, job, "down", "--remove-orphans", prefix="  ")
        version_state_set(version, secretsPending=None)
    return {"changed": changed}


# -- in-game password verification + account passwords --

PW_VERIFY_RE = re.compile(r'("enable_password_verify"\s*:\s*)(true|false)')
BCRYPT_RE = re.compile(r"^\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}$")
# Runs INSIDE the sdk container (bcrypt is in its image, never on the host): same formula as the
# vendor's utils.password_hash -- bcrypt over the sha256 hexdigest.
BCRYPT_SCRIPT = ("import sys, hashlib, bcrypt\n"
                 "pw = sys.stdin.read()\n"
                 "if pw.endswith('\\n'): pw = pw[:-1]\n"
                 "h = bcrypt.hashpw(hashlib.sha256(pw.encode()).hexdigest().encode(), bcrypt.gensalt(12))\n"
                 "sys.stdout.write(h.decode())\n")


def set_password_verify_text(text, on):
    """Flip the one boolean in config.json(.tmpl) with a regex -- the file is never re-serialised
    (it carries RSA keys and the vendor's formatting). Returns (new_text, n)."""
    return PW_VERIFY_RE.subn(r"\g<1>%s" % ("true" if on else "false"), text)


def sdk_password_verify_setting(version):
    """What sdk/data/config.json.tmpl (the file do_auth rewrites) says: True/False, None when the
    stack has no such file or flag."""
    if not is_configured(version):
        return None
    try:
        m = PW_VERIFY_RE.search(read_text(os.path.join(stack_dir(version), "sdk", "data", "config.json.tmpl")))
    except (OSError, ValueError):  # ValueError: not UTF-8
        return None
    return None if m is None else m.group(2) == "true"


def reconcile_password_verify(data):
    """A state rebuilt without the last writes (reset, .bak restore): take passwordVerify from the
    sdk config of every configured stack. Mutates `data`; returns the "<version>=<bool>" changes."""
    if not isinstance(data.get("versions"), dict):
        data["versions"] = {}
    changed = []
    for v in configured_versions():
        on = sdk_password_verify_setting(v)
        vs = data["versions"].get(v)
        if on is None or bool((vs or {}).get("passwordVerify")) == on:
            continue
        data["versions"].setdefault(v, {})["passwordVerify"] = on
        changed.append("%s=%s" % (v, "true" if on else "false"))
    if not data["versions"]:
        del data["versions"]
    return changed


def _manifest_password(version):
    """The manifest's password when it is a real one (the 2.8 payload says 'any')."""
    pw = (read_manifest(version) or {}).get("password") or ""
    return pw if PASSWORD_RE.match(pw) else ""


def bcrypt_hash_in_sdk(version, password):
    d = stack_dir(version)
    r = _run(["docker", "compose", "--project-directory", d, "exec", "-T", "sdk", "python", "-c",
              BCRYPT_SCRIPT], input_text=password + "\n", timeout=120)
    if r.returncode != 0:
        raise AgentError(503, "Could not compute the password hash inside the sdk container (is the "
                              "stack up?): %s" % _cmd_err(r, 200))
    h = (r.stdout or "").strip()
    if not BCRYPT_RE.match(h):
        raise AgentError(500, "Unexpected bcrypt output from the sdk container.")
    return h.encode("ascii")


def sdk_store_password(version, name, hash_bytes):
    """UPDATE accounts SET password=<BLOB> on the live sdk.db (sqlite locks the write; the sdk
    calls bcrypt.checkpw(bytes, hashed), so the hash MUST be stored as bytes). Returns the exact
    account name."""
    acc = _sdk_db_lookup(version, name)
    if acc is None:
        raise AgentError(404, "Account '%s' does not exist on the server." % name)
    exact = acc[1]
    con = sqlite3.connect(_sdk_db_path(version), timeout=10)
    try:
        cur = con.execute("UPDATE accounts SET password = ? WHERE name = ?", (hash_bytes, exact))
        con.commit()
        if cur.rowcount != 1:
            raise AgentError(500, "The password row for '%s' was not updated." % exact)
    finally:
        con.close()
    return exact


def set_account_password(version, job, name, password):
    if not (isinstance(password, str) and PASSWORD_RE.match(password)):
        raise AgentError(400, "The password must be 8-64 printable characters.")
    if VERSIONS[version]["project"] not in (running_projects()[0] or []):
        raise AgentError(409, "Version %s is not running -- the password hash is computed inside the "
                              "sdk container." % version)
    h = bcrypt_hash_in_sdk(version, password)
    exact = sdk_store_password(version, name, h)
    job.log("Password set for account '%s'." % exact)
    return exact


def do_account_password(version, job, name, password):
    if not is_bootstrapped(version):
        raise AgentError(409, "Version %s is not installed on the server." % version)
    exact = set_account_password(version, job, str(name).strip(), password)
    return {"name": exact}


def apply_pending_password(version, job):
    """A verify-ON switch done while the stack was down deferred the default account's password
    to the next start -- set it now (never fatal)."""
    vs = version_state(version)
    if not vs.get("passwordPending"):
        return
    if not vs.get("defaultAccount"):
        # A keep/fixes stack (or one bootstrapped afresh) has no pre-made account: without this the
        # sdk answers "does not exist" at every start and the pending flag never clears.
        job.log("No pre-made account on this server -- nothing to set.")
        version_state_set(version, passwordPending=False)
        return
    acc = (read_manifest(version) or {}).get("account")
    pw = vs.get("defaultPassword") or _manifest_password(version)
    if not acc or not pw:
        version_state_set(version, passwordPending=False)
        return
    try:
        set_account_password(version, job, acc, pw)
        version_state_set(version, passwordPending=False)
    except AgentError as e:
        job.log("WARNING: could not set the default account's password (%s) -- will retry at the "
                "next start." % e.message)


def do_auth(version, job, verify, default_password=None):
    """Turn in-game password verification on/off: edit the sdk config template AND the rendered
    file (the sdk re-reads its config per request, no restart needed), persist the choice and,
    when turning it on, set the default account's password so the shipped account stays usable."""
    require_state_readable()
    if not is_present(version):
        raise AgentError(404, "Version %s does not exist on this server." % version)
    if default_password is not None and default_password != "" and \
            not (isinstance(default_password, str) and PASSWORD_RE.match(default_password)):
        raise AgentError(400, "defaultPassword must be 8-64 printable characters.")
    d = stack_dir(version)
    tmpl = os.path.join(d, "sdk", "data", "config.json.tmpl")
    rendered = os.path.join(d, "sdk", "data", "config.json")
    if not os.path.isfile(tmpl):
        raise AgentError(500, "sdk/data/config.json.tmpl is missing in %s." % d)
    n = _rewrite(tmpl, lambda t: set_password_verify_text(t, verify))
    if n == 0:
        raise AgentError(500, "enable_password_verify not found in sdk/data/config.json.tmpl.")
    if os.path.isfile(rendered):
        _rewrite(rendered, lambda t: set_password_verify_text(t, verify))
    job.log("enable_password_verify = %s written to the sdk config%s."
            % ("true" if verify else "false", " (template + rendered)" if os.path.isfile(rendered) else " (template)"))
    st = {"passwordVerify": bool(verify)}
    if default_password:
        st["defaultPassword"] = default_password
    version_state_set(version, **st)
    result = {"passwordVerify": bool(verify), "defaultPasswordSet": False}
    if verify:
        acc = (read_manifest(version) or {}).get("account")
        pw = default_password or version_state(version).get("defaultPassword") or _manifest_password(version)
        if not version_state(version).get("defaultAccount"):
            # keep/fixes stack: the manifest names an account this database does not hold
            job.log("No pre-made account on this server -- nothing to set.")
            version_state_set(version, passwordPending=False)
        elif not acc:
            job.log("No default account in the manifest -- nothing to set.")
        elif not pw:
            job.log("No default password known for '%s' -- it keeps its current one (set one with "
                    "the account-password action)." % acc)
        elif VERSIONS[version]["project"] in (running_projects()[0] or []):
            try:
                set_account_password(version, job, acc, pw)
                version_state_set(version, passwordPending=False)
                result["defaultPasswordSet"] = True
            except AgentError as e:
                version_state_set(version, passwordPending=True)
                job.log("WARNING: %s -- the password will be set at the next start." % e.message)
        else:
            version_state_set(version, passwordPending=True)
            job.log("The stack is down -- the default account's password will be set at the next start.")
    else:
        version_state_set(version, passwordPending=False)
    _invalidate_public_status()
    return result


# -- agent settings (agent 3.6): GET/POST /agent/config, POST /agent/restart --
# An allowlist of the GIO_* keys the launcher's "Agent settings" card may change. POST /agent/config
# writes them into the file this agent was started from (CONFIG_PATH: --config, else the systemd
# EnvironmentFile) and applies the `live` ones to the running process at once -- the code reads those
# globals at every use --; a `restart` one is read once at start and waits for POST /agent/restart.
# What identifies the box or guards it (token, listen address, MUIP key, state / payload paths, region)
# is never editable here, and a stack folder moves only through POST /server/relocate. A key the
# process environment sets to something else than the file (an exported variable, another EnvironmentFile=
# of a systemd drop-in -- env_overridden) is shown but refused: editing the file would change nothing.
# The spike reads the tuple below with a regex (every key needs agentCfg.key.<KEY> +
# agentCfg.key.<KEY>.sub in en.json): keep it a literal on consecutive lines, with no parenthesis inside.
AGENT_SETTING_KEYS = (
    "GIO_SERVER_NAME", "GIO_PROVISION_MODE", "GIO_TXT_FIXES_MODE", "GIO_PATHFINDING",
    "GIO_BIND_IP", "GIO_ADVERTISED_IP", "GIO_ADVERTISED_HOST", "GIO_ADVERTISED_ALLOW_PRIVATE",
    "GIO_ADVERTISED_CHECK", "GIO_MUIP_HOST", "GIO_HOTPATCH_URL", "GIO_TRUST_PROXY",
    "GIO_HOTPATCH_DIR", "GIO_HOTPATCH_BUNDLE_DIR", "GIO_7Z",
    "GIO_FETCH_DISK_RESERVE", "GIO_HOTPATCH_DISK_RESERVE", "GIO_HOTPATCH_ONDEMAND_MAX",
    "GIO_HOTPATCH_UPSTREAM", "GIO_HOTPATCH_BUNDLES", "GIO_TLS_PINNED_FALLBACK", "GIO_CA_FILE",
)
SETTING_MIB_MAX = 10485760       # 10 TiB -- any reserve / budget beyond it is a typo
SETTING_SECONDS = (30, 86400)    # GIO_ADVERTISED_CHECK: the watchdog's floor .. once a day
SETTING_TEXT_MAX = 64
# A path or URL value (and a relocation folder) longer than this is a paste accident: Windows refuses an
# environment variable over 32767 characters and Linux an argument over 128 KiB (systemd's execve).
SETTING_PATH_MAX = 4096
# Line breaks beyond the C0 controls: str.splitlines() breaks at them, systemd / bash / the launcher do not.
_UNICODE_LINE_BREAKS = "\x85  "


def plain_line_problem(v):
    """Why v cannot be stored as one KEY=value line of the config file -- None when it can. A control
    character (C0, DEL, C1 -- NEL among them), a Unicode line or paragraph separator (every character
    str.splitlines() breaks at is one of these), or a lone surrogate (a JSON "\\udc80": it would land in
    the file as a raw byte no UTF-8 reader accepts). Shared by the settings and the stack relocation."""
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in _UNICODE_LINE_BREAKS for c in v):
        return "must be one line of plain text (no newline, tab, line separator or control character)"
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return "must be valid text (no unpaired surrogate)"
    return None


def _show_flag(v):
    return "1" if v else "0"


def _show_mib(v):
    return str(int(v) >> 20)


def _strip(raw):
    return (raw or "").strip()


def _setting(group, type_, apply, var, parse, show=str, choices=None, check=None):
    """One registry row. parse(raw) turns the config string (None = the key is absent) into exactly
    what the import-time code above produces for it -- the live apply and the next-start view both
    use it (selftest-locked against those expressions); show() turns the global back into a string;
    check narrows a path: "file" (an existing file), "pem" (a loadable PEM bundle), "dir" (not a file)."""
    return {"group": group, "type": type_, "apply": apply, "var": var, "parse": parse, "show": show,
            "choices": choices, "check": check}


AGENT_SETTINGS = {
    "GIO_SERVER_NAME": _setting("general", "text", "live", "SERVER_NAME",
                                lambda raw: (raw or "").strip() or socket.gethostname()),
    "GIO_PROVISION_MODE": _setting("general", "enum", "live", "PROVISION_MODE",
                                   lambda raw: ("once" if raw is None else raw).strip().lower(),
                                   choices=("once", "switch", "never")),
    "GIO_TXT_FIXES_MODE": _setting("general", "enum", "live", "TXT_FIXES_MODE",
                                   lambda raw: ("now" if raw is None else raw).strip().lower(),
                                   choices=("now", "later")),
    # the setup-time default of a stack prepared without an explicit choice; never changes a stack
    "GIO_PATHFINDING": _setting("general", "bool", "live", "PATHFINDING_DEFAULT", _flag_on, _show_flag),
    # read by ensure_bind_ip before a bootstrap renders .env (and by the loopback heal); an installed
    # stack keeps the OUTER_IP it was set up with
    "GIO_BIND_IP": _setting("network", "ip", "live", "BIND_IP", _strip),
    # the running value is the one this process started with (ADVERTISED_IP follows a DDNS host)
    "GIO_ADVERTISED_IP": _setting("network", "ip", "restart", "_BOOT_ADVERTISED_IP", _strip),
    # the advertise watchdog thread exists only when a host was set at start
    "GIO_ADVERTISED_HOST": _setting("network", "host", "restart", "ADVERTISED_HOST",
                                    lambda raw: (raw or "").strip().rstrip(".")),
    "GIO_ADVERTISED_ALLOW_PRIVATE": _setting("network", "bool", "live", "ADVERTISED_ALLOW_PRIVATE", _flag_off,
                                             _show_flag),
    "GIO_ADVERTISED_CHECK": _setting("network", "seconds", "live", "ADVERTISED_CHECK", _parse_check_every),
    "GIO_MUIP_HOST": _setting("network", "url", "live", "MUIP_HOST", _strip),
    # lands in t_region_config at the next hotpatch apply
    "GIO_HOTPATCH_URL": _setting("network", "url", "live", "HOTPATCH_URL",
                                 lambda raw: (raw or "").strip().rstrip("/")),
    "GIO_TRUST_PROXY": _setting("network", "bool", "live", "TRUST_PROXY", _flag_off, _show_flag),
    # the files already mirrored are NOT moved
    "GIO_HOTPATCH_DIR": _setting("storage", "path", "restart", "HOTPATCH_DIR",
                                 lambda raw: _hotpatch_dir_from(raw, STATE_PATH), check="dir"),
    "GIO_HOTPATCH_BUNDLE_DIR": _setting("storage", "path", "restart", "HOTPATCH_BUNDLE_DIR",
                                        lambda raw: _bundle_dir_from(raw, HOTPATCH_DIR)[0], check="dir"),
    "GIO_7Z": _setting("storage", "path", "live", "EXTRACTOR_PATH", _strip, check="file"),
    "GIO_FETCH_DISK_RESERVE": _setting("downloads", "mib", "live", "FETCH_DISK_RESERVE",
                                       lambda raw: _parse_mib(raw, 4096) << 20, _show_mib),
    "GIO_HOTPATCH_DISK_RESERVE": _setting("downloads", "mib", "live", "HOTPATCH_DISK_RESERVE",
                                          lambda raw: _parse_mib(raw, 2048) << 20, _show_mib),
    "GIO_HOTPATCH_ONDEMAND_MAX": _setting("downloads", "mib", "live", "HOTPATCH_ONDEMAND_MAX",
                                          lambda raw: _parse_mib(raw, 2048) << 20, _show_mib),
    # a hard off, AND-ed with the policy's hotpatchUpstream
    "GIO_HOTPATCH_UPSTREAM": _setting("downloads", "bool", "live", "HOTPATCH_UPSTREAM", _flag_on, _show_flag),
    "GIO_HOTPATCH_BUNDLES": _setting("downloads", "bool", "live", "HOTPATCH_BUNDLES", _flag_on, _show_flag),
    "GIO_TLS_PINNED_FALLBACK": _setting("downloads", "bool", "live", "TLS_PINNED_FALLBACK", _flag_on, _show_flag),
    # a change re-installs the process-wide opener
    "GIO_CA_FILE": _setting("downloads", "path", "live", "CA_FILE", _strip, check="pem"),
}
# Known keys POST /agent/config refuses (400) with a reason; any other unknown key is refused too.
_SETTING_REFUSED = {
    "GIO_DIR_16": "a server's folder is changed with POST /server/relocate (Agent settings, Server folders)",
    "GIO_DIR_28": "a server's folder is changed with POST /server/relocate (Agent settings, Server folders)",
    "GIO_HOTPATCH_SOURCE": "the hotpatch card's Mirror source sets it (the server's policy)",
    "GIO_AGENT_TOKEN": "set when the agent is installed -- not editable here",
    "GIO_AGENT_LISTEN": "set when the agent is installed -- not editable here",
    "GIO_MUIP_KEY": "set when the agent is installed -- not editable here",
    "GIO_STATE_PATH": "set when the agent is installed -- not editable here",
    "GIO_PAYLOAD_DIR": "set when the agent is installed -- not editable here",
    "GIO_AGENT_REGION": "set when the agent is installed -- not editable here",
}
_DNS_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_config_lock = threading.RLock()  # the config file's one writer + the live apply that follows it


def runs_from_systemd_file():
    """True when systemd started this process with the config file as its EnvironmentFile (no --config,
    the unit's own child): then the FILE wins over the unit's Environment= lines and the manager's
    environment (systemd.exec: "Settings from these files override settings made with Environment=")."""
    return not CONFIG_FILE and CONFIG_PATH == SYSTEMD_CONFIG and restart_mode() == "systemd"


def env_overridden(key, boot_env=None, boot_file=None, systemd_file=None):
    """True when the environment this process started with sets key to something else than the config
    file did, so editing the file changes nothing at the next start. With --config (and a manual run)
    every exported value wins over the file (apply_config_file never overrides one). Under systemd's
    EnvironmentFile the file wins over Environment= / the manager's environment, so only a key the file
    DOES set that arrived with another value is overridden (another EnvironmentFile= of a drop-in, read
    later); a key the file does not set is not -- writing it there would win. Pure given its arguments
    (defaults: the boot snapshots, runs_from_systemd_file())."""
    boot_env = _BOOT_ENV if boot_env is None else boot_env
    boot_file = _BOOT_FILE if boot_file is None else boot_file
    systemd_file = runs_from_systemd_file() if systemd_file is None else systemd_file
    if key not in boot_env:
        return False
    if systemd_file:
        return key in boot_file and boot_file[key] != boot_env[key]
    return boot_file.get(key) != boot_env[key]


def env_beneath_file(key, boot_env=None, boot_file=None, systemd_file=None):
    """The value the next start takes for key when the config file does NOT set it: under systemd the
    unit's Environment= / manager value this process received for a key the file did not set; else None
    (a self-restart hands the new process only what the file does not own, respawn_env). Pure given its
    arguments."""
    boot_env = _BOOT_ENV if boot_env is None else boot_env
    boot_file = _BOOT_FILE if boot_file is None else boot_file
    systemd_file = runs_from_systemd_file() if systemd_file is None else systemd_file
    if systemd_file and key in boot_env and key not in boot_file:
        return boot_env[key]
    return None


def read_config_values(path=None):
    """{key: value} of the config file as the NEXT start reads it (first line wins with --config,
    the last for systemd); {} when there is none or it cannot be read."""
    path = CONFIG_PATH if path is None else path
    if not path:
        return {}
    try:
        return config_dict(read_config_pairs(path), first_wins=bool(CONFIG_FILE))
    except (OSError, ValueError):
        return {}


def config_writable(path=None):
    """(writable, why-not) for the file this agent would save its settings to."""
    path = CONFIG_PATH if path is None else path
    if not path:
        return False, "it was started without a configuration file (no --config%s)" % (
            "" if IS_WINDOWS else ", no " + SYSTEMD_CONFIG)
    folder = os.path.dirname(os.path.abspath(path)) or "."
    if os.path.islink(path):
        return False, "%s is a symbolic link" % path  # a rename would replace the link, not its target
    if os.path.lexists(path):
        if not os.path.isfile(path):
            return False, "%s is not a regular file" % path
        if not os.access(path, os.W_OK):
            return False, "%s is not writable by this process" % path
    if not os.path.isdir(folder) or not os.access(folder, os.W_OK):
        return False, "its folder %s is not writable by this process" % folder
    return True, None


def render_config_update(text, updates, explicit_empty=()):
    """The config text with `updates` ({KEY: value}, "" = drop the key) applied. Pure. Comments, blank
    lines, unknown keys and the line order stay; the newline style is the file's (CRLF when any line
    has one); a BOM is dropped. Per updated key the FIRST matching line is rewritten KEY=value (or
    dropped for "") and every later line of that key is dropped -- --config reads the first, systemd
    the last, so after a write only one line may exist. New keys are appended. A line counts as a key
    exactly when parse_config_text would read it. A key in `explicit_empty` is written "KEY=" for ""
    instead of dropped: a stack folder taken off the agent on Linux, where an ABSENT GIO_DIR_xx means
    the /home/<v>_live default."""
    text = text.lstrip("﻿")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ended the last line, not an empty line of its own
    out, seen = [], set()
    for raw in lines:
        raw = raw[:-1] if raw.endswith("\r") else raw
        s = raw.strip()
        key = None
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k in updates and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                key = k
        if key is None:
            out.append(raw)
            continue
        if key not in seen and (updates[key] != "" or key in explicit_empty):
            out.append("%s=%s" % (key, updates[key]))
        seen.add(key)
    for k, v in updates.items():
        if k not in seen and (v != "" or k in explicit_empty):
            out.append("%s=%s" % (k, v))
    return nl.join(out) + (nl if out else "")


def update_config_file(path, updates, explicit_empty=()):
    """Apply `updates` to the config file at path (render_config_update) with an atomic write: a temp
    file created 0600 beside it, the original's mode (and owner, when running as root on POSIX)
    copied onto it, fsync, rename over the original, directory fsync. A missing file is created (0600).
    Bytes that are not UTF-8 round-trip untouched. Returns the new text; OSError is the caller's."""
    try:
        text = read_text(path)
    except FileNotFoundError:
        text = ""
    new = render_config_update(text, updates, explicit_empty)
    folder = os.path.dirname(os.path.abspath(path)) or "."
    tmp = os.path.join(folder, "." + os.path.basename(path) + ".relic-tmp")
    _remove_quiet(tmp)  # a leftover of a crashed write (the caller holds _config_lock)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new.encode("utf-8", "surrogateescape"))
            f.flush()
            os.fsync(f.fileno())
        st = _stat_or_none(path)
        if st is not None and os.name != "nt":
            os.chmod(tmp, st.st_mode & 0o7777)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
        replace_file(tmp, path)
    except BaseException:
        _remove_quiet(tmp)
        raise
    _fsync_dir(folder)
    return new


def _config_not_writable(why):
    return AgentError(409, "This agent cannot save its configuration: %s -- edit %s by hand and restart it."
                      % (why, CONFIG_PATH or "its configuration"))


def save_config_values(updates, explicit_empty=()):
    """THE writer of this agent's config file (POST /agent/config, the stack relocation): {KEY: value},
    "" removes the key (or writes "KEY=" for a key in explicit_empty). AgentError 409 when this agent
    cannot save it, 500 when the write fails. Returns the new text."""
    writable, why = config_writable()
    if not writable:
        raise _config_not_writable(why)
    with _config_lock:
        try:
            return update_config_file(CONFIG_PATH, updates, explicit_empty)
        except OSError as e:
            raise AgentError(500, "Saving %s failed: %s" % (CONFIG_PATH, e))


def _is_abs_path(v):
    # Windows: a drive or UNC path (Python < 3.13 calls "\x" absolute: it is relative to the drive)
    return os.path.isabs(v) and (os.name != "nt" or bool(os.path.splitdrive(v)[0]))


def _ca_file_problem(path):
    """None when path is a PEM bundle OpenSSL loads, else why not."""
    if ssl is None:
        return "this Python has no ssl module"
    try:
        ssl.create_default_context().load_verify_locations(cafile=path)
    except (OSError, ValueError) as e:
        return "not a loadable PEM bundle (%s)" % e
    return None


def normalise_setting(key, value):
    """A POSTed value -> the string to store ("" = remove the key), or AgentError 400 "KEY: why".
    Values travel as strings (a JSON bool / integer is accepted and spelled as one): bools become
    1/0, numbers plain decimals, URLs lose a trailing slash, a DNS name its trailing dot."""
    spec = AGENT_SETTINGS[key]

    def bad(why):
        return AgentError(400, "%s: %s" % (key, why))

    if isinstance(value, bool):
        value = "1" if value else "0"
    elif isinstance(value, int):
        value = str(value)
    elif not isinstance(value, str):
        raise bad("must be a string")
    v = value.strip()
    why = plain_line_problem(v)
    if why:
        raise bad(why)
    if v[:1] in ("'", '"'):
        # the config readers strip one pair of surrounding quotes, and systemd reads an opening one up
        # to its closing partner -- a value is stored unquoted
        raise bad("must not start with a quote")
    if "\\" in v and not CONFIG_FILE and not IS_WINDOWS:
        raise bad("a backslash is an escape character in the systemd EnvironmentFile -- not supported here")
    if v == "":
        return ""
    t = spec["type"]
    if t == "bool":
        if v.lower() in _TRUTHY:
            return "1"
        if v.lower() in _FALSY:
            return "0"
        raise bad("must be on or off (1/0, true/false, yes/no, on/off)")
    if t == "enum":
        if v.lower() in spec["choices"]:
            return v.lower()
        raise bad("must be one of %s" % ", ".join(spec["choices"]))
    if t in ("mib", "seconds"):
        lo, hi = (0, SETTING_MIB_MAX) if t == "mib" else SETTING_SECONDS
        if not re.match(r"^[0-9]{1,12}$", v) or not lo <= int(v) <= hi:
            raise bad("must be a whole number from %d to %d%s" % (lo, hi, " (MiB)" if t == "mib" else " (seconds)"))
        return str(int(v))
    if t == "ip":
        try:
            return str(ipaddress.IPv4Address(v))
        except ValueError:
            raise bad("must be an IPv4 address like 192.0.2.10 (or empty)")
    if t == "host":
        v = v.rstrip(".")
        try:
            ipaddress.ip_address(v)
            is_ip = True
        except ValueError:
            is_ip = False
        if is_ip:
            raise bad("is an IP address -- a fixed address belongs in GIO_ADVERTISED_IP")
        if not (0 < len(v) <= 253 and all(_DNS_LABEL_RE.match(p) for p in v.split("."))):
            raise bad("must be a DNS name like game.example.com (or empty)")
        return v
    if t in ("url", "path") and len(v) > SETTING_PATH_MAX:
        raise bad("must be at most %d characters" % SETTING_PATH_MAX)
    if t == "url":
        v = v.rstrip("/")
        try:
            p = urllib.parse.urlsplit(v)
            ok_url = (p.scheme.lower() in ("http", "https") and bool(p.hostname) and not p.query
                      and not p.fragment and " " not in v)
            p.port  # noqa: B018 -- raises ValueError on a malformed port
        except ValueError:
            ok_url = False
        if not ok_url:
            raise bad("must be an http:// or https:// URL without a query (or empty)")
        return v
    if t == "path":
        if not _is_abs_path(v):
            raise bad("must be an absolute path%s (or empty)" % (" with a drive letter" if os.name == "nt" else ""))
        if spec["check"] in ("file", "pem") and not os.path.isfile(v):
            raise bad("%s is not an existing file" % v)
        if spec["check"] == "dir" and os.path.isfile(v):
            raise bad("%s is a file, not a folder" % v)
        if spec["check"] == "pem":
            why = _ca_file_problem(v)
            if why:
                raise bad("%s is %s" % (v, why))
        return v
    if len(v) > SETTING_TEXT_MAX:
        raise bad("must be at most %d characters" % SETTING_TEXT_MAX)
    return v


def _setting_value(key):
    spec = AGENT_SETTINGS[key]
    return spec["show"](globals()[spec["var"]])


def _setting_default(key):
    spec = AGENT_SETTINGS[key]
    return spec["show"](spec["parse"](None))


def _next_raw(key, stored, updates=None):
    """The raw value the NEXT start reads for key (None = absent): a pending update, else an
    environment override, else the file -- and where the file will not set the key, what the environment
    supplies beneath it (env_beneath_file: a systemd Environment= line)."""
    if updates and key in updates:
        raw = updates[key] or None
    elif env_overridden(key):
        return _BOOT_ENV[key]
    else:
        raw = stored.get(key)
    return env_beneath_file(key) if raw is None else raw


def _next_hotpatch_dirs(stored, updates=None):
    """(mirror, bundle folder, refusals) of the NEXT start: _hotpatch_dirs_from over the next raw values and
    the folders as they are now."""
    return _hotpatch_dirs_from(_next_raw("GIO_HOTPATCH_DIR", stored, updates),
                               _next_raw("GIO_HOTPATCH_BUNDLE_DIR", stored, updates), STATE_PATH,
                               _agent_owned_folders(CONFIG_PATH, STATE_PATH, PAYLOAD_DIR), _stack_dir_pairs())


def _stack_dir_pairs():
    return [(v, meta["dir"]) for v, meta in list(VERSIONS.items())]


def _same_path(a, b):
    def norm(p):
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    return norm(a) == norm(b)


def restart_pending(stored, updates=None):
    """The restart-only keys whose next-start value differs from what this process runs with."""
    # the two folders as the next start derives them: its refusals, and the bundle folder's default (and its
    # inside-the-mirror refusal) following the NEXT mirror folder
    next_mirror, next_bundle, _refused = _next_hotpatch_dirs(stored, updates)
    out = []
    for key in AGENT_SETTING_KEYS:
        spec = AGENT_SETTINGS[key]
        if spec["apply"] != "restart":
            continue
        if key == "GIO_HOTPATCH_DIR":
            nxt = next_mirror
        elif key == "GIO_HOTPATCH_BUNDLE_DIR":
            nxt = next_bundle
        else:
            nxt = spec["parse"](_next_raw(key, stored, updates))
        cur = globals()[spec["var"]]
        if not (_same_path(nxt, cur) if spec["type"] == "path" else nxt == cur):
            out.append(key)
    return out


def format_listen(host, port):
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def restart_mode(env=None, is_windows=None, argv=None, executable=None, ppid=None):
    """How POST /agent/restart would bring this process back: "systemd" (a system service -- the unit's
    Restart=always restarts it after an exit, with the EnvironmentFile read again), "self" (start a new
    copy of this script, then exit) or None (neither is possible). Pure given its arguments. systemd is
    believed only for a child of PID 1: INVOCATION_ID can leak into a shell a terminal service started,
    and exiting there would leave no agent at all (a "self" restart under systemd still ends well: the
    unit's cgroup is restarted as a whole)."""
    env = _BOOT_ENV if env is None else env
    is_windows = IS_WINDOWS if is_windows is None else is_windows
    argv = sys.argv if argv is None else argv
    executable = sys.executable if executable is None else executable
    if not is_windows and ("INVOCATION_ID" in env or "JOURNAL_STREAM" in env):
        if (os.getppid() if ppid is None else ppid) == 1:
            return "systemd"
    if executable and argv and argv[0] and os.path.isfile(argv[0]):
        return "self"
    return None


def respawn_env(boot_env=None, boot_file=None, file_now=None):
    """The environment a self-restart hands the new process: the BOOT environment (os.environ holds the old
    config file's values, which would win over the rewritten file) minus what the config file owned -- a
    variable that arrived with exactly the value the file held at boot (exported from it, `set -a; . config`)
    takes the file's CURRENT value, or goes when the file no longer sets it. A value the file did not hold is
    an explicit override and stays (POST /agent/config refused to edit it). Pure given its arguments
    (defaults: the boot snapshots and read_config_values())."""
    boot_env = _BOOT_ENV if boot_env is None else boot_env
    boot_file = _BOOT_FILE if boot_file is None else boot_file
    file_now = read_config_values() if file_now is None else file_now
    out = dict(boot_env)
    for k, v in boot_env.items():
        if k in boot_file and boot_file[k] == v:
            if k in file_now:
                out[k] = file_now[k]
            else:
                del out[k]
    return out


def respawn_command(argv=None, executable=None, env=None, is_windows=None, cwd=None):
    """(command, Popen kwargs) of a self-restart: this interpreter + this script + the same arguments,
    respawn_env() (the boot environment with the config file authoritative for what it owned), the same
    working directory, no inherited handles, detached from this process."""
    argv = list(sys.argv if argv is None else argv)
    executable = sys.executable if executable is None else executable
    env = respawn_env() if env is None else env
    is_windows = IS_WINDOWS if is_windows is None else is_windows
    cmd = [executable, os.path.abspath(argv[0])] + argv[1:]
    kw = {"env": dict(env), "cwd": os.getcwd() if cwd is None else cwd, "close_fds": True,
          "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if is_windows:
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kw["start_new_session"] = True
    return cmd, kw


def _addr_in_use(e):
    return getattr(e, "errno", None) in (errno.EADDRINUSE, 10048) or getattr(e, "winerror", None) == 10048


def bind_http_server(host, port, factory=None, wait=10.0, sleep=None):
    """The listening server, retrying "address in use" for up to `wait` seconds: after a restart the
    previous process may still be closing its socket."""
    factory = factory or (lambda addr: ThreadingHTTPServer(addr, Handler))
    sleep = sleep or time.sleep
    deadline = time.monotonic() + wait
    while True:
        try:
            return factory((host, port))
        except OSError as e:
            if not _addr_in_use(e) or time.monotonic() >= deadline:
                raise
            sleep(0.5)


def write_pid_file(config_file=None, is_windows=None, pid=None):
    """Windows: agent.pid beside --config (the launcher's LocalAgent reads it) -- written by the agent
    too, so a self-restarted agent keeps it truthful. Never fatal. Returns the path written or None."""
    config_file = CONFIG_FILE if config_file is None else config_file
    is_windows = IS_WINDOWS if is_windows is None else is_windows
    if not (is_windows and config_file):
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(config_file)), "agent.pid")
    try:
        write_text(path, str(os.getpid() if pid is None else pid))
        return path
    except OSError as e:
        log_line("gio-agent: WARNING: could not write %s (%s)" % (path, e))
        return None


def agent_config_info():
    """GET /agent/config."""
    stored = read_config_values()
    writable, why = config_writable()
    settings = []
    for key in AGENT_SETTING_KEYS:
        spec = AGENT_SETTINGS[key]
        item = {"key": key, "group": spec["group"], "type": spec["type"]}
        if spec["choices"]:
            item["choices"] = list(spec["choices"])
        item.update({"value": _setting_value(key), "stored": stored.get(key), "default": _setting_default(key),
                     "apply": spec["apply"], "env": env_overridden(key)})
        settings.append(item)
    running = running_projects()[0] or []
    versions = {}
    for v in list(VERSIONS):
        meta = VERSIONS[v]  # one read: a relocation swaps the whole dict
        versions[v] = {"dir": meta["dir"], "configured": bool(meta["dir"]), "present": is_present(v),
                       "up": bool(meta["dir"]) and meta["project"] in running, "project": meta["project"]}
    with _tls_lock:
        hosts = sorted(_TLS_FALLBACK_HOSTS)
    strict = bool(_TLS_CONTEXT is not None
                  and int(_TLS_CONTEXT.verify_flags) & int(getattr(ssl, "VERIFY_X509_STRICT", 0)))
    return {
        "agent": AGENT_VERSION, "platform": "windows" if IS_WINDOWS else "linux", "started": round(_STARTED, 3),
        "file": CONFIG_PATH, "writable": writable, "writableReason": why, "restart": restart_mode(),
        "listen": format_listen(LISTEN_HOST, LISTEN_PORT),
        "restartPending": restart_pending(stored),
        # caFile: the extra roots the installed opener really carries ("" when none loaded); caFileError: why
        # GIO_CA_FILE did not load (the opener then runs without it), else null
        "tls": {"strict": strict, "caFile": _TLS_CA_LOADED, "caFileError": _TLS_CA_ERROR,
                "pinnedFallback": TLS_PINNED_FALLBACK, "fallbackHosts": hosts},
        "settings": settings,
        "versions": versions,
    }


def set_agent_config(body):
    """POST /agent/config {"set": {KEY: value}}. Everything is validated before anything is written:
    400 naming the key (unknown, not editable here, an invalid value, both advertised keys set, the
    bundle folder inside the mirror, a mirror / bundle folder that is a drive root, is or contains one of
    the agent's own folders or overlaps a server folder), 409 for an environment-overridden key, a file
    this agent cannot save or a restart under way. Then the file is written, the live keys are applied, and the GET body
    comes back with `applied` (live keys set) and `restartRequired` (restart keys set whose next-start
    value differs from the running one)."""
    sets = body.get("set")
    if not isinstance(sets, dict) or not sets:
        raise AgentError(400, "set must be a non-empty object of setting: value")
    updates = {}
    for key, value in sets.items():
        if key in AGENT_SETTINGS:
            updates[key] = normalise_setting(key, value)
        elif key in _SETTING_REFUSED:
            raise AgentError(400, "%s: %s" % (key, _SETTING_REFUSED[key]))
        else:
            raise AgentError(400, "%s: unknown setting" % key)
    stored = read_config_values()
    if "GIO_ADVERTISED_IP" in updates or "GIO_ADVERTISED_HOST" in updates:
        ip = _strip(_next_raw("GIO_ADVERTISED_IP", stored, updates))
        host = _strip(_next_raw("GIO_ADVERTISED_HOST", stored, updates)).rstrip(".")
        if ip and host:
            key = "GIO_ADVERTISED_HOST" if "GIO_ADVERTISED_HOST" in updates else "GIO_ADVERTISED_IP"
            raise AgentError(400, "%s: GIO_ADVERTISED_IP and GIO_ADVERTISED_HOST cannot both be set (the host would "
                                  "win and the IP be ignored) -- clear one of them" % key)
    if "GIO_HOTPATCH_DIR" in updates or "GIO_HOTPATCH_BUNDLE_DIR" in updates:
        mirror = _hotpatch_dir_from(_next_raw("GIO_HOTPATCH_DIR", stored, updates), STATE_PATH)
        bundle = _strip(_next_raw("GIO_HOTPATCH_BUNDLE_DIR", stored, updates))
        if bundle and _bundle_dir_inside_mirror(bundle, mirror):
            key = "GIO_HOTPATCH_BUNDLE_DIR" if "GIO_HOTPATCH_BUNDLE_DIR" in updates else "GIO_HOTPATCH_DIR"
            raise AgentError(400, "%s: the bundle folder %s would lie inside the hotpatch mirror %s, which serves "
                                  "everything under it" % (key, bundle, mirror))
        # the mirror is served to anyone without a token and the agent writes + empties the bundle folder: a
        # folder of their own only (the next start would ignore anything else with a WARNING)
        owned, stacks = _agent_owned_folders(CONFIG_PATH, STATE_PATH, PAYLOAD_DIR), _stack_dir_pairs()
        for key, folder, what in (("GIO_HOTPATCH_DIR", mirror, "the hotpatch mirror is served to anyone without a "
                                                               "token"),
                                  ("GIO_HOTPATCH_BUNDLE_DIR", bundle, "the agent writes and empties the bundle "
                                                                      "folder")):
            if updates.get(key):
                problem = _served_folder_problem(folder, owned, stacks)
                if problem:
                    raise AgentError(400, "%s: %s -- %s, so it must be a folder of its own." % (key, problem, what))
    for key in updates:
        if env_overridden(key):
            raise AgentError(409, "%s is set by this agent's environment (an exported variable, or another "
                                  "EnvironmentFile= of a systemd drop-in), which wins over the configuration file "
                                  "-- change it there." % key)
    writable, why = config_writable()
    if not writable:
        raise _config_not_writable(why)
    with _config_lock:
        if _restarting:
            raise _restarting_error()
        save_config_values(updates)
        applied = []
        for key in AGENT_SETTING_KEYS:
            spec = AGENT_SETTINGS[key]
            if key in updates and spec["apply"] == "live":
                globals()[spec["var"]] = spec["parse"](updates[key] or None)  # "" = removed = the default
                applied.append(key)
        if "GIO_CA_FILE" in applied:
            install_http_opener()
        info = agent_config_info()
    _invalidate_public_status()
    log_line("gio-agent: settings saved to %s: %s" % (CONFIG_PATH, ", ".join(
        "%s=%s" % (k, updates[k] or "(default)") for k in AGENT_SETTING_KEYS if k in updates)))
    info["applied"] = applied
    info["restartRequired"] = [k for k in AGENT_SETTING_KEYS if k in updates
                               and AGENT_SETTINGS[k]["apply"] == "restart" and k in info["restartPending"]]
    return info


# The server main() serves (POST /agent/restart shuts it down); None in the selftest and the CLI modes.
_HTTPD = None


def request_restart():
    """POST /agent/restart's refusals, then the flag that stops every new job: 409 when this agent
    cannot restart itself, is not serving, or any job runs (a yielding one included). Returns how."""
    global _restarting
    how = restart_mode()
    if how is None:
        raise AgentError(409, "This agent cannot restart itself (it does not run under systemd and its script "
                              "path is not a file it can start again) -- restart it by hand.")
    if _HTTPD is None:
        raise AgentError(409, "This agent process is not serving -- nothing to restart.")
    with _jobs_lock:
        if _restarting:
            raise _restarting_error()
        busy = _current_job_locked()
        if busy is not None:
            raise _busy_error(busy)
        _restarting = True
    return how


def _launch_restart(how):
    """After the 202 went out. Module level so the selftest can swap it (it never restarts anything)."""
    threading.Thread(target=_restart_worker, args=(how,), name="gio-agent-restart", daemon=True).start()


def _restart_worker(how):
    global _HTTPD, _restarting
    time.sleep(0.5)  # the 202 is on its way
    log_line("gio-agent: restarting on request (%s) ..." % how)
    try:
        _HTTPD.shutdown()
        _HTTPD.server_close()  # the port is free for the next process
    except Exception as e:  # noqa: BLE001
        log_line("gio-agent: restart: closing the server: %s" % e)
    # A settings write in flight finishes first; a short critical section a watchdog runs outside any
    # job (the advertise swap) gets a moment too. Both files are written tmp + rename anyway.
    _config_lock.acquire()
    got_op = _op_lock.acquire(timeout=30)
    if how == "systemd":
        os._exit(0)  # Restart=always: back in RestartSec with the EnvironmentFile read again
    try:
        cmd, kw = respawn_command()  # inside: a vanished working directory raises here too
        subprocess.Popen(cmd, **kw)
    except Exception as e:  # noqa: BLE001 -- whatever failed, this process must keep serving
        # Never leave the box without an agent: serve again from this thread (main() is parked).
        log_line("gio-agent: ERROR: the restart could not start a new process (%s) -- this one keeps serving." % e)
        if got_op:
            _op_lock.release()
        _config_lock.release()
        try:
            _HTTPD = bind_http_server(LISTEN_HOST, LISTEN_PORT)
        except Exception as e2:  # noqa: BLE001
            # Serving again is impossible: end the process rather than linger with no listener (systemd's
            # Restart=always, or the launcher's Start, brings a fresh one up).
            log_line("gio-agent: ERROR: could not listen again on %s (%s) -- exiting."
                     % (format_listen(LISTEN_HOST, LISTEN_PORT), e2))
            os._exit(1)
        _restarting = False
        _HTTPD.serve_forever()
        return
    os._exit(0)


# -- stack relocation (agent 3.6): POST /server/relocate, job `relocate` --
# GIO_DIR_xx changes only here, never through /agent/config: the folder's basename IS the docker compose
# project name, and docker keeps every container's absolute bind paths, so a config-only change would
# orphan the old project -- still holding the ports and the 172.10.3.0/24 subnet, invisible to every
# up-check and watchdog, which look for the new name. The job, under _op_lock, brings the version down
# under its CURRENT project name with the full grace (the game server saves the players' data), then
#   move    -- takes the tree along: one rename on the same volume; across volumes a copy into the
#              sibling .relic-move-<name> (cp -a on Linux: owner, mode, links, xattrs -- MariaDB's and
#              redis' data need them; a Python walk on Windows), verified by counts + bytes and renamed
#              into place, the old tree deleted only once the config file names the new folder, or
#   repoint -- touches no file: the version is pointed at a folder that already holds a stack (moved by
#              hand, a second copy), at one still empty or absent (a later download fills it), or at
#              nothing ("" = the version leaves this agent),
# persists GIO_DIR_xx (the config file first: a restart must never revert it), swaps VERSIONS[v] in one
# assignment and starts the stack again when it was running. Nothing inside a stack holds its own
# absolute path (the renders substitute .env values, every bind mount is relative), so a moved stack
# needs no re-render.

RELOCATE_MODES = ("move", "repoint")
RELOCATE_RENAME_WAIT = 30              # seconds a rename is retried while Windows still holds the tree
RELOCATE_COPY_TIMEOUT = 6 * 3600       # a cross-volume `cp -a` still running after this is killed
RELOCATE_PROGRESS_EVERY = 15           # seconds between two progress lines of a `cp -a` copy
RELOCATE_PROGRESS_BYTES = 500 << 20    # bytes between two progress lines of the Windows copy
_WIN_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# A repoint at a folder that is NOT the same server (its .relic-provisioned does not say what the state's
# provisionedAt says) keeps only the admin's decisions about the version -- re-asserted onto the new stack
# by the next start (the pathfinding profile, the hotpatch files + URL columns) or not bound to a stack at
# all (the mirror's voice languages, where the mirror's files came from, the toggle times). Everything else
# every version_state_set() caller writes describes the old folder's CONTENT and is dropped:
#   runningOk, provisionedAt, progress, defaultAccount, txtFixes (its database and payload files),
#   advertisedIp, advertisedIpSql, advertisedIpSqlOwed (its XMLs and URL columns), towerRestartPending (its
#   gameserver), templates (its relic_tpl_* databases), secrets (changedAt, muipChangedAt, muipSource: its
#   .env / muipserver.xml), secretsPending, passwordVerify (re-read from the new stack's sdk config),
#   passwordPending, defaultPassword (its sdk accounts), fetchedAt, fetchedFrom (what was downloaded there),
#   and inside `hotpatch` what was APPLIED to it: url, res, data, silence -- `pending` is set again for an
#   enabled hotpatch (the new stack advertises nothing until the next start re-asserts it).
# The provisioning record is then filled in from the new folder's marker (provision_record /
# needs_provision), as for any lost record -- and an INSTALLED folder without a marker gets autoProvision
# False (dropped like the rest by the next repoint, cleared by an explicit provision): nothing could re-seed
# its record, and the next Start must not import the save over a database that may hold players. The
# selftest checks every keyword a version_state_set() call in this file writes against these lists.
_RELOCATE_DROP = ("runningOk", "provisionedAt", "progress", "defaultAccount", "txtFixes", "advertisedIp",
                  "advertisedIpSql", "advertisedIpSqlOwed", "towerRestartPending", "templates", "secrets",
                  "secretsPending", "passwordVerify", "passwordPending", "defaultPassword", "fetchedAt",
                  "fetchedFrom", "autoProvision")
_RELOCATE_KEEP = ("pathfinding", "hotpatch")
_RELOCATE_HOTPATCH_KEEP = ("enabled", "voice", "source", "appliedAt", "disabledAt")


class RelocateStopped(Exception):
    """POST /server/relocate {"cancel": true} arrived while the tree was being copied."""


# (_real_norm / _path_within live at the top of the file: the import-time hotpatch folder check uses them.)


def _existing_ancestor(path):
    """path when it exists, else its nearest existing parent (a missing drive root comes back as it is)."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def _same_volume(a, b):
    """Do a and b (its nearest existing ancestor) sit on one volume (st_dev)? None when unreadable. A
    rename may still refuse (EXDEV: a bind mount, another btrfs subvolume) -- the move copies then.
    Module level: the selftest forces the cross-volume path through it."""
    try:
        return os.stat(a).st_dev == os.stat(_existing_ancestor(b)).st_dev
    except OSError:
        return None


def _is_mount(path):
    """os.path.ismount -- module level: the selftest swaps it (a temp folder is never a mount point)."""
    try:
        return os.path.ismount(path)
    except (OSError, ValueError):
        return False


def _sync_filesystems():
    """POSIX: sync(2) -- blocks until every filesystem has written its dirty data (a copied tree included)
    and the renames. Windows has none (_copy_tree_py fsyncs each file instead). Module level: the selftest
    swaps it."""
    if hasattr(os, "sync"):
        os.sync()


def _cross_device(e):
    # EXDEV (POSIX, and what Python maps Windows' ERROR_NOT_SAME_DEVICE = 17 to)
    return getattr(e, "errno", None) == errno.EXDEV or getattr(e, "winerror", None) == 17


def _tree_counts(path):
    """(files, folders, bytes) of a tree, symbolic links counted as themselves and never followed, sizes
    from lstat -- what a copy is verified with. os.scandir: on Windows the listing already carries the
    sizes (no open per file on a 26k-file stack). OSError is the caller's."""
    files = folders = nbytes = 0
    todo = [path]
    while todo:
        with os.scandir(todo.pop()) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    folders += 1
                    todo.append(e.path)
                else:
                    files += 1
                    nbytes += e.stat(follow_symlinks=False).st_size
    return files, folders, nbytes


def relocate_target_kind(d):
    """What is at a relocation target: "absent" | "empty" (a folder) | "stack" (a compose file or its
    template) | "other" (a non-empty folder without one, a file, an unreadable folder)."""
    if not os.path.lexists(d):
        return "absent"
    try:
        if not os.path.isdir(d):
            return "other"
        if not os.listdir(d):
            return "empty"
    except OSError:
        return "other"
    if os.path.isfile(os.path.join(d, "docker-compose.yml.tmpl")) or os.path.isfile(os.path.join(d, "docker-compose.yml")):
        return "stack"
    return "other"


def _agent_folders():
    """The agent's own folders, which a stack folder must neither lie in nor contain: its configuration,
    state, payload and script folders, and the hotpatch mirror + bundle folder -- both the ones this process
    uses and the ones the next start will use (a changed GIO_HOTPATCH_DIR waiting for the restart). The
    settings refuse the same nesting from the other side (_served_folder_problem)."""
    out = [(label, folder) for label, folder in _agent_owned_folders(CONFIG_PATH, STATE_PATH, PAYLOAD_DIR)]
    out += [("the hotpatch mirror", HOTPATCH_DIR), ("the hotpatch bundle folder", HOTPATCH_BUNDLE_DIR)]
    try:
        next_mirror, next_bundle, _refused = _next_hotpatch_dirs(read_config_values())
    except Exception:  # noqa: BLE001 -- the running folders above are checked either way
        next_mirror = next_bundle = None
    if next_mirror and not _same_path(next_mirror, HOTPATCH_DIR):
        out.append(("the hotpatch mirror (after the agent's restart)", next_mirror))
    if next_bundle and not _same_path(next_bundle, HOTPATCH_BUNDLE_DIR):
        out.append(("the hotpatch bundle folder (after the agent's restart)", next_bundle))
    return tuple(out)


# GetDriveTypeW answers a stack folder must not get (Docker Desktop shares local fixed drives only, and
# a RAM disk loses the database at the next reboot); DRIVE_UNKNOWN (0) -- or no answer -- is let through.
_WIN_DRIVE_REFUSED = {
    1: "is not a drive on this machine",
    4: "is a network drive (Docker Desktop cannot bind-mount it)",
    5: "is a CD/DVD drive",
    6: "is a RAM disk (its files are gone after a reboot)",
}


def _win_drive_type(letter):
    """Windows' GetDriveTypeW for "<letter>:\\" (0 = unknown / not Windows / ctypes unavailable -- never
    fatal). Module level: the selftest swaps it."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(letter.upper() + ":\\")))
    except Exception:  # noqa: BLE001
        return 0


def relocate_target(raw, mode, version):
    """The requested folder -> a normalised absolute path, "" (repoint only: the version leaves this
    agent), or AgentError 400. Windows: a drive-letter path only -- Docker Desktop cannot bind-mount a
    network share, and a drive-relative path is a typo. The value lands in the config file as it is: no
    quotes, no control characters (and, in the systemd EnvironmentFile, no backslash)."""
    if not isinstance(raw, str):
        raise AgentError(400, "dir must be a string: an absolute folder, or \"\" with mode repoint.")
    v = raw.strip()
    if not v:
        if mode != "repoint":
            raise AgentError(400, "A move needs the folder to move the server to (an empty dir goes only with "
                                  "repoint: it takes the version off this agent).")
        return ""
    if plain_line_problem(v) or '"' in v or "'" in v:
        # the settings' own rule (a line separator would split the config line, a lone surrogate break the file)
        raise AgentError(400, "The folder must be one plain line without quotes.")
    if len(v) > SETTING_PATH_MAX:
        raise AgentError(400, "The folder path must be at most %d characters." % SETTING_PATH_MAX)
    if "\\" in v and not CONFIG_FILE and not IS_WINDOWS:
        raise AgentError(400, "A backslash is an escape character in the systemd EnvironmentFile -- choose a "
                              "folder without one.")
    if os.name == "nt":
        if not _WIN_DRIVE_PATH_RE.match(v):
            raise AgentError(400, "The folder must be a full path on a local drive, like C:\\relic_servers\\%s_live "
                                  "(Docker Desktop cannot bind-mount a network path)." % version)
        # a letter mapped to a network share passes the spelling test above: ask Windows what it is
        why = _WIN_DRIVE_REFUSED.get(_win_drive_type(v[0]))
        if why:
            raise AgentError(400, "%s %s -- choose a folder on a local drive, like C:\\relic_servers\\%s_live."
                             % (v[:2].upper(), why, version))
    elif not os.path.isabs(v):
        raise AgentError(400, "The folder must be an absolute path, like /home/%s_live." % version)
    new = os.path.normpath(os.path.abspath(v))
    if os.path.dirname(new) == new:
        raise AgentError(400, "The folder must not be the root of a drive -- pick a folder inside it.")
    if _project_name(new) == "default":
        # compose's name for the folder: empty after normalisation (or literally "default") is no name
        raise AgentError(400, "The folder name %s gives docker no usable project name -- use letters or digits "
                              "(like %s_live)." % (os.path.basename(new), version))
    return new


def _relocate_refusal(version, cur, new, mode, rep, ls_err, in_job, projects_all=None):
    """Raise the first thing that refuses this relocation: 400 for the request itself, 409 for the state
    of the box, 500 when docker cannot say whether the stack runs. rep holds what relocate_check measured;
    projects_all is compose_projects_all()'s list (None = docker did not answer: that check is skipped)."""
    if new:
        if cur and _real_norm(new) == _real_norm(cur):
            raise AgentError(400, "%s already is the folder of version %s." % (new, version))
        proj = _project_name(new)
        for v, meta in list(VERSIONS.items()):
            if v == version or not meta["dir"]:
                continue
            if proj == meta["project"]:
                raise AgentError(400, "The folder name %s gives the docker project the same name as version %s's (%s) "
                                      "-- the two stacks would take each other over. Pick another folder name."
                                 % (os.path.basename(new), v, proj))
            if _path_within(new, meta["dir"]) or _path_within(meta["dir"], new):
                raise AgentError(400, "%s and version %s's folder %s would lie inside one another." % (new, v, meta["dir"]))
        # Another compose project on this box (running or stopped) that already carries the name: docker would
        # treat the two as one -- the up-checks would see it as this server, and the next down --remove-orphans
        # would remove its containers. The version's own name stays allowed, and so does a project whose
        # compose files live in the new folder (the very stack being pointed at, started by hand).
        own = VERSIONS[version]["project"]
        for name, config_files in (projects_all or ()):
            if name != proj or name == own:
                continue
            files = [p.strip() for p in str(config_files or "").split(",") if p.strip()]
            if not any(_path_within(p, new) for p in files):
                raise AgentError(409, "The folder name %s gives the docker project the name of another compose project "
                                      "on this box (%s, compose files %s) -- docker would treat the two as one and a "
                                      "stop would remove its containers. Pick another folder name."
                                 % (os.path.basename(new), name, ", ".join(files) or "unknown"))
        for label, folder in _agent_folders():
            if _path_within(new, folder) or _path_within(folder, new):
                raise AgentError(400, "%s would lie inside %s %s (or contain it) -- pick a folder of its own."
                                 % (new, label, folder))
        if mode == "move" and cur and (_path_within(new, cur) or _path_within(cur, new)):
            raise AgentError(400, "The new folder must not lie inside the current one %s, nor contain it." % cur)
    elif not cur:
        raise AgentError(400, "Version %s is not configured on this agent already." % version)
    key = dir_setting_key(version)
    if env_overridden(key):
        raise AgentError(409, "%s is set by this agent's environment (an exported variable, or another "
                              "EnvironmentFile= of a systemd drop-in), which wins over the configuration file -- "
                              "change it there." % key)
    writable, why = config_writable()
    if not writable:
        raise _config_not_writable(why)
    if new:
        base = _existing_ancestor(os.path.dirname(new))  # a missing drive, a file where a parent folder goes
        if not os.path.isdir(base):
            raise AgentError(409, "Nothing can be created at %s: %s is not a folder on this machine." % (new, base))
    if ls_err is not None:
        raise AgentError(500, "Cannot read the docker state (%s) -- the stack may be running; retry once docker "
                              "answers." % ls_err)
    if new and os.path.lexists(new) and not os.path.isdir(new):
        raise AgentError(409, "%s is a file, not a folder." % new)
    target = rep["target"]
    if mode == "move":
        if not cur:
            raise AgentError(409, "Version %s has no folder on this agent yet -- nothing to move; point it at a "
                                  "folder with repoint." % version)
        # A move takes the WHOLE current tree along -- and across drives deletes it after the copy: whatever else
        # lives inside it (the other server, the mirror, the agent's own folders) would go with it.
        for v, meta in list(VERSIONS.items()):
            if v != version and meta["dir"] and _path_within(meta["dir"], cur):
                raise AgentError(409, "%s holds version %s's folder %s -- a move would take that server along (and "
                                      "delete it after a copy to another drive). Move version %s out of it first."
                                 % (cur, v, meta["dir"], v))
        for label, folder in _agent_folders():
            if _path_within(folder, cur):
                raise AgentError(409, "%s contains %s %s -- a move would take it along. Move it out of the server "
                                      "folder first." % (cur, label, folder))
        if not is_present(version):
            raise AgentError(409, "%s holds no server (no docker-compose.yml.tmpl) -- nothing to move. Use repoint to "
                                  "point version %s at another folder." % (cur, version))
        if os.path.islink(new):
            raise AgentError(409, "%s is a symbolic link -- move the server to a real folder." % new)
        if os.path.isdir(new) and _is_mount(new):
            # the job would copy onto the parent's filesystem and then fail to replace the mount point
            raise AgentError(409, "%s is a mount point -- a move cannot replace it; move the server into a folder "
                                  "inside it, like %s." % (new, os.path.join(new, "%s_live" % version)))
        if target not in ("absent", "empty"):
            raise AgentError(409, "%s is not empty -- a move needs a folder that does not exist yet or is empty%s."
                             % (new, " (it holds a server already: repoint switches to it)" if target == "stack" else ""))
        need, free = rep["needBytes"], rep["freeBytes"]
        if need is not None and free is not None and free < need:
            raise AgentError(409, "Not enough free disk space on the target drive: %.1f GB needed (%.1f GB of server "
                                  "files + %d MiB reserve, GIO_FETCH_DISK_RESERVE), %.1f GB free at %s."
                             % (need / 1e9, rep["stackBytes"] / 1e9, FETCH_DISK_RESERVE >> 20, free / 1e9,
                                _existing_ancestor(new)))
    elif new and target == "other":
        raise AgentError(409, "The folder is not empty and holds no server: %s" % new)
    if not in_job:
        busy = current_job()
        if busy is not None:
            raise _busy_error(busy)


def relocate_check(version, raw_dir, mode, in_job=False, measure=True):
    """POST /server/relocate {"check": true}, the real request's synchronous refusals and the job's first
    step (again, under _op_lock). Returns (report, refusal): the report is the check reply -- what was
    measured, `ok`, and `problem` (the refusal's text) -- and refusal the AgentError the real request
    raises (None = go). An unknown version or mode is raised at once (400): nothing to report on.
    stackBytes / needBytes: a move across volumes only (on one volume nothing is copied); measure=False
    (the job) skips that walk -- the move measures the stopped tree itself."""
    check_known(version)
    if mode not in RELOCATE_MODES:
        raise AgentError(400, "mode must be move or repoint")
    meta = VERSIONS[version]  # one read: a relocation swaps the whole dict
    cur = meta["dir"]
    rep = {"version": version, "mode": mode, "from": cur, "to": raw_dir.strip() if isinstance(raw_dir, str) else "",
           "target": None, "running": None, "sameVolume": None, "stackBytes": None, "freeBytes": None,
           "needBytes": None, "ok": False, "problem": None}
    try:
        new = relocate_target(raw_dir, mode, version)
        rep["to"] = new
        ls_err = None
        if cur:
            projects, err = running_projects()
            if err is None:
                rep["running"] = meta["project"] in projects
            elif is_present(version) or is_bootstrapped(version):
                ls_err = err  # a stack that may be running is never moved blind
        else:
            rep["running"] = False
        projects_all = None
        if new:
            rep["target"] = relocate_target_kind(new)
            rep["freeBytes"] = _free_bytes_at(new)
            projects_all = compose_projects_all()[0]  # None (docker mute): ls_err covers a stack that may run
        if mode == "move" and new and cur and is_present(version) and rep["target"] in ("absent", "empty"):
            rep["sameVolume"] = _same_volume(cur, new)
            if rep["sameVolume"] is not True and measure:
                try:
                    rep["stackBytes"] = _tree_counts(cur)[2]
                except OSError as e:
                    raise AgentError(500, "Cannot measure %s (%s)." % (cur, e))
                rep["needBytes"] = rep["stackBytes"] + FETCH_DISK_RESERVE
        _relocate_refusal(version, cur, new, mode, rep, ls_err, in_job, projects_all)
    except AgentError as e:
        rep["problem"] = e.message
        return rep, e
    rep["ok"] = True
    return rep, None


def _rename_retry(src, dst, should_stop=None, wait=None):
    """os.rename, retried for up to RELOCATE_RENAME_WAIT seconds while the OS reports the tree in use
    (Windows: Docker Desktop and antivirus scanners let go of a stack's files a few seconds after the
    down -- the same wait _rmtree_retry gives them). A cross-volume refusal and anything else raise at once."""
    deadline = time.monotonic() + (RELOCATE_RENAME_WAIT if wait is None else wait)
    while True:
        try:
            os.rename(src, dst)
            return
        except OSError as e:
            if _cross_device(e):
                raise
            in_use = isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33) \
                or getattr(e, "errno", None) == errno.EBUSY
            if not in_use or time.monotonic() >= deadline or (should_stop is not None and should_stop()):
                raise
            time.sleep(1.0)


def _copy_tree(src, dst, job, should_stop, total):
    """The cross-volume copy of a stopped stack into dst (created): `cp -a` on POSIX -- owner, mode,
    symbolic and hard links, xattrs kept, which MariaDB's and redis' data folders need -- a Python walk
    on Windows. Module level: the selftest swaps it. RelocateStopped on a stop request, OSError else."""
    if os.name == "nt":
        return _copy_tree_py(src, dst, job, should_stop, total)
    return _copy_tree_cp(src, dst, job, should_stop, total)


def _copy_progress(job, done, total):
    job.log("  %.1f of %.1f GB copied (%d%%)" % (done / 1e9, total / 1e9, done * 100 // max(total, 1)))


def _copy_tree_cp(src, dst, job, should_stop, total):
    """POSIX: `cp -a <src>/. <dst>/`, polled every 0.5 s (a stop request kills it), a progress line from
    measuring dst every RELOCATE_PROGRESS_EVERY seconds. Returns the bytes of the tree."""
    os.makedirs(dst, exist_ok=True)
    argv = ["cp", "-a", os.path.join(src, "."), dst.rstrip(os.sep) + os.sep]
    job.log("$ cp -a %s %s" % (argv[2], argv[3]))
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         **_popen_kwargs())
    tail = []

    def drain():
        # an error per file on a damaged tree must not block cp on a full pipe; keep only the end
        try:
            buf = b""
            for chunk in iter(lambda: p.stdout.read(4096), b""):
                buf = (buf + chunk)[-2048:]
            tail.append(buf)
        except Exception:  # noqa: BLE001 -- the pipe closing under a kill
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    started = last = time.time()
    while True:
        rc = p.poll()
        if rc is not None:
            break
        if should_stop():
            _kill_quiet(p)
            raise RelocateStopped("stopped on request")
        now = time.time()
        if now - started > RELOCATE_COPY_TIMEOUT:
            _kill_quiet(p)
            raise OSError("cp -a did not finish within %d hours" % (RELOCATE_COPY_TIMEOUT // 3600))
        if now - last >= RELOCATE_PROGRESS_EVERY:
            last = now
            try:
                _copy_progress(job, _tree_counts(dst)[2], total)
            except OSError:
                pass
        time.sleep(0.5)
    t.join(5)
    if rc != 0:
        text = (tail[0] if tail else b"").decode("utf-8", "replace").strip()
        text = " | ".join(l.strip() for l in text.splitlines()[-4:] if l.strip())
        raise OSError("cp -a exited with code %d%s" % (rc, (": " + text) if text else ""))
    return total


def _copy_file_durable(src, dst):
    """shutil.copy2 whose data is ON DISK when it returns: the bytes copied into a new file, fsync'ed (the
    only copy of the database is deleted after the move -- a lazy writer must not be holding it), THEN the
    times and the read-only bit (copystat: fsync needs a writable handle, so the read-only bit comes last)."""
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, 1 << 20)
        fdst.flush()
        os.fsync(fdst.fileno())
    shutil.copystat(src, dst, follow_symlinks=False)


def _copy_tree_py(src, dst, job, should_stop, total):
    """Windows: the tree copied entry by entry -- _copy_file_durable (data fsync'ed, times, the read-only
    bit), a symbolic link recreated as one --, the stop request honoured between two files, a progress line
    every RELOCATE_PROGRESS_BYTES. Folder metadata is not copied (a Windows stack folder carries none that
    matters; the caller restores the ACL of creds.txt). Returns the bytes copied."""
    copied, next_log = 0, RELOCATE_PROGRESS_BYTES
    os.makedirs(dst, exist_ok=True)
    todo = [(src, dst)]
    while todo:
        s, d = todo.pop()
        with os.scandir(s) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if should_stop():
                raise RelocateStopped("stopped on request")
            target = os.path.join(d, e.name)
            if e.is_symlink():
                os.symlink(os.readlink(e.path), target, target_is_directory=os.path.isdir(e.path))
            elif e.is_dir():
                os.mkdir(target)
                todo.append((e.path, target))
            else:
                _copy_file_durable(e.path, target)
                copied += e.stat(follow_symlinks=False).st_size
                if copied >= next_log:
                    _copy_progress(job, copied, total)
                    next_log = copied + RELOCATE_PROGRESS_BYTES
    return copied


def _relocate_restart(version, job):
    """Start the version again after a relocation -- or the rollback of one -- found it running: the start
    job's own routine (the job already holds _op_lock), never with an import (provision "never": moving a
    folder must not provision anything). A failure is logged, never raised: by then the relocation is
    decided either way. Returns (started, the error text or None)."""
    job.log("Starting version %s again ..." % version)
    try:
        do_start(version, job, provision="never")
        return True, None
    except AgentError as e:
        err = e.message
    except Exception as e:  # noqa: BLE001 -- the relocation's outcome must still be reported
        err = str(e)
    job.log("WARNING: version %s did not start again (%s) -- start it from the Server page." % (version, err))
    return False, err


def _relocate_down(version, job, old, new, project, was_up):
    """Bring the version down under its CURRENT project name: `down --remove-orphans` with the full grace
    (the game server flushes the players' data) -- containers left merely stopped would keep the old bind
    paths and, with restart: always, come back on the next docker start. A project still running whose
    folder no longer holds its compose file (the tree moved by hand) is brought down by name with the
    compose file of the folder it is being re-pointed at. Then docker must no longer list it."""
    compose_dir, extra = old, ()
    if not os.path.isfile(os.path.join(old, "docker-compose.yml")):
        if new and os.path.isfile(os.path.join(new, "docker-compose.yml")):
            compose_dir, extra = new, ("-p", project)
        else:
            raise AgentError(409, "The docker project %s is running, but %s no longer holds its docker-compose.yml -- "
                                  "stop it by hand (docker compose -p %s down) and retry." % (project, old, project))
    # strict: 507 before the down -- a stack in transition must never look "supposed to be up" to the watchdog
    version_state_set(version, strict=True, runningOk=False)
    job.log("Stopping version %s (docker compose down of project %s -- every grace period is waited out so the "
            "game server saves the players' data) ..." % (version, project))
    # (a failure below has stopped the server, or part of it: "nothing was moved" -- do_relocate starts it
    # again when it was running)
    rc = _compose_stream(compose_dir, job, *extra, "down", "--remove-orphans", prefix="  ")
    if rc != 0:
        raise AgentError(500, "docker compose down failed (code %d) -- nothing was moved." % rc)
    projects, err = running_projects()
    if err is not None:
        raise AgentError(500, "Cannot read the docker state after the down (%s) -- nothing was moved; retry." % err)
    if project in projects:
        raise AgentError(500, "docker still lists the %s project after the down -- nothing was moved. Stop it in "
                              "Docker and retry." % project)
    job.log("Version %s is down." % version)


def _relocate_move_tree(version, job, old, new, was_up):
    """move: the stopped stack's tree from old to new. Returns (cross_volume, bytes copied or None). On
    a failure or a stop request nothing is left behind -- the temp copy removed, an empty target folder
    the admin made put back, created parents pruned --, the stack is started again where it was when it
    was running, and the job fails (a stop: 409 "Stopped on request")."""
    parent = os.path.dirname(new)
    target_existed = os.path.isdir(new)  # validated: absent or empty
    created, tmp = [], None
    try:
        created = _makedirs_tracked(parent)
        if _same_volume(old, new):
            if target_existed:
                os.rmdir(new)
            job.log("Moving %s -> %s (the same drive: one rename) ..." % (old, new))
            try:
                _rename_retry(old, new, should_stop=_relocate_cancel.is_set)
                return False, None
            except OSError as e:
                if not _cross_device(e):
                    raise
                job.log("The rename crosses a volume boundary after all (%s) -- copying instead." % e)
        files, folders, total = _tree_counts(old)
        free = _free_bytes_at(parent)
        if free is not None and free < total + FETCH_DISK_RESERVE:
            raise AgentError(409, "Not enough free disk space on the target drive: %.1f GB needed (%.1f GB of server "
                                  "files + %d MiB reserve), %.1f GB free at %s."
                             % ((total + FETCH_DISK_RESERVE) / 1e9, total / 1e9, FETCH_DISK_RESERVE >> 20,
                                free / 1e9, parent))
        tmp = os.path.join(parent, ".relic-move-" + os.path.basename(new))
        _rmtree_retry(tmp)  # the leftover of a run that died half-way
        job.log("Copying %d files (%.1f GB) to the other drive into %s -- the old copy stays until the new one is "
                "verified and the configuration names it ..." % (files, total / 1e9, tmp))
        _copy_tree(old, tmp, job, _relocate_cancel.is_set, total)
        # The copy must be ON DISK before the configuration names it and the old tree is deleted: cp -a and the
        # page cache leave it dirty for up to ~30 s (delayed allocation), and a power cut then would leave the
        # new database files empty with the old ones already gone. (Windows: each file was fsync'ed.)
        job.log("Flushing the copy to disk ...")
        _sync_filesystems()
        got = _tree_counts(tmp)
        if got != (files, folders, total):
            raise OSError("the copy does not match the original (%d files, %d folders, %d bytes; expected %d, %d, %d)"
                          % (got + (files, folders, total)))
        job.log("Copy verified: %d files, %d folders, %d bytes." % (files, folders, total))
        if os.path.isdir(new):
            os.rmdir(new)
        _rename_retry(tmp, new)
        tmp = None
        _fsync_dir(parent)  # the rename durable too, before the config write that follows
        return True, total
    except BaseException as e:
        if tmp is not None:
            _rmtree_quiet(tmp)
        if target_existed and not os.path.lexists(new):
            try:
                os.mkdir(new)
            except OSError:
                pass
        _prune_dirs(created)
        if isinstance(e, RelocateStopped) or (isinstance(e, OSError) and _relocate_cancel.is_set()):
            if was_up:
                _relocate_restart(version, job)
            raise AgentError(409, "Stopped on request -- nothing was moved; %s stays the folder of version %s."
                             % (old, version))
        if isinstance(e, (AgentError, OSError)):
            if was_up:
                _relocate_restart(version, job)
            if isinstance(e, AgentError):
                raise
            raise AgentError(500, "Moving %s to %s failed (%s) -- nothing was moved; the server stays in %s."
                             % (old, new, e, old))
        raise


def _relocate_siblings(old, new, job):
    """After a move: what a download left beside the old folder follows it -- <old>.7z (a verified
    archive a retry adopts) and <old>.7z.part (a resumable download) become <new>.7z(.part) --, and a
    stale extraction folder of the old name is removed. Best effort, logged."""
    old_s, new_s = old.rstrip("/\\"), new.rstrip("/\\")
    for suffix in (".7z", ".7z.part"):
        src, dst = old_s + suffix, new_s + suffix
        if not os.path.isfile(src):
            continue
        if os.path.lexists(dst):
            job.log("  %s is already there -- %s left where it is." % (dst, src))
            continue
        try:
            try:
                os.rename(src, dst)
            except OSError as e:
                if not _cross_device(e):
                    raise
                try:
                    _copy_file_durable(src, dst)  # on disk before the original goes
                except OSError:
                    _remove_quiet(dst)
                    raise
                os.remove(src)
            job.log("  moved %s -> %s" % (src, dst))
        except OSError as e:
            job.log("  WARNING: could not move %s (%s) -- move or delete it by hand." % (src, e))
    stale = os.path.join(os.path.dirname(os.path.abspath(old_s)), ".relic-extract-" + os.path.basename(old_s))
    if os.path.isdir(stale):
        try:
            _rmtree_retry(stale)
            job.log("  removed the stale extraction folder %s" % stale)
        except OSError as e:
            job.log("  WARNING: could not remove %s (%s) -- delete it by hand." % (stale, e))


def relocated_version_state(vs):
    """The record a version keeps when it is re-pointed at ANOTHER server's folder (see _RELOCATE_DROP):
    the admin's decisions only, runningOk off, an enabled hotpatch pending again. Pure."""
    out = {"runningOk": False}
    if "pathfinding" in vs:
        out["pathfinding"] = vs["pathfinding"]
    hp = vs.get("hotpatch")
    if isinstance(hp, dict):
        kept = {k: hp[k] for k in _RELOCATE_HOTPATCH_KEEP if k in hp}
        kept["pending"] = bool(hp.get("enabled"))
        out["hotpatch"] = kept
    return out


def _repoint_state(version, new, job):
    """repoint: the version's record follows the folder only when the folder IS the same server -- its
    .relic-provisioned says what the state's provisionedAt says (the tree was moved by hand). Otherwise
    it starts over (relocated_version_state), passwordVerify is read from the new stack's sdk config (a
    signup must know whether a password is enforced), and the marker logic fills the provisioning record
    in from the new folder. Runs after VERSIONS was swapped. Returns True when the record was reset."""
    marker = marker_at(new)
    with _state_lock:
        try:
            data = load_state()
        except StateUnreadable:
            raise state_unreadable_error()
        versions = data.setdefault("versions", {})
        vs = versions.get(version) or {}
        at = vs.get("provisionedAt")
        if at and marker is not None and str(marker["at"]) == str(at):
            job.log("%s holds the same server (its %s says provisioned %s, as the agent's record does) -- the "
                    "version's record is kept." % (new, PROVISIONED_MARKER, at))
            return False
        rec = relocated_version_state(vs)
        pv = sdk_password_verify_setting(version)
        if pv is not None:
            rec["passwordVerify"] = pv
        no_record = marker is None and is_bootstrapped(version)  # VERSIONS already names the new folder
        if no_record:
            # An installed server this agent holds no provisioning record of (a vendor bootstrap.sh stack, a
            # copy made before agent 3.2): its database may hold players, and nothing could re-seed the record
            # -- the next Start (provision "auto") would import the shipped save over it. Only an explicit
            # Provision clears this (do_provision); needs_provision honours it.
            rec["autoProvision"] = False
        versions[version] = rec
        try:
            save_state(data, strict=True)
        except AgentError as e:
            raise AgentError(507, "Version %s now uses %s, but the agent state could not be updated (%s) -- it still "
                                  "describes the previous server. Fix the state folder and re-point again."
                             % (version, new, e.message), code="state_write_failed")
    if marker is not None:
        job.log("%s is another server than the one on record -- the version's record starts over from its %s "
                "(provisioned %s, progress %s); the admin's choices (pathfinding, hotpatch) are kept."
                % (new, PROVISIONED_MARKER, marker["at"], marker["progress"]))
    elif no_record:
        job.log("%s holds an installed server but no provisioning record of this agent -- the version's record "
                "starts over; the admin's choices (pathfinding, hotpatch) are kept. Automatic provisioning is OFF "
                "for it: no Start imports the save over that database -- only an explicit Provision (Re-apply GAA "
                "progress) touches it." % new)
    else:
        job.log("%s holds no provisioning record of this agent -- the version's record starts over; the admin's "
                "choices (pathfinding, hotpatch) are kept." % new)
    return True


def do_relocate(version, job, raw_dir, mode):
    """POST /server/relocate (job `relocate`, under _op_lock) -- see the section comment. Returns {version,
    from, to, mode, moved, crossVolume, bytes, restarted, stateReset, restartError}."""
    _relocate_cancel.clear()
    require_state_readable()
    rep, err = relocate_check(version, raw_dir, mode, in_job=True, measure=False)
    if err is not None:
        raise err
    old, new = rep["from"], rep["to"]
    key = dir_setting_key(version)
    project = VERSIONS[version]["project"]
    was_up = bool(rep["running"])
    job.log("%s version %s: %s -> %s." % ("Moving" if mode == "move" else "Re-pointing", version,
                                          old or "(not configured)", new or "(not configured)"))
    # 1. down under the OLD project name (a stack merely installed too: its stopped containers must go)
    if old and (was_up or is_bootstrapped(version)):
        try:
            _relocate_down(version, job, old, new, project, was_up)
        except AgentError as e:
            # a 409 / 507 is raised before anything is stopped; a failed down (500) may have stopped all or part
            # of a running server, with runningOk already off -- no watchdog would bring it back
            if e.status != 500 or not was_up:
                raise
            started, rerr = _relocate_restart(version, job)
            raise AgentError(500, "%s The server had been stopped for the move; %s." % (
                e.message, "it was started again" if started
                else "it did not start again (%s) -- start it from the Server page" % rerr))
    if _relocate_cancel.is_set():
        if was_up:
            _relocate_restart(version, job)
        raise AgentError(409, "Stopped on request -- nothing was moved or changed.")
    # 2. the files (move only)
    moved = cross = False
    nbytes = None
    if mode == "move":
        cross, nbytes = _relocate_move_tree(version, job, old, new, was_up)
        moved = True
    # 3. the config file BEFORE anything old is deleted: a restart must find the server where it is
    try:
        save_config_values({key: new}, explicit_empty=(key,))
    except AgentError as e:
        undo = "nothing had been moved"
        if moved and not cross:
            try:
                _rename_retry(new, old)
                undo = "the folder was moved back to %s" % old
            except OSError as e2:
                raise AgentError(500, "Saving %s failed (%s), and moving the folder back failed too (%s): the server "
                                      "is now in %s while the configuration still names %s -- set %s=%s in %s by hand "
                                      "and restart the agent." % (key, e.message, e2, new, old, key, new, CONFIG_PATH))
        elif moved:
            _rmtree_quiet(new)
            undo = "the copy was deleted, %s is untouched" % old
        if was_up:
            _relocate_restart(version, job)
        raise AgentError(500, "Saving %s in %s failed (%s) -- %s." % (key, CONFIG_PATH, e.message, undo))
    # 4. this process follows: one assignment (status() and the watchdogs read VERSIONS without a lock)
    set_version_dir(version, new)
    job.log("Configuration saved: %s=%s. Version %s %s." % (
        key, new, version, ("now uses %s (docker project %s)" % (new, VERSIONS[version]["project"])) if new
        else "is no longer configured on this agent (its files are untouched)"))
    # 5. repoint: whose record is it?
    state_reset = _repoint_state(version, new, job) if mode == "repoint" and new else False
    # 6. what a download left beside the old folder, and the old copy of a cross-volume move
    if moved:
        _relocate_siblings(old, new, job)
    if cross:
        job.log("Deleting the old copy %s ..." % old)
        try:
            _rmtree_retry(old)
            job.log("  deleted.")
        except OSError as e:
            job.log("WARNING: the old copy %s could not be deleted completely (%s) -- delete it by hand; the server "
                    "runs from %s now." % (old, e, new))
    # 7. heal what a hand-extracted or copied tree may lack
    if new and is_present(version):
        ensure_data_layout(version, job)
        ensure_exec_bits(version, job)
        creds = os.path.join(new, "creds.txt")
        if IS_WINDOWS and os.path.isfile(creds):
            protect_path(creds, 0o600)  # a copied file inherits the new folder's ACL
    # 8. back up where it was running
    restarted, restart_err = False, None
    if was_up:
        if new and is_present(version) and is_bootstrapped(version):
            restarted, restart_err = _relocate_restart(version, job)
        else:
            job.log("Version %s was running; %s, so it stays down -- %s." % (
                version, ("%s holds no installed server" % new) if new else "it has no folder any more",
                "Prepare the server there" if new else "point it at a folder again to use it"))
    elif new:
        job.log("Version %s was not running -- it stays down." % version)
    return {"version": version, "from": old, "to": new, "mode": mode, "moved": moved, "crossVolume": cross,
            "bytes": nbytes, "restarted": restarted, "stateReset": state_reset, "restartError": restart_err}


# -- HTTP --

class Handler(BaseHTTPRequestHandler):
    server_version = "gio-agent/" + AGENT_VERSION
    timeout = 20  # socket timeout: a client that stalls mid-request must not park a thread forever

    def _authed(self):
        # compare_digest: no timing side-channel now that the agent may listen on the LAN.
        # Compare BYTES, not str: http.server decodes headers as latin-1, so a non-ASCII byte in the
        # Authorization header would make compare_digest(str, str) raise TypeError and crash the
        # handler thread. Encoding both sides to bytes accepts any client input and just returns False.
        if not TOKEN:
            return False
        got = self.headers.get("Authorization", "").encode("latin-1", "replace")
        return hmac.compare_digest(got, f"Bearer {TOKEN}".encode("utf-8"))

    def _send(self, code, obj, retry_after=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(int(retry_after)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, e):
        obj = {"error": e.message}
        if e.code:
            obj["code"] = e.code
        if e.retry_after is not None:
            obj["retryAfter"] = int(e.retry_after)
        return self._send(e.status, obj, retry_after=e.retry_after)

    def _read_json(self, max_body=MAX_BODY):
        raw = read_http_body(self.rfile, self.headers, max_body=max_body)
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            raise AgentError(400, "invalid request body (not JSON)", code="bad_request")
        return parsed if isinstance(parsed, dict) else {}

    def _client_ip(self):
        ip = self.client_address[0]
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                ip = xff.split(",")[0].strip() or ip
        return ip

    def _job_reply(self, kind, version, fn, lock=True):
        check_known(version)
        job = start_job(kind, version, fn, lock=lock)
        return self._send(202, {"ok": True, "async": True, "job": job.id,
                                "kind": kind, "version": version})

    # -- public (token-less) --

    def _public_get(self, path, query):
        ip = self._client_ip()
        try:
            if path == "/public/status":
                enforce_limits([("status:" + ip, 30, 60, "rate_limited")])
                return self._send(200, public_status())
            if path.startswith("/public/jobs/"):
                enforce_limits([("jobs:" + ip, 60, 60, "rate_limited")])
                job = public_job(path[len("/public/jobs/"):])
                if job is None:
                    return self._send(404, {"error": "unknown job", "code": "not_found"})
                return self._send(200, job.public_snapshot())
        except AgentError as e:
            return self._send_error(e)
        except Exception as e:  # noqa: BLE001
            log_line("gio-agent: public GET %s from %s failed: %s" % (path, ip, e))
            return self._send(500, {"error": "server error, try later", "code": "server_error"})
        return self._send(404, {"error": "not found", "code": "not_found"})

    def _public_post(self, path):
        ip = self._client_ip()
        try:
            # Body BEFORE anything else and capped small: a token-less caller gets 4 KB, period.
            b = self._read_json(max_body=PUBLIC_MAX_BODY)
            if path == "/public/account/create":
                return self._public_create(b, ip)
            if path == "/public/command":
                return self._public_command(b, ip)
        except AgentError as e:
            return self._send_error(e)
        except KeyError as e:
            return self._send(400, {"error": f"missing/invalid field: {e}", "code": "bad_request"})
        except Exception as e:  # noqa: BLE001
            log_line("gio-agent: public POST %s from %s failed: %s" % (path, ip, e))
            return self._send(500, {"error": "server error, try later", "code": "server_error"})
        return self._send(404, {"error": "not found", "code": "not_found"})

    def _public_create(self, b, ip):
        policy = get_policy()
        if not policy["signup"]["enabled"]:
            raise AgentError(403, "account creation is disabled on this server", code="signup_disabled")
        version = str(b.get("version", "")).strip()
        check_known(version)
        vp = (policy["signup"]["versions"] or {}).get(version)
        if vp is None:
            raise AgentError(403, "account creation is disabled for this version", code="signup_disabled_version")
        name = str(b.get("name", "")).strip()
        template = str(b.get("template", "fresh")).strip() or "fresh"
        password = b.get("password")
        why = validate_account_name(name, reserved_names(version))
        if why:
            raise AgentError(400, why[1], code=why[0])
        if template not in (vp.get("templates") or []):
            raise AgentError(400, "that template is not available on this server", code="template_unavailable")
        if template != "fresh" and not template_ready(version, template):
            raise AgentError(503, "that progress template is not ready on this server", code="template_not_ready")
        if version_state(version).get("passwordVerify"):
            if not (isinstance(password, str) and PASSWORD_RE.match(password)):
                raise AgentError(400, "a password of 8-64 printable characters is required", code="password_required")
        elif password is not None and password != "" and \
                not (isinstance(password, str) and PASSWORD_RE.match(password)):
            raise AgentError(400, "the password must be 8-64 printable characters", code="password_invalid")
        # agent 3.7: this launcher's accounts on the version, before anything is spent (the job checks
        # again under the job slot, and counts the account it creates).
        client = signup_client_key(b.get("client"), ip)
        check_player_limit(version, client, vp)
        # The cached snapshot is free -- a version that is down must not burn the caller's quota.
        snap = public_status()
        vs = (snap.get("versions") or {}).get(version) or {}
        if not vs.get("up"):
            raise AgentError(503, "this server version is not running right now", code="version_down")
        # Quota fairness: a refusal that did no work must not cost the caller a signup token.
        # (a) The job slot first. server_busy is the one 429 a launcher retries on its own, and each
        #     of those retries used to spend a create token.
        if current_job(skip_yielding=True) is not None:
            raise public_busy_error()
        # (b) "Name taken" BEFORE the quota, under its own small budget (namecheck_rules): the budget
        #     bounds how fast this endpoint can enumerate names (the vendor register page on :21000
        #     answers "already exists" anyway). A spent budget never refuses a signup: it falls back
        #     to the old order -- quota first, lookup after -- and a name taken on that path is NOT
        #     refunded, or take + refund would be an unlimited name oracle.
        quota = signup_quota_rules(ip, version, vp)

        def lookup():
            # A missing/relocated sdk.db raises a code-less 500 naming the stack's path: log it, answer
            # the player a coded, path-free server_error (still refunded below on the fallback path).
            try:
                return _sdk_db_lookup(version, name)
            except AgentError as e:
                if e.status < 500 or e.code:
                    raise
                log_line("gio-agent: public signup lookup failed: %s" % e.message)
                raise AgentError(500, "server error, try later", code="server_error")

        prechecked = LIMITER.take_all(namecheck_rules(ip))[0]
        if prechecked and lookup() is not None:
            raise AgentError(409, "name taken", code="name_taken")
        # (c) All four create buckets or none.
        enforce_limits(quota)
        try:
            # (d) The fallback lookup.
            if not prechecked and lookup() is not None:
                raise AgentError(409, "name taken", code="name_taken")
            # (e) A job can have taken the slot since (a): that race is refunded below. The job hands
            #     the quota back itself when it fails before registering -- only for a request the
            #     namecheck budget admitted, and never for version_down (its docker exec probe holds
            #     the slot for up to 30 s, see do_signup).
            job = start_public_job("signup", version,
                                   lambda j: do_signup(version, j, name, password, template, ip,
                                                       refund=quota if prechecked else None,
                                                       client=client))
        except Exception as e:
            if not (isinstance(e, AgentError) and e.code == "name_taken"):
                LIMITER.give(quota)  # nothing ran: a busy race, or an unreadable account database
            raise
        log_line("gio-agent: public signup from %s -> job %s (name=%s version=%s template=%s)"
                 % (ip, job.id, name, version, template))
        return self._send(202, {"ok": True, "job": job.id})

    def _public_command(self, b, ip):
        if not get_policy().get("playerCommands"):
            raise AgentError(403, "player commands are disabled on this server", code="commands_disabled")
        enforce_limits([("cmd:" + ip, 30, 60, "rate_limited"), ("cmd:*", 300, 60, "rate_limited_server")])
        version = str(b.get("version", "")).strip()
        check_known(version)
        snap = public_status()
        if not ((snap.get("versions") or {}).get(version) or {}).get("up"):
            raise AgentError(503, "this server version is not running right now", code="version_down")
        uid, msg = str(b["uid"]).strip(), str(b["msg"])
        if not uid.isdigit() or len(msg) > 512:
            raise AgentError(400, "uid must be numeric and msg at most 512 characters", code="command_invalid")
        log_line("gio-agent: public command from %s: version=%s uid=%s msg=%s" % (ip, version, uid, msg[:120]))
        res = gm_command(version, uid, msg)
        return self._send(200, {"ok": True, "response": res})

    # -- the hotpatch mirror (token-less static files in the CDN layout) --

    def _hotpatch_miss(self, rel, local, ip):
        """A cached file is missing: fetch it once from the official CDN when allowed
        (hotpatch_miss_policy: a listed file, or an unlisted one under an ADVERTISED output only),
        within the per-address + global miss quotas, at most HOTPATCH_ONDEMAND_PARALLEL transfers
        at once (503 + Retry-After when busy) and, for unlisted files, the HOTPATCH_ONDEMAND_MAX byte
        budget. Anything else is a plain 404. Returns True when served. Waits are bounded and sized
        for the game's downloader (2.8: no HEAD, 30 s per GET, 6 tries 3 s apart): a request for a
        file that is ALREADY being fetched waits for that fetch without taking a transfer slot, and
        a voice pack waits up to 20 s for a slot -- an instant 503 would burn the six tries in 15 s.
        The anonymous caller only ever sees "not found" / "upstream unavailable": the upstream
        detail (resolver, proxy, CDN hosts) goes to the log."""
        pol = hotpatch_miss_policy(rel)  # first: it also counts the voice packs players ask for in vain
        if not hotpatch_upstream_enabled():
            return False
        if pol is None:
            if hotpatch_voice_path(rel) is None:  # (a refused voice pack has its own, hourly line)
                log_line("gio-agent: hotpatch miss %s from %s: not a listed file nor under an advertised "
                         "output -- not fetched" % (_log_rel(rel), ip))
            return False
        man, expect, cap = pol
        if expect is not None and expect.get("voice"):
            free = _mirror_free_bytes()
            if free is not None and free < int(expect["size"]) + HOTPATCH_DISK_RESERVE:
                log_line("gio-agent: hotpatch miss %s from %s: only %d MiB free on the mirror's volume "
                         "(GIO_HOTPATCH_DISK_RESERVE keeps %d) -- not fetched"
                         % (_log_rel(rel), ip, free >> 20, HOTPATCH_DISK_RESERVE >> 20))
                return False
        enforce_limits([("hotpatch-miss:" + ip, 120, 60), ("hotpatch-miss:*", 60, 60)], limiter=HOTPATCH_LIMITER)
        if expect is None:
            used = ondemand_bytes()
            if used >= HOTPATCH_ONDEMAND_MAX:
                log_line("gio-agent: hotpatch miss %s from %s: the on-demand budget is used up (%d of %d MiB, "
                         "GIO_HOTPATCH_ONDEMAND_MAX) -- not fetched" % (_log_rel(rel), ip, used >> 20,
                                                                        HOTPATCH_ONDEMAND_MAX >> 20))
                return False
            cap = min(cap, HOTPATCH_ONDEMAND_MAX - used)
        voice = bool(expect and expect.get("voice"))
        lk = _path_lock(rel)
        if lk.locked():  # this very file is being fetched (the same player's retry, a second player)
            if lk.acquire(timeout=HOTPATCH_WAIT):
                lk.release()
            if os.path.isfile(local):
                return True
            raise AgentError(503, "mirror busy, try again later", retry_after=3)
        if not (_hotpatch_ondemand_sem.acquire(timeout=20) if voice
                else _hotpatch_ondemand_sem.acquire(blocking=False)):
            raise AgentError(503, "mirror busy, try again later", retry_after=5)
        try:
            if voice and hotpatch_voice_expect(rel) is None:
                return False  # deselected (and maybe purged) while this request waited for its slot
            log_line("gio-agent: hotpatch miss %s from %s -- fetching from the upstream%s"
                     % (_log_rel(rel), ip, "" if expect else " (unlisted, %d MiB cap)" % (cap >> 20)))
            try:
                n = hotpatch_fetch(man.get("upstreams") or [], rel, expect=expect, cap=cap, resume=voice)
            except AgentError as e:
                if e.status == 507:
                    log_line("gio-agent: hotpatch miss %s: %s" % (_log_rel(rel), e.message))
                    raise AgentError(503, "mirror busy, try again later", retry_after=60)
                if "HTTP 404" in e.message:
                    log_line("gio-agent: hotpatch miss %s: not on the upstream either" % _log_rel(rel))
                    return False  # the CDN has no such file: a plain 404, not an error
                log_line("gio-agent: hotpatch miss %s: upstream failed: %s" % (_log_rel(rel), e.message))
                raise AgentError(502, "upstream unavailable")
            if expect is None:
                ondemand_bytes(add=n)
        finally:
            _hotpatch_ondemand_sem.release()
        return os.path.isfile(local)

    def _serve_hotpatch(self, raw, head_only):
        ip = self._client_ip()
        local = None
        sent_headers = False  # once the status line is out, no error may be written into the body
        try:
            enforce_limits([("hotpatch:" + ip, 600, 60)], limiter=HOTPATCH_LIMITER)
            safe = safe_hotpatch_path(raw)
            if safe is None:
                return self._send(404, {"error": "not found"})
            rel, local = safe
            if not os.path.isfile(local):
                log_line("gio-agent: hotpatch miss %s from %s" % (_log_rel(rel), ip))
                if not self._hotpatch_miss(rel, local, ip):
                    return self._send(404, {"error": "not found"})
            st = os.stat(local)
            size = st.st_size
            rng = parse_range(self.headers.get("Range"), size)
            start, end = rng if rng else (0, size - 1)
            length = max(0, end - start + 1)
            sent_headers = True
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=86400")
            if rng:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if head_only or length == 0:
                return None
            with open(local, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(HOTPATCH_SEND_CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return None
        except AgentError as e:
            if sent_headers:
                return None
            if e.status == 416 and local is not None:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % os.path.getsize(local))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send_error(e)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            return None  # the client went away / stalled mid-transfer -- nothing to report
        except Exception as e:  # noqa: BLE001
            log_line("gio-agent: hotpatch GET %s from %s failed: %s" % (_log_rel(raw or ""), ip, e))
            if sent_headers:
                return None  # the body is already declared: a JSON error would corrupt the file
            try:
                return self._send(500, {"error": "server error, try later"})
            except OSError:
                return None

    def do_HEAD(self):
        path = self.path.partition("?")[0]
        if path.startswith("/hotpatch/"):
            return self._serve_hotpatch(path[len("/hotpatch/"):], head_only=True)
        self._send(404, {"error": "not found"})

    # -- admin --

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            return self._send(200, {"ok": True, "service": "gio-agent", "version": AGENT_VERSION})
        if path.startswith("/public/"):
            return self._public_get(path, query)
        if path.startswith("/hotpatch/"):
            return self._serve_hotpatch(path[len("/hotpatch/"):], head_only=False)
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        qs = urllib.parse.parse_qs(query)
        try:
            if path == "/status":
                return self._send(200, status())
            if path == "/server/hotpatch":
                return self._send(200, hotpatch_info(qs.get("version", [""])[0]))
            if path == "/jobs":
                with _jobs_lock:
                    jobs = [JOBS[j].snapshot(10 ** 9) for j in JOBS_ORDER]
                return self._send(200, {"jobs": jobs})
            if path.startswith("/jobs/"):
                job = JOBS.get(path[len("/jobs/"):])
                if job is None:
                    return self._send(404, {"error": "no such job (was the agent restarted?)"})
                since = qs.get("since", ["0"])[0]
                return self._send(200, job.snapshot(since))
            if path == "/server/account/policy":
                return self._send(200, get_policy())
            if path == "/server/templates":
                return self._send(200, templates_report(qs.get("version", [""])[0]))
            if path == "/server/secrets":
                return self._send(200, secrets_info(qs.get("version", [""])[0]))
            if path == "/agent/config":
                return self._send(200, agent_config_info())
        except AgentError as e:
            return self._send_error(e)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        path = self.path.partition("?")[0]
        if path == "/server/account/policy":
            return self.do_POST()
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path.startswith("/public/"):
            return self._public_post(path)
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            # Inside the try: a malformed body is an AgentError with a real status, not a traceback
            # on the handler thread.
            b = self._read_json()
            if path == "/server/start":
                v, mode = b["version"], str(b.get("provision", "auto")).lower()
                return self._job_reply("start", v, lambda j: do_start(v, j, provision=mode))
            if path == "/server/stop":
                v = b["version"]
                return self._job_reply("stop", v, lambda j: do_stop(v, j))
            if path == "/server/setup":
                v, txt, force = b["version"], b.get("txtFixes"), bool(b.get("force"))
                prog = parse_progress(b.get("progress"))  # 400 here, before a job exists
                pf = b.get("pathfinding")  # bool | null (omitted = the stored decision, else the default)
                if pf is not None and not isinstance(pf, bool):
                    raise AgentError(400, "pathfinding must be true or false")
                return self._job_reply("setup", v, lambda j: do_setup(v, j, txt_fixes=txt, force=force,
                                                                      progress=prog, pathfinding=pf))
            if path == "/server/provision":
                v, txt = b["version"], b.get("txtFixes")
                prog = parse_progress(b.get("progress"))
                return self._job_reply("provision", v, lambda j: do_provision(v, j, txt_fixes=txt, progress=prog))
            if path == "/server/pathfinding":
                v, on = b["version"], b["enabled"]
                if not isinstance(on, bool):
                    raise AgentError(400, "enabled must be true or false")
                return self._job_reply("pathfinding", v, lambda j: do_pathfinding(v, j, on))
            if path == "/server/txtfixes":
                v, action = b["version"], str(b.get("action", "apply")).lower()
                return self._job_reply("txtfixes", v, lambda j: do_txt_fixes(v, j, action=action))
            if path == "/server/events":
                v = b["version"]
                return self._job_reply("events", v, lambda j: do_events(v, j))
            if path == "/server/netfix":
                v = b["version"]
                return self._job_reply("netfix", v, lambda j: do_netfix(v, j))
            if path == "/server/account/copy":
                v = b["version"]
                src_a, dst_a = str(b["from"]).strip(), str(b["to"]).strip()
                dry = bool(b.get("dryRun"))
                return self._job_reply("accountcopy", v,
                                       lambda j: do_account_copy(v, j, src_a, dst_a, dry_run=dry))
            if path == "/server/account/create":
                # agent 3.5: the malformed request is a 400 here, before a job exists (never a 404 --
                # that means "no such route, an older agent" to the launcher). An admin-started job:
                # it waits (409) while a yielding job runs, like every other one.
                v, name, pw, tpl = admin_create_preflight(b)
                return self._job_reply("accountcreate", v, lambda j: do_account_create(v, j, name, pw, tpl))
            if path == "/server/account/policy":
                return self._send(200, set_policy(b))
            if path == "/agent/state/reset":
                return self._send(200, reset_state(b.get("confirm"), force=b.get("force") is True))
            if path == "/agent/config":
                return self._send(200, set_agent_config(b))
            if path == "/agent/restart":
                how = request_restart()  # the 409s, then no job may start any more
                try:
                    self._send(202, {"ok": True, "restarting": True, "how": how})
                finally:
                    _launch_restart(how)  # even when the reply could not be written: the flag is set
                return
            if path == "/server/templates/ensure":
                v = b["version"]
                return self._job_reply("templates", v, lambda j: do_templates_ensure(v, j))
            if path == "/server/secrets":
                v = b["version"]
                vals = {k: b.get(k) for k in SECRET_KNOBS}
                return self._job_reply("secrets", v, lambda j: do_secrets(v, j, vals))
            if path == "/server/auth":
                v, verify = b["version"], b["verifyPassword"]
                if not isinstance(verify, bool):
                    raise AgentError(400, "verifyPassword must be true or false")
                dp = b.get("defaultPassword")
                return self._job_reply("auth", v, lambda j: do_auth(v, j, verify, default_password=dp))
            if path == "/server/account/password":
                v, name, pw = b["version"], str(b["name"]), b["password"]
                return self._job_reply("password", v, lambda j: do_account_password(v, j, name, pw))
            if path == "/server/hotpatch":
                v, on, purge = b["version"], b["enabled"], bool(b.get("purge"))
                if not isinstance(on, bool):
                    raise AgentError(400, "enabled must be true or false")
                return self._job_reply("hotpatch", v, lambda j: do_hotpatch(v, j, on, purge=purge))
            if path == "/server/hotpatch/voice" and b.get("cancel") is True:
                # Not a job: asks the running voice job of this version to stop after the chunk it
                # is writing (its .part stays, a later run resumes it).
                v = b["version"]
                check_known(v)
                running = running_job_of_kind("hotpatch-voice")
                stopping = running is not None and running.version == v
                if stopping:
                    _voice_cancel.set()
                return self._send(200, {"ok": True, "stopping": stopping})
            if path == "/server/fetch" and b.get("cancel") is True:
                # Same shape for the server package download: the .part stays, the archive too.
                v = b["version"]
                check_known(v)
                running = running_job_of_kind("fetch")
                stopping = running is not None and running.version == v
                if stopping:
                    _fetch_cancel.set()
                return self._send(200, {"ok": True, "stopping": stopping})
            if path == "/server/fetch":
                v, force = b["version"], bool(b.get("force"))
                fetch_preflight(v, force)  # the synchronous refusals (404/409/500) before a job exists
                return self._job_reply("fetch", v, lambda j: do_fetch(v, j, force=force),
                                       lock=False)  # it holds _op_lock for its prepare + finish phases only
            if path == "/server/relocate" and b.get("cancel") is True:
                # Same shape as the fetch's: a cross-volume copy stops between two files (cp -a is killed),
                # the temp copy goes, the server stays where it was (and is started again if it ran).
                v = b["version"]
                check_known(v)
                running = running_job_of_kind("relocate")
                stopping = running is not None and running.version == v
                if stopping:
                    _relocate_cancel.set()
                return self._send(200, {"ok": True, "stopping": stopping})
            if path == "/server/relocate":
                # agent 3.6: {version, dir, mode: move|repoint[, check: true]}. `dir` is required -- an
                # omitted one must never read as "" (= take the version off this agent).
                v, raw_dir, mode = b["version"], b["dir"], b.get("mode")
                if not isinstance(v, str):
                    raise AgentError(400, "version must be a string")
                mode = mode.strip().lower() if isinstance(mode, str) else ""
                rep, refusal = relocate_check(v, raw_dir, mode)  # 400 unknown version / mode
                if b.get("check") is True:
                    return self._send(200, rep)  # the dry run: every refusal is the report's `problem`
                if refusal is not None:
                    raise refusal
                return self._job_reply("relocate", v, lambda j: do_relocate(v, j, raw_dir, mode))
            if path == "/server/hotpatch/voice":
                v, langs, purge = b["version"], b["languages"], bool(b.get("purge"))
                # Names only ever select entries of the verified index -- never a path component.
                if not isinstance(langs, list) or len(langs) > VOICE_LANGUAGES_MAX or not all(
                        isinstance(x, str) and VOICE_PACK_RE.match(x + "/x.pck") for x in langs):
                    raise AgentError(400, "languages must be a list of voice language names")
                check_known(v)
                return self._job_reply("hotpatch-voice", v, lambda j: do_hotpatch_voice(v, j, langs, purge=purge),
                                       lock=False)  # it holds _op_lock for its prepare phase only
            if path == "/command":
                res = gm_command(b["version"], b["uid"], b["msg"])
                return self._send(200, {"ok": True, "response": res})
        except AgentError as e:
            return self._send_error(e)
        except KeyError as e:
            return self._send(400, {"error": f"missing/invalid field: {e}"})
        except Exception as e:  # noqa: BLE001 -- report any failure as 500 JSON
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def log_message(self, *_):  # keep the journal quiet
        pass


def selftest():
    import io
    import tempfile
    global BIND_IP, ADVERTISED_IP, ADVERTISED_HOST, IS_WINDOWS, CONFIG_FILE
    global HOTPATCH_DIR, HOTPATCH_URL, PAYLOAD_DIR, STATE_PATH, LISTEN_HOST, LISTEN_PORT
    global HOTPATCH_BUNDLE_DIR, HOTPATCH_BUNDLES, HOTPATCH_DISK_RESERVE, REGION
    global running_projects, _fetch_to_file, mysql_exec, run_sql, _compose_stream, verify_stack, mysql_reachable
    checks = 0
    # install_agent.sh runs the selftest with the box's stored GIO_* exported: a configured DDNS host
    # would make every "no advertised address" case below reachable. The checks set it themselves.
    ADVERTISED_HOST = ""
    # Same for a configured mirror URL (the Agent settings card can set it since 3.6): with one, every
    # "no reachable mirror URL" case below would answer that URL. The checks that want one set it.
    HOTPATCH_URL = ""
    # And the box's free-disk reserve: the hotpatch blocks below run the REAL guard against the REAL
    # free space of the volume that holds the temp mirror, so a reserve the admin raised from the
    # Agent settings card -- or a box with less than 2 GiB free under TMPDIR -- refused a 1600-byte
    # voice job and aborted the whole upgrade. The fetch block already does this with its own twin.
    HOTPATCH_DISK_RESERVE = 1 << 20
    # Same for the box's MUIP region (free text in install_agent.sh's prompt): a value holding "&"
    # would add split points to the signed query the signing check below takes apart.
    REGION = "selftest_region"
    # For the same reason the box's STATE FILE is not an input: GIO_STATE_PATH is exported too, so
    # every check that reads a record used to read the RUNNING agent's history. That is how the
    # upgrade of a real box died on 2026-09-20 -- `secrets_info` reported the stack's vendor key
    # together with `muipSource: "random"` out of the live state, the check that expects a fresh
    # record failed, and install_agent.sh (which runs --selftest before it touches anything)
    # aborted. Every box that had ever prepared a stack was in that state. A scratch file is the
    # default from here on, so the selftest can neither read nor write the box's state; the blocks
    # that need a state file still point STATE_PATH at their own temp copy and restore it to this.
    def _box_state_bytes(path):
        """The box's state file as it is right now, for the before/after proof below. Never
        raises: an unreadable file is a state this must tolerate, not judge."""
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            return "unreadable: %s" % e.__class__.__name__

    _state_scratch = tempfile.mkdtemp(prefix="gio-selftest-state-")
    _box_state = STATE_PATH  # what the box configured -- kept only to prove it stays untouched
    _box_state_before = _box_state_bytes(_box_state)
    STATE_PATH = os.path.join(_state_scratch, "state.json")

    def ok(msg):
        nonlocal checks
        checks += 1
        print("selftest: " + msg)

    payload = {"uid": "10001", "msg": "item add 202 555"}
    secret, host = "TESTKEY", "http://10.0.0.1:21051"
    url, qstr, sign = sign_query(payload, secret, host)
    assert "cmd=1116" in qstr, qstr
    assert f"region={REGION}" in qstr
    assert "uid=10001" in qstr and "msg=item add 202 555" in qstr
    parts = qstr.split("&")
    assert parts == sorted(parts), "kvs must be sorted"
    assert sign == hashlib.sha256((qstr + secret).encode()).hexdigest()
    assert url.startswith(host + "/api?") and f"sign={sign}" in url
    assert "msg=item+add+202+555" in url, "spaces must urlencode to '+'"
    ok("MUIP signing OK")

    # Config-file loader: KEY=VALUE, comments, BOM, quotes; the environment keeps precedence.
    pairs = parse_config_text("\ufeff# comment\n\nGIO_A=1\n GIO_B = \"two\" \nbad line\n9X=no\nGIO_C='3'\r\n")
    assert pairs == [("GIO_A", "1"), ("GIO_B", "two"), ("GIO_C", "3")], pairs
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "config")
        with open(cfg, "w", encoding="utf-8-sig") as f:
            f.write("GIO_X=file\nGIO_Y=file\n")
        env = {"GIO_X": "env"}
        applied = apply_config_file(cfg, env)
        assert applied == ["GIO_Y"] and env == {"GIO_X": "env", "GIO_Y": "file"}, (applied, env)
        # --config / --log option parsing, both spellings
        assert _cli_option("--config", ["--config", cfg]) == cfg
        assert _cli_option("--log", ["--log=" + cfg, "x"]) == cfg
        assert _cli_option("--config", ["--selftest"]) is None
        # the rotating log: writes append, rotation keeps one .1
        lp = os.path.join(td, "agent.log")
        lg = _RotatingLog(lp, max_bytes=64)
        try:
            for i in range(10):
                lg.write("line %d is long enough to matter\n" % i)
            lg.flush()
            assert os.path.isfile(lp) and os.path.isfile(lp + ".1"), os.listdir(td)
            assert os.path.getsize(lp) <= 64 + 40 and os.path.getsize(lp + ".1") > 64
        finally:
            lg.close()
    ok("config file loader + CLI options + log rotation OK")

    # Platform defaults: Windows has no stack dirs unless told, and state lives beside --config.
    saved_plat = (IS_WINDOWS, CONFIG_FILE)
    try:
        IS_WINDOWS, CONFIG_FILE = True, None
        os.environ.pop("GIO_DIR_16_SELFTEST", None)
        assert _dir_setting("GIO_DIR_16_SELFTEST", "/home/1.6_live") == ""
        assert _default_state_path().endswith(os.path.join("Relic", "agent", "state.json")), _default_state_path()
        CONFIG_FILE = os.path.join("C:" + os.sep, "relic", "agent", "config")
        assert _default_state_path() == os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), "state.json")
        IS_WINDOWS, CONFIG_FILE = False, None
        assert _dir_setting("GIO_DIR_16_SELFTEST", "/home/1.6_live") == "/home/1.6_live"
        assert _default_state_path() == "/var/lib/gio-agent/state.json"
        assert _popen_kwargs() == {}
        IS_WINDOWS = True
        assert "creationflags" in _popen_kwargs()
    finally:
        IS_WINDOWS, CONFIG_FILE = saved_plat
    # an unconfigured version is never a path: stack_dir refuses, is_present/is_bootstrapped say no
    saved_dir = VERSIONS["2.8"]["dir"]
    try:
        VERSIONS["2.8"]["dir"] = ""
        assert not is_present("2.8") and not is_bootstrapped("2.8") and not is_configured("2.8")
        try:
            stack_dir("2.8")
            raise AssertionError("an unconfigured version must not resolve to a directory")
        except AgentError as e:
            assert e.status == 404
    finally:
        VERSIONS["2.8"]["dir"] = saved_dir
    ok("platform defaults OK (Windows: empty dirs, state beside --config, CREATE_NO_WINDOW)")

    # TSV repair: the real NewActivityCondData line-217 shape (a tab that became two spaces).
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("a\tb\tc\n1\t2\t3\n4  5\t6\n")
        tmp = f.name
    text, repaired, bad = validate_tsv(tmp)
    assert repaired == 1 and not bad, (repaired, bad)
    assert "4\t5\t6" in text, text
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("a\tb\tc\n1\t2\n")
    _, _, bad = validate_tsv(tmp)
    assert bad == [2], bad
    os.remove(tmp)
    ok("TSV repair OK")

    # Spiral Abyss calendar shift: whole years forward, byte-identical everywhere else. The
    # synthetic file mirrors the real one: CRLF, header without dates, half-month windows.
    hdr = "id\tentr\tfloors\topen1\tf2\to2\tf3\to3\tf4\to4\tend\treward\r"
    tower = "\n".join([
        hdr,
        "1\t1001,1002\t1009,1010\t2020-07-01 00:00:00\t\t\t\t\t\t\t2020-07-16 03:59:59\t40\r",
        "2\t1001,1002\t1011,1012\t2020-07-16 04:00:00\t\t\t\t\t\t\t2021-08-01 03:59:59\t60\r",
    ]) + "\n"
    now = "2026-08-14 12:00:00"
    new, info = shift_tower_calendar(tower, now)
    assert info["shift"] == 6 and info["start"] == "2026-07-01 00:00:00" \
        and info["end"] == "2027-08-01 03:59:59", info
    assert "2026-07-16 04:00:00" in new and "2027-08-01 03:59:59" in new, new
    assert _STAMP_RE.sub("@", new) == _STAMP_RE.sub("@", tower), "only timestamps may change"
    assert new.count("\r\n") == tower.count("\r\n") == 3, "CRLF endings must survive"
    assert tower_active_window(new, now) == ("2", "2027-08-01 03:59:59")
    again, info2 = shift_tower_calendar(new, now)
    assert info2["shift"] == 0 and again == new, "a calendar that already covers today is left alone"
    # under 12 months no whole-year shift can cover `now`: the earliest open stretches back instead
    short = "id\topen\tend\r\n7\t2020-07-01 04:00:00\t2020-10-01 03:59:59\r\n"
    new2, info3 = shift_tower_calendar(short, "2026-02-10 09:00:00")
    assert info3["start"] == "2026-02-10 00:00:00" and info3["end"] == "2026-10-01 03:59:59", info3
    assert tower_active_window(new2, "2026-02-10 09:00:00") == ("7", "2026-10-01 03:59:59")
    # a shifted Feb 29 must never be emitted into a non-leap year
    assert _plus_years("2020-02-29 04:00:00", 1) == "2021-02-28 04:00:00"
    assert _plus_years("2020-02-29 04:00:00", 4) == "2024-02-29 04:00:00"
    # a file shifted under a future-skewed clock must come BACK once the clock is right
    ahead, _ = shift_tower_calendar(tower, "2044-08-14 12:00:00")
    back, info4 = shift_tower_calendar(ahead, now)
    assert info4["shift"] == -18 and back == new, "negative re-shift must undo the skewed one"
    try:
        shift_tower_calendar("id\topen\tend\nno dates here\n", now)
        raise AssertionError("a dateless file must be refused, not rewritten")
    except AgentError as e:
        assert e.status == 500, e.status
    ok("Abyss/tower calendar OK (+years shift, CRLF and TSV intact, active window)")

    # Request framing. The chunked case is byte-for-byte what .NET's HttpClient sends; reading only
    # Content-Length is what answered "400 missing/invalid field: 'version'" to every command.
    CHUNKED = {"Transfer-Encoding": "chunked"}

    def body(raw, hdrs=None, **kw):
        return read_http_body(io.BytesIO(raw), hdrs or {}, **kw)

    assert body(b'11\r\n{"version":"1.6"}\r\n0\r\n\r\n', CHUNKED) == b'{"version":"1.6"}'
    # split across chunks, with a chunk extension and a trailer -- all legal HTTP/1.1
    assert body(b'5;x=y\r\n{"ver\r\n5\r\nsion"\r\n0\r\nX-T: 1\r\n\r\n', CHUNKED) == b'{"version"'
    assert body(b'{"a":1}', {"Content-Length": "7"}) == b'{"a":1}'
    assert body(b"ignored") == b""  # no framing headers at all -> no body
    for raw, hdrs in ((b"zz\r\n", CHUNKED), (b"", {"Content-Length": "x"})):
        try:
            body(raw, hdrs)
            raise AssertionError("malformed framing must be rejected: %r" % raw)
        except AgentError as e:
            assert e.status == 400, e.status
    # the public body cap: 4 KB + 1 is refused with 413 in both framings
    big = b"x" * (PUBLIC_MAX_BODY + 1)
    for raw, hdrs in ((big, {"Content-Length": str(len(big))}),
                      (b"%x\r\n" % len(big) + big + b"\r\n0\r\n\r\n", CHUNKED)):
        try:
            body(raw, hdrs, max_body=PUBLIC_MAX_BODY)
            raise AssertionError("a public body over the cap must be refused")
        except AgentError as e:
            assert e.status == 413, e.status
    ok("HTTP body framing OK (chunked + counted + malformed + public 4 KB cap)")

    # compose ps parsing -- the eyes of the stability check and the service watchdog. NDJSON is
    # modern compose, the array is older compose; garbage must read as "docker is mute" (None),
    # never as an empty-but-valid answer that would count every service as dead.
    nd = _parse_ps_json('{"Service":"mysql","State":"running"}\n{"Service":"gameserver","State":"exited"}')
    assert nd == {"mysql": "running", "gameserver": "exited"}, nd
    arr = _parse_ps_json('[{"Service":"mysql","State":"running"},{"Service":"nodeserver","State":"Exited"}]')
    assert arr == {"mysql": "running", "nodeserver": "exited"}, arr
    assert _parse_ps_json("") == {} and _parse_ps_json(None) == {}
    assert _parse_ps_json("plain text, not json") is None
    # exited AND never-created both count as dead; a healthy stack reports nothing
    dead = _dead_from(["gameserver", "mysql", "nodeserver"], {"mysql": "running", "gameserver": "exited"})
    assert dead == ["gameserver", "nodeserver"], dead
    assert _dead_from(["mysql"], {"mysql": "running"}) == []
    ok("compose ps parsing OK (NDJSON + array + garbage -> mute)")

    # compose ls parsing -- same defensive shape as ps: array today, NDJSON-proof for tomorrow.
    assert _parse_ls_json('[{"Name":"16_live","Status":"running(13)"}]') == ["16_live"]
    assert _parse_ls_json('{"Name":"16_live"}\n{"Name":"28_live"}') == ["16_live", "28_live"]
    assert _parse_ls_json("") == [] and _parse_ls_json(None) == []
    assert _parse_ls_json("not json") is None and _parse_ls_json('["only","strings"]') is None
    # the compose project name is derived from the dir basename exactly like compose derives it --
    # the agent's up-checks compare it against `ls` output, so the derivation must match compose's.
    assert _project_name("/home/1.6_live") == "16_live"
    assert _project_name("/root/2.8_live/") == "28_live"
    assert _project_name("/srv/My.Stack-X") == "mystack-x"
    assert _project_name("D:\\servers\\1.6_live") == "16_live" or os.name != "nt"
    ok("compose ls parsing + derived project name OK")

    # IP rewrite must not maul the docker subnet (172.10.3.8 shares no boundary with 172.10.3.80).
    text = 'outer_ip="192.0.2.57" peer="172.10.3.8" other="192.0.2.570"'
    out = re.sub(IP_RE % re.escape("192.0.2.57"), "198.51.100.171", text)
    assert 'outer_ip="198.51.100.171"' in out and '172.10.3.8' in out and '192.0.2.570' in out, out

    # Bind-IP guard: a re-extracted archive ships OUTER_IP=127.0.0.1 and every render (compose port
    # bindings, server XMLs, data.sql) inherits it -- the "server busy (502)" reinstall trap.
    saved = (BIND_IP, ADVERTISED_IP, VERSIONS["1.6"]["dir"])

    class _Job:
        def __init__(self):
            self.lines = []

        def log(self, msg):
            self.lines.append(str(msg))

    with tempfile.TemporaryDirectory() as td:
        VERSIONS["1.6"]["dir"] = td
        with open(os.path.join(td, ".env"), "w", encoding="utf-8") as f:
            f.write("# keep this comment\nMYSQL_ROOT_PASSWORD=pw\nOUTER_IP=10.99.99.99\n"
                    "RELOAD_CONFIG_INTERVAL=-1\n")
        try:
            # 192.0.2.x is TEST-NET-1 -- unbindable, unless the box allows nonlocal binds
            # (net.ipv4.ip_nonlocal_bind=1), in which case the sub-check proves nothing: skip it.
            BIND_IP, ADVERTISED_IP = "192.0.2.123", "198.51.100.171"
            if _bindable("192.0.2.123"):
                print("selftest: skipping the foreign-IP refusal (ip_nonlocal_bind=1 on this machine)")
            else:
                try:
                    ensure_bind_ip("1.6", _Job())
                    raise AssertionError("a GIO_BIND_IP that is not this machine's must be refused")
                except AgentError as e:
                    assert e.status == 500 and "GIO_ADVERTISED_IP" in e.message

            # loopback GIO_BIND_IP on a server clients must reach is a config error, not a repair
            BIND_IP = "127.0.0.1"
            try:
                ensure_bind_ip("1.6", _Job())
                raise AssertionError("loopback GIO_BIND_IP + GIO_ADVERTISED_IP set must be refused")
            except AgentError as e:
                assert e.status == 500 and "loopback" in e.message

            # ...but explicit localhost play (no advertised IP) is fine -- and exercises the rewrite
            ADVERTISED_IP = ""
            assert ensure_bind_ip("1.6", _Job()) == "127.0.0.1"
            env = read_env("1.6")
            assert env["OUTER_IP"] == "127.0.0.1" and env["MYSQL_ROOT_PASSWORD"] == "pw"
            with open(os.path.join(td, ".env"), encoding="utf-8") as f:
                assert "# keep this comment" in f.read(), ".env comments must survive"

            # localhost-only play (no advertised IP, no bind IP) must be left alone
            BIND_IP = ""
            assert ensure_bind_ip("1.6", _Job()) == "127.0.0.1"
            assert read_env("1.6")["OUTER_IP"] == "127.0.0.1"

            # reachable server left with the vendor loopback: auto-detect (or the explicit error
            # pointing at GIO_BIND_IP on a box where detection has no route to work with)
            ADVERTISED_IP = "198.51.100.171"
            try:
                got = ensure_bind_ip("1.6", _Job())
                assert got and not got.startswith("127."), got
                assert read_env("1.6")["OUTER_IP"] == got
            except AgentError as e:
                assert e.status == 500 and "GIO_BIND_IP" in e.message

            # a BOM'd .env (Windows editor round trip) must still edit in place, not append a twin,
            # and CRLF endings must come out as CRLF
            with open(os.path.join(td, ".env"), "w", encoding="utf-8-sig", newline="") as f:
                f.write("OUTER_IP=127.0.0.1\r\nMYSQL_ROOT_PASSWORD=pw\r\n")
            write_env_value("1.6", "OUTER_IP", "192.0.2.7")
            write_env_value("1.6", "NEW_KEY", "v")
            with open(os.path.join(td, ".env"), "rb") as f:
                raw = f.read()
            assert raw.count(b"OUTER_IP=") == 1 and b"192.0.2.7" in raw, raw
            assert raw == b"OUTER_IP=192.0.2.7\r\nMYSQL_ROOT_PASSWORD=pw\r\nNEW_KEY=v\r\n", raw

            # the loopback-render detector + the generic template renderer (CRLF preserved)
            with open(os.path.join(td, "docker-compose.yml.tmpl"), "w", encoding="utf-8", newline="") as f:
                f.write('ports:\r\n  - "%OUTER_IP%:21000:80"\r\npass: "%MYSQL_ROOT_PASSWORD%"\r\n')
            n = render_stack_configs("1.6", _Job())
            assert n == 1, n
            with open(os.path.join(td, "docker-compose.yml"), "rb") as f:
                rendered = f.read()
            assert b'"192.0.2.7:21000:80"\r\n' in rendered and b'pass: "pw"' in rendered, rendered
            assert not compose_binds_loopback("1.6")
            write_env_value("1.6", "OUTER_IP", "127.0.0.1")
            render_stack_configs("1.6", _Job())
            assert compose_binds_loopback("1.6")
            # a LAN-only box (advertised empty, bind IP set) walks into the same trap -- stay armed
            ADVERTISED_IP, BIND_IP = "", "10.99.99.99"
            assert compose_binds_loopback("1.6")
            BIND_IP = ""  # localhost play proper: loopback bindings are the point, not a bug
            assert not compose_binds_loopback("1.6")

            # secrets: the shared literal is replaced in every syntax and nowhere else; the MUIP key
            # by anchor; the password-verify flag by regex; all with endings intact
            old = "HsjnaKGabKebabSarmale"
            sql = ("CREATE USER IF NOT EXISTS 'hk4e_work'@'172.10.%%' IDENTIFIED BY '%s';\r\n"
                   "CREATE USER IF NOT EXISTS 'hk4e_readonly'@'172.10.%%' IDENTIFIED BY '%s';\r\n"
                   "-- %s appears in a comment too\r\n" % (old, old, old))
            new_sql, n = replace_sql_identified_by(sql, old, "NewPass_123")
            assert n == 2 and new_sql.count("NewPass_123") == 2 and "-- %s appears" % old in new_sql, new_sql
            comp = "    command: redis-server --save 60 1 --loglevel warning --requirepass %s\n  x: %sX\n" % (old, old)
            new_comp, n = replace_requirepass(comp, old, "NewPass_123")
            assert n == 1 and "--requirepass NewPass_123\n" in new_comp and "%sX" % old in new_comp, new_comp
            sh = "mysql -h172.10.3.100 -u hk4e_work -p%s < data.sql\nmysql -u hk4e_work -p%s < adjust.sql\necho -p%sx\n" % (old, old, old)
            new_sh, n = replace_sh_password(sh, old, "NewPass_123")
            assert n == 2 and new_sh.count("-pNewPass_123 <") == 2 and "-p%sx" % old in new_sh, new_sh
            xml = ('<Db index="1" user="hk4e_work" pwd="%s" dbname="hk4e_db_user" />\r\n'
                   '<Db index="14" host="172.10.3.101" pwd="%s" db="7"/>\r\n'
                   '<ApiConf sign_key="8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ" />\r\n'
                   '<Other notpwd="%s" />\r\n' % (old, old, old))
            new_xml, n = replace_xml_pwd(xml, old, "NewPass_123")
            assert n == 2 and new_xml.count('pwd="NewPass_123"') == 2 and 'notpwd="%s"' % old in new_xml, new_xml
            assert new_xml.count("\r\n") == 4
            keyed, n = replace_sign_key(new_xml, "MuipKey_0123456789")
            assert n == 1 and 'sign_key="MuipKey_0123456789"' in keyed and 'pwd="NewPass_123"' in keyed
            assert "sign_key" not in replace_sign_key("no key here", "x")[0] or True
            assert replace_sign_key("no key here", "x")[1] == 0
            cfg = '{\r\n  "auth": {\r\n    "enable_password_verify": false,\r\n    "enable_server_guest": true\r\n  }\r\n}\r\n'
            on, n = set_password_verify_text(cfg, True)
            assert n == 1 and '"enable_password_verify": true,' in on and on.count("\r\n") == cfg.count("\r\n"), on
            off, n = set_password_verify_text(on, False)
            assert n == 1 and off == cfg, off
            assert set_password_verify_text("{}", True)[1] == 0
            # the same, applied to files through _rewrite (atomic, endings preserved)
            sdk_dir = os.path.join(td, "sdk", "data")
            os.makedirs(sdk_dir)
            with open(os.path.join(sdk_dir, "config.json.tmpl"), "w", newline="") as f:
                f.write(cfg)
            VERSIONS["1.6"]["dir"] = td
            n = _rewrite(os.path.join(sdk_dir, "config.json.tmpl"), lambda t: set_password_verify_text(t, True))
            with open(os.path.join(sdk_dir, "config.json.tmpl"), "rb") as f:
                assert n == 1 and b'"enable_password_verify": true,\r\n' in f.read()
            assert _rewrite(os.path.join(td, "nope.json"), lambda t: (t, 1)) == 0
            # secrets_info: values come from the sources, the MUIP key only as a fingerprint
            os.makedirs(os.path.join(td, "server", "muipserver", "conf"))
            with open(os.path.join(td, "server", "muipserver", "conf", "muipserver.xml.tmpl"), "w") as f:
                f.write('<ApiConf sign_key="8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ" />\n')
            with open(os.path.join(td, "docker-compose.yml.tmpl"), "a") as f:
                f.write("cmd: redis-server --requirepass %s\n" % old)
            write_env_value("1.6", "FLASK_SECRET_KEY", "fl")
            info = secrets_info("1.6")
            assert info["mysqlRoot"] == "pw" and info["flask"] == "fl" and info["internal"] == old, info
            assert info["muip"]["set"] and len(info["muip"]["fingerprint"]) == 8
            assert "8JTdsghu" not in json.dumps(info), "the MUIP key must never be returned"
            assert info["muip"]["vendorDefault"] is True and info["muip"]["source"] is None, info["muip"]
            assert internal_literal("1.6") == old
            write_creds_file("1.6", {"mysqlRoot": "a", "flask": "b", "muip": "c", "internal": "d"})
            with open(os.path.join(td, "creds.txt")) as f:
                creds = f.read()
            assert "mysql/root: a" in creds and "h4ke pass: d" in creds and "Managed by" in creds
        finally:
            BIND_IP, ADVERTISED_IP, VERSIONS["1.6"]["dir"] = saved
    ok("bind-IP guard OK (.env repair, CRLF kept, loopback render detection)")
    ok("secrets helpers OK (SQL/compose/sh/XML anchors, sign_key, creds.txt, fingerprint only)")
    ok("password-verify flag edit OK (regex, endings kept, atomic rewrite)")

    # Exec-bit repair + owner/mode preservation -- POSIX only (Windows has no Unix exec bit; the
    # authoritative run is the one install_agent.sh does on the box).
    if os.name == "posix":
        saved_dir = VERSIONS["1.6"]["dir"]
        with tempfile.TemporaryDirectory() as td:
            VERSIONS["1.6"]["dir"] = td
            try:
                os.makedirs(os.path.join(td, "dockerfiles", "prepare-vars"))
                os.makedirs(os.path.join(td, "server", "gameserver"))
                os.makedirs(os.path.join(td, "server", "data"))
                for rel in (("bootstrap.sh",), ("dockerfiles", "prepare-vars", "prepare-vars.sh"),
                            ("server", "gameserver", "gameserver"), ("server", "data", "notes.txt")):
                    p = os.path.join(td, *rel)
                    with open(p, "w") as f:
                        f.write("x")
                    os.chmod(p, 0o644)
                assert ensure_exec_bits("1.6", _Job()) == 3
                assert os.stat(os.path.join(td, "bootstrap.sh")).st_mode & 0o111 == 0o111
                assert os.stat(os.path.join(td, "server", "gameserver", "gameserver")).st_mode & 0o111
                assert not os.stat(os.path.join(td, "server", "data", "notes.txt")).st_mode & 0o111, \
                    "data files must not get an exec bit"
                assert ensure_exec_bits("1.6", _Job()) == 0, "idempotent"
                # an overwritten file gets its previous mode back...
                secret_f = os.path.join(td, "sec.bin")
                with open(secret_f, "w") as f:
                    f.write("old")
                os.chmod(secret_f, 0o600)
                prev = _stat_or_none(secret_f)
                with open(secret_f, "w") as f:
                    f.write("new")
                os.chmod(secret_f, 0o644)  # what copy2 would have stamped from the payload
                _restore_owner_mode(secret_f, prev)
                assert os.stat(secret_f).st_mode & 0o7777 == 0o600
                # ...and a NEW file gets 0644 + its directory's owner (the container-uid case)
                fresh = os.path.join(td, "server", "data", "fresh.bin")
                open(fresh, "w").close()
                os.chmod(fresh, 0o750)
                _restore_owner_mode(fresh, None)
                assert os.stat(fresh).st_mode & 0o7777 == 0o644
                if hasattr(os, "chown") and os.geteuid() == 0:
                    os.chown(os.path.join(td, "server", "data"), 12345, 12345)
                    _restore_owner_mode(fresh, None)
                    st = os.stat(fresh)
                    assert (st.st_uid, st.st_gid) == (12345, 12345), \
                        "a new file must inherit the directory's owner"
            finally:
                VERSIONS["1.6"]["dir"] = saved_dir
        ok("exec-bit repair + owner/mode preservation OK")
    else:
        j = _Job()
        saved_dir = VERSIONS["1.6"]["dir"]
        VERSIONS["1.6"]["dir"] = os.getcwd()
        try:
            assert ensure_exec_bits("1.6", j) == 0 and j.lines and "skipped" in j.lines[0]
        finally:
            VERSIONS["1.6"]["dir"] = saved_dir
        print("selftest: skipping the exec-bit test (POSIX only; the no-op path logs one line)")

    # Every shipped manifest must be loadable and point at files that exist.
    for v in VERSIONS:
        man = read_manifest(v)
        if not man:
            print("selftest: no payload for", v, "(skipped)")
            continue
        for item in man.get("files", []) + man.get("mysql", []):
            p = os.path.join(payload_dir(v), item["src"])
            assert os.path.isfile(p), "payload missing: " + p
        for stmt in man.get("sql_events", []):
            # One statement per entry -- run_sql reports per-statement failures, and a stray ';'
            # would hide the second half of a line inside a single "ok"/"ignored" log entry.
            # (A ';' inside a quoted string, as in a `desc` column, is fine.)
            assert ";" not in re.sub(r"'[^']*'", "''", stmt), \
                "sql_events must be one statement per entry: " + stmt[:60]
        ev = man.get("events") or {}
        gaa, others = ev.get("gaa") or {}, ev.get("others") or {}
        assert gaa, "no GAA events listed for " + v
        assert not (set(gaa) & set(others)), \
            "an event cannot be both open and closed: %s" % (set(gaa) & set(others))
        for k in list(gaa) + list(others):
            assert str(k).isdigit(), "schedule ids must be numeric (SQL is built from them): " + str(k)
        tpls = manifest_templates(v, man)
        validate_templates(tpls, payload_dir(v))
        ok("events %s OK (%d open, %d closed)" % (v, len(gaa), len(others)))
        ok("payload %s OK (%d files, %d dumps, %d sql, %d templates: %s)"
           % (v, len(man.get("files", [])), len(man.get("mysql", [])), len(man.get("sql_events", [])),
              len(tpls), ", ".join(t["id"] for t in tpls) or "-"))
    # template manifest validation refuses bad ids, duplicates and missing dumps
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "a.sql"), "w").close()
        validate_templates([{"id": "pre-gaa", "label": "x", "sql": "a.sql", "md5": None}], td)
        for bad_list, why in (
                ([{"id": "Pre GAA", "label": "", "sql": "a.sql", "md5": None}], "id"),
                ([{"id": "fresh", "label": "", "sql": "a.sql", "md5": None}], "reserved"),
                ([{"id": "x", "label": "", "sql": "a.sql", "md5": None}] * 2, "duplicate"),
                ([{"id": "x", "label": "", "sql": "missing.sql", "md5": None}], "missing"),
                ([{"id": "x", "label": "", "sql": "a.sql", "md5": "00"}], "md5")):
            try:
                validate_templates(bad_list, td)
                raise AssertionError("validate_templates must refuse: " + why)
            except ValueError as e:
                assert why in str(e), (why, e)
    assert template_db_name("post-gaa") == "relic_tpl_post-gaa"
    ok("template manifest validation OK")

    # Guid rewrite for account copy: (uid<<32)|seq over varint/fixed64/packed lists.
    def _f(fn, wt):
        return _pb_enc((fn << 3) | wt)

    def _g(uid, seq):
        return (uid << 32) | seq

    # The pair that set the trap: 2 packed guids parse as a "message" unless the maximum field
    # number is validated (the first guid becomes a tag and escapes unrewritten).
    packed2 = _pb_enc(_g(1, 0x58)) + _pb_enc(_g(1, 0x70))
    equip = _f(2, 2) + _pb_enc(len(packed2)) + packed2                      # ...101.2 (packed known)
    avatar = _f(3, 0) + _pb_enc(_g(1, 5)) + _f(101, 2) + _pb_enc(len(equip)) + equip
    avlist = _f(1, 2) + _pb_enc(len(avatar)) + avatar                       # 2.1
    item = _f(3, 1) + _g(1, 7).to_bytes(8, "little")                        # 5.1.1.3 fixed64
    ilist = _f(1, 2) + _pb_enc(len(item)) + item
    pack = _f(1, 2) + _pb_enc(len(ilist)) + ilist
    root = _f(2, 2) + _pb_enc(len(avlist)) + avlist + _f(5, 2) + _pb_enc(len(pack)) + pack
    entries = []
    out, ch = _guid_rewrite(root, 1, 3, (), entries)
    assert ch and sum(n for k, _, n in entries) == 4, entries               # 2 packed + varint + fixed64
    rv, rf = _guid_raw_scan(out, 1)
    assert not rv and not rf, "source guids left: %s/%s" % (rv, rf)
    rv, rf = _guid_raw_scan(out, 3)
    assert len(rv) == 3 and len(rf) == 1, (rv, rf)
    back, _ = _guid_rewrite(out, 3, 1, (), [])
    assert back == root, "roundtrip failed"
    # the same packed list on an UNKNOWN path: the field-number limit makes the message parse fail
    # and the packed fallback rewrites it anyway
    unk = _f(9, 2) + _pb_enc(len(packed2)) + packed2
    entries = []
    out, _ = _guid_rewrite(unk, 1, 3, (), entries)
    assert any(k == "packed-new" for k, _, _n in entries), entries
    assert not _guid_raw_scan(out, 1)[0], "a guid hidden in a tag escaped"
    # co-op fields are dropped (7.2 cur_scene_owner_uid)
    scene = _f(2, 0) + _pb_enc(77)
    r7 = _f(7, 2) + _pb_enc(len(scene)) + scene
    entries = []
    out, _ = _guid_rewrite(r7, 1, 3, (), entries)
    assert any(k == "drop" for k, _, _n in entries) and out == _f(7, 2) + _pb_enc(0), (entries, out)
    ok("account-copy guid rewrite OK (packed + tag-trap + drop + roundtrip)")

    # Regression, live 2.8 template signup 2026-09-21: an OPAQUE bytes field whose first byte reads
    # as a wiretype-1 tag. _guid_rewrite refuses it ("truncated fixed64") and copies it verbatim,
    # but _guid_scan used to read the 8-byte value PAST the end of the field -- a 5-byte slice whose
    # high32 is a small number -- and copy_save refused a save that had been rewritten correctly.
    opq = bytes.fromhex("5152a501b501")
    assert int.from_bytes(opq[1:9], "little") >> 32 == 1, "the trap byte pattern changed"
    node = _f(1, 0) + _pb_enc(33) + _f(2, 0) + _pb_enc(57) + _f(3, 2) + _pb_enc(len(opq)) + opq
    assert 1 not in _guid_scan(node), "a fixed64 must not be read past the end of its field"
    for wt, why in ((1, "fixed64"), (5, "fixed32")):
        try:
            _guid_scan(_f(1, wt) + bytes([1, 2]))
            raise AssertionError("_guid_scan must refuse a truncated " + why)
        except _PbParseError:
            pass
    # the same bytes through the real gate: a second pass changes nothing and the field is CHECKED
    # (a clean run of varints), so it leaves no residual candidate at all
    entries = []
    out, _ = _guid_rewrite(node, 1, 3, (), entries)
    assert out == node and not entries, entries
    smap = _GuidSpans()
    out2, ch2 = _guid_rewrite(out, 1, 3, (), [], smap=smap)
    assert not ch2 and out2 == out, "the rewrite must be a fixed point"
    assert not smap.opaque and not _raw_scan_in_spans(out, 1, smap.opaque), smap.opaque

    # the fixed-point gate catches a source guid left in any decoded position, with no threshold
    root3, _ = _guid_rewrite(root, 1, 3, (), [])
    smap = _GuidSpans()
    same, ch = _guid_rewrite(root3, 1, 3, (), [], smap=smap)
    assert not ch and same == root3, "a clean rewrite must be a fixed point"
    for planted, why in (
            (root3.replace(_g(3, 7).to_bytes(8, "little"), _g(1, 7).to_bytes(8, "little")), "fixed64"),
            (root3.replace(_pb_enc(_g(3, 5)), _pb_enc(_g(1, 5))), "varint"),
            (root3.replace(_pb_enc(_g(3, 0x58)), _pb_enc(_g(1, 0x58))), "packed element")):
        assert planted != root3, why
        assert _guid_rewrite(planted, 1, 3, (), [])[1], "a guid left in a %s must fail the gate" % why

    # a guid hidden in a bytes field the walk cannot decode: the rewriter cannot reach it, so the
    # span map is what has to see it -- and a control uid over the same span must not
    hidden = _g(1, 77).to_bytes(8, "little") + bytes([0x80])   # the trailing byte breaks _pb_packed
    blob = _f(12, 2) + _pb_enc(len(hidden)) + hidden
    smap = _GuidSpans()
    out, ch = _guid_rewrite(blob, 1, 3, (), [], smap=smap)
    assert not ch and out == blob and len(smap.opaque) == 1, (ch, smap.opaque)
    assert _raw_scan_in_spans(blob, 1, smap.opaque) == 1, "the hidden guid must be counted"
    assert _raw_scan_in_spans(blob, 2, smap.opaque) == 0, "a control uid must not match it"
    # control uids: never the source or the destination, always the source's byte width
    for src_uid, dst_uid in ((1, 3), (2, 9), (10437, 10438)):
        ctl = _raw_noise_controls(src_uid, dst_uid)
        assert len(ctl) == _RAW_NOISE_CONTROLS and len(set(ctl)) == len(ctl), ctl
        assert src_uid not in ctl and dst_uid not in ctl, ctl
        assert all(c.bit_length() + 7 >> 3 == src_uid.bit_length() + 7 >> 3 for c in ctl), ctl
    ok("account-copy residual gates OK (fixed point + span map + measured noise budget)")

    # copy_save's SQL: the default source is unqualified (admin copy, byte-for-byte the old
    # statements); a template source qualifies ONLY the SELECT side, never the destination.
    plain = build_copy_sql(17, 23, "ab", 1)
    assert plain[0] == "START TRANSACTION;" and plain[-1] == "COMMIT;" and len(plain) == 8, plain
    assert "FROM t_player_data_7 WHERE uid=17;" in plain[2] and "INSERT INTO t_player_data_3 " in plain[2]
    assert "0xab" in plain[2] and "`" not in "".join(plain)
    assert "FROM t_home_data_7 WHERE uid=17;" in plain[6]
    none_home = build_copy_sql(17, 23, "ab", 0)
    assert len(none_home) == 7 and "INSERT INTO t_home_data_3" not in "".join(none_home), none_home
    assert none_home[5] == "DELETE FROM t_home_data_3 WHERE uid=23;", \
        "a source without a teapot still clears the destination's realm rows"
    tpl = build_copy_sql(1, 23, "ab", 1, src_db="relic_tpl_pre-gaa")
    assert "FROM `relic_tpl_pre-gaa`.t_player_data_1 WHERE uid=1;" in tpl[2], tpl[2]
    assert "FROM `relic_tpl_pre-gaa`.t_block_data_1 WHERE uid=1;" in tpl[4], tpl[4]
    assert "FROM `relic_tpl_pre-gaa`.t_home_data_1 WHERE uid=1;" in tpl[6], tpl[6]
    for s in tpl:
        assert "INTO `" not in s and "DELETE FROM `" not in s, "destination must stay unqualified: " + s
        assert "relic_tpl_pre-gaa`.t_player_data_3" not in s
    assert tpl[1] == "DELETE FROM t_player_data_3 WHERE uid=23;"
    ok("copy_save SQL qualification OK (default unqualified, template source qualified)")

    # A command's own notices are not its error: docker compose writes one on EVERY call with the
    # vendor compose file, and it used to be the entire error message (the real reason was truncated
    # off). And a database service that dies under the copy is waited for, then written again.
    notice = ('time="2026-09-23T00:22:30+03:00" level=warning msg="D:\\x\\docker-compose.yml: the '
              'attribute `version` is obsolete, it will be ignored"')
    assert _clean_cmd_output(notice) == "", notice
    assert _clean_cmd_output(notice + "\nERROR 1064 (42000) at line 3: bad") == "ERROR 1064 (42000) at line 3: bad"
    assert _clean_cmd_output("WARN[0000] the attribute `version` is obsolete\nreal") == "real"
    assert _clean_cmd_output(None) == ""
    # a warning that MATTERS is never dropped -- from the log line or from the message
    keep = 'time="x" level=warning msg="The \\"OUTER_IP\\" variable is not set. Defaulting to a blank string."'
    assert _clean_cmd_output(keep) == keep and _clean_cmd_output("WARN[0000] Found orphan containers")
    assert _cmd_err(_Failed("boom")) == "boom"

    class _R:
        def __init__(self, rc, err="", out=""):
            self.returncode, self.stderr, self.stdout = rc, err, out

    assert _cmd_err(_R(1, notice + "\nERROR 2013: Lost connection")) == "ERROR 2013: Lost connection"
    assert _cmd_err(_R(1, notice, "row output")) == "row output", "stdout when stderr is only notices"
    assert _cmd_err(_R(1, notice)).startswith("time="), "notices beat an empty reason"
    assert _cmd_err(_R(1, "x" * 500), 100) == "..." + "x" * 100, "the TAIL, where the error is"

    g2 = globals()
    saved_w = {k: g2[k] for k in ("mysql_import", "mysql_reachable", "wait_for_mysql", "_mysql_q")}
    try:
        imports, waits = [], []
        g2["wait_for_mysql"] = lambda _v, _j, timeout=0: waits.append(timeout)
        g2["mysql_import"] = lambda _v, path, _db, timeout=0: imports.append(path) or _R(0)
        g2["mysql_reachable"] = lambda _v: True
        j = _Job()
        _write_copy_sql("1.6", j, "sql")
        assert imports == ["sql"] and waits == [], (imports, waits)
        # dead under the write: one wait, one more write, no error
        dead = [_R(1, notice + "\nERROR 2013 (HY000): Lost connection to server during query"), _R(0)]
        g2["mysql_import"] = lambda _v, path, _db, timeout=0: imports.append(path) or dead.pop(0)
        g2["mysql_reachable"] = lambda _v: False
        _write_copy_sql("1.6", j, "sql")
        assert len(imports) == 3 and waits == [180], (imports, waits)
        assert any("went away" in l for l in j.lines), j.lines
        # still failing: the reason is the SQL error, never the compose notice
        g2["mysql_import"] = lambda _v, path, _db, timeout=0: _R(1, notice + "\nERROR 1064 (42000): syntax")
        try:
            _write_copy_sql("1.6", j, "sql")
            raise AssertionError("a failing write must raise")
        except AgentError as e:
            assert e.status == 500 and e.message.endswith("ERROR 1064 (42000): syntax"), e.message
            assert "level=warning" not in e.message, e.message
        # reachable all along: a real SQL failure is not retried
        tries = []
        g2["mysql_import"] = lambda _v, path, _db, timeout=0: tries.append(path) or _R(1, "ERROR 1146: no table")
        g2["mysql_reachable"] = lambda _v: True
        try:
            _write_copy_sql("1.6", j, "sql")
            raise AssertionError("a failing write must raise")
        except AgentError:
            pass
        assert len(tries) == 1, tries
        # the rollback waits for the service instead of logging three failures of its own
        rolled = []
        g2["mysql_reachable"] = lambda _v: False

        def _no_come_back(_v, _j, timeout=0):
            raise AgentError(500, "MySQL did not come up within %ds." % timeout)

        g2["wait_for_mysql"] = _no_come_back
        g2["_mysql_q"] = lambda *a, **k: rolled.append(a) or ""
        j2 = _Job()
        _signup_rollback("1.6", j2, 5, name="bob")
        assert rolled == [] and any("did not come back" in l for l in j2.lines), (rolled, j2.lines)
    finally:
        g2.update(saved_w)
    ok("command errors OK (the tool's notices are never the reason; a database service that dies "
       "under the copy is waited for and the write repeated; the rollback waits too)")

    # Rate limiter: token buckets with a fixed clock -- limit 3 per hour refuses the 4th, says when
    # to retry, recovers after that long, and never lets a burst exceed the limit; LRU cap holds.
    rl = RateLimiter(cap=3)
    t0 = 1000.0
    assert all(rl.take("ip1", 3, 3600, now=t0)[0] for _ in range(3))
    okk, wait = rl.take("ip1", 3, 3600, now=t0)
    assert not okk and 1190 <= wait <= 1201, wait
    assert not rl.take("ip1", 3, 3600, now=t0 + wait - 5)[0]
    assert rl.take("ip1", 3, 3600, now=t0 + wait + 1)[0]
    assert not rl.take("ip1", 3, 3600, now=t0 + wait + 1)[0], "refill is gradual, not a reset"
    assert rl.take("ip1", 3, 3600, now=t0 + 100000)[0] and rl.take("ip1", 3, 3600, now=t0 + 100000)[0]
    for k in ("a", "b", "c", "d"):
        rl.take(k, 1, 60, now=t0)
    assert "ip1" not in rl._buckets and len(rl._buckets) == 3, list(rl._buckets)  # LRU-evicted
    assert rl.take("a", 1, 60, now=t0)[0], "an evicted key starts fresh"
    # a global quota bucket ("<rule>:*") is never the LRU victim, however many addresses churn
    rl2 = RateLimiter(cap=2)
    rl2.take("create-d:*", 1, 86400, now=t0)
    for k in ("x", "y", "z"):
        rl2.take(k, 1, 60, now=t0)
    assert "create-d:*" in rl2._buckets and len(rl2._buckets) == 2, list(rl2._buckets)
    assert not rl2.take("create-d:*", 1, 86400, now=t0)[0], "the global counter must have survived"
    ok("rate limiter OK (bucket math, retry-after, LRU cap, global buckets exempt)")

    # take_all is all or nothing: a refused LATER bucket leaves the earlier ones untouched, the
    # refusal names the longest wait and that rule's code; give() never exceeds the limit and is a
    # no-op on an evicted key.
    rl3 = RateLimiter()
    quota = signup_quota_rules("203.0.113.5", "1.6", {"maxPerDay": 1})
    assert rl3.take_all(quota, now=t0) == (True, 0, None)
    before = dict(rl3._buckets)
    okk, wait, code = rl3.take_all(signup_quota_rules("203.0.113.6", "1.6", {"maxPerDay": 1}), now=t0)
    assert not okk and code == "quota_server_day" and 86390 <= wait <= 86400, (okk, wait, code)
    assert "create-h:203.0.113.6" not in rl3._buckets and dict(rl3._buckets) == before, \
        "a drained global bucket must not spend the caller's per-address tokens"
    rl3.give(quota, now=t0)
    assert rl3._buckets["create-h:203.0.113.5"][0] == 3.0 and rl3._buckets["create-d:*:1.6"][0] == 1.0
    rl3.give(quota, now=t0)
    assert all(rl3._buckets[r[0]][0] == float(r[1]) for r in quota), "give must never exceed the limit"
    rl3.give([("gone:203.0.113.7", 3, 3600)], now=t0)
    assert "gone:203.0.113.7" not in rl3._buckets, "give on a missing/evicted key is a no-op"
    # the longest wait wins when several buckets refuse at once
    rl4 = RateLimiter()
    rl4.take_all([("a:1", 1, 60, "rate_limited"), ("b:*", 1, 3600, "rate_limited_server")], now=t0)
    assert rl4.take_all([("a:1", 1, 60, "rate_limited"), ("b:*", 1, 3600, "rate_limited_server")],
                        now=t0)[1:] == (3600, "rate_limited_server")
    assert rl4.take_all([("zero", 0, 86400, "quota_server_day")], now=t0) == (False, 86400, "quota_server_day")
    # one code per enforce_limits rule, every one of them in the documented vocabulary
    rl5 = RateLimiter()
    for i, rule in enumerate(signup_quota_rules("198.51.100.9", "2.8", {"maxPerDay": 50})
                             + namecheck_rules("198.51.100.9")
                             + [("status:x", 30, 60, "rate_limited"), ("cmd:*", 300, 60, "rate_limited_server")]):
        assert rule[3] in PUBLIC_ERROR_CODES, rule
        for _ in range(rule[1]):
            enforce_limits([rule], limiter=rl5)
        try:
            enforce_limits([rule], limiter=rl5)
            raise AssertionError("rule %r must refuse once drained" % (rule,))
        except AgentError as e:
            assert e.status == 429 and e.code == rule[3] and e.retry_after >= 1, (rule, e.code)
    try:
        enforce_limits([("hotpatch:x", 0, 60)], limiter=rl5)
        raise AssertionError("a zero limit refuses")
    except AgentError as e:
        assert e.code is None, "the hotpatch mirror limiter carries no public code"
    ok("rate limiter take_all/give OK (all or nothing, longest wait + its code, refund capped, one code per rule)")

    # Account names + reserved names
    assert validate_account_name("Player_1", ["aether"]) is None
    assert validate_account_name("abc", []) is None
    for bad in ("ab", "_abc", "a" * 21, "has space", "", None):
        assert validate_account_name(bad, ["aether"])[0] == "name_invalid", bad
    for bad in ("aether", "AETHER", "relicx", "tplfoo", "admin1", "GM_x"):
        assert validate_account_name(bad, ["aether"])[0] == "name_reserved", bad
    assert PASSWORD_RE.match("Secret12") and not PASSWORD_RE.match("short") and not PASSWORD_RE.match("has space1")
    assert parse_sdk_register_reply("<p>Account created. Please close this page</p>") == "created"
    assert parse_sdk_register_reply("Account with that username already exists.") == "taken"
    assert parse_sdk_register_reply("Password must consists of at least 8 characters.") == "refused:password too short"
    assert parse_sdk_register_reply("<html>nothing</html>") is None
    ok("account-name validation + reserved names + sdk reply parsing OK")

    # Policy: defaults, partial merge, validation against the manifest templates
    base = default_policy()
    assert base["signup"]["enabled"] is True and base["playerCommands"] is True, \
        "signup + player commands are on by default (agent 3.4)"
    # agent 3.7: every starting point the manifest ships is offered by default (the new account
    # first, then the manifest's order) and a launcher may create 5 accounts per version
    for v in VERSIONS:
        assert base["signup"]["versions"][v] == {
            "templates": ["fresh"] + [t["id"] for t in manifest_templates(v)],
            "maxPerDay": 50, "maxPerPlayer": 5}, base["signup"]["versions"][v]
    if read_manifest("1.6") and read_manifest("2.8"):
        assert base["signup"]["versions"]["1.6"]["templates"] == ["fresh", "pre-gaa", "post-gaa"], base
        assert base["signup"]["versions"]["2.8"]["templates"] == ["fresh", "pre-gaa", "post-gaa"], base
    allowed = {"1.6": ["pre-gaa", "post-gaa"], "2.8": ["pre-gaa"]}
    base_old = merge_policy({"name": "", "signup": {"enabled": True, "versions": {
        v: {"templates": ["fresh"], "maxPerDay": 50, "maxPerPlayer": 5} for v in VERSIONS}},
        "playerCommands": True, "hotpatchUpstream": True, "hotpatchSource": HOTPATCH_SOURCE}, {}, allowed)
    p1 = merge_policy(base_old, {"signup": {"enabled": True, "versions": {"1.6": {"templates": ["fresh", "post-gaa", "fresh"]}}},
                                 "name": " My GIO ", "playerCommands": True}, allowed)
    assert p1["signup"]["enabled"] and p1["name"] == "My GIO" and p1["playerCommands"] is True
    assert p1["signup"]["versions"]["1.6"] == {"templates": ["fresh", "post-gaa"], "maxPerDay": 50, "maxPerPlayer": 5}, p1
    assert p1["signup"]["versions"]["2.8"] == {"templates": ["fresh"], "maxPerDay": 50, "maxPerPlayer": 5}, \
        "untouched version keeps what it had"
    # maxPerPlayer: 0..1000 (0 = nobody may create more), refused otherwise; a policy stored before
    # 3.7 (no maxPerPlayer) takes the default of 5 when it is merged over default_policy()
    for m in (0, 1, 7, MAX_PER_PLAYER_MAX):
        assert merge_policy(p1, {"signup": {"versions": {"2.8": {"maxPerPlayer": m}}}}, allowed)[
            "signup"]["versions"]["2.8"]["maxPerPlayer"] == m
    stored_36 = {"signup": {"enabled": True, "versions": {"2.8": {"templates": ["fresh", "pre-gaa"], "maxPerDay": 50}}}}
    got = merge_policy(default_policy(), stored_36, allowed)["signup"]["versions"]["2.8"]
    assert got == {"templates": ["fresh", "pre-gaa"], "maxPerDay": 50, "maxPerPlayer": 5}, got
    # a merge that flips both OFF proves the input is not mutated (with the defaults on, a merge to
    # True could not tell)
    p_off = merge_policy(base, {"signup": {"enabled": False}, "playerCommands": False}, allowed)
    assert p_off["signup"]["enabled"] is False and p_off["playerCommands"] is False
    assert base["signup"]["enabled"] is True and base["playerCommands"] is True, "merge must not mutate its input"
    p2 = merge_policy(p1, {"signup": {"versions": {"2.8": {"maxPerDay": 7}}}}, allowed)
    assert p2["signup"]["versions"]["2.8"]["maxPerDay"] == 7 and p2["signup"]["versions"]["1.6"]["templates"] == ["fresh", "post-gaa"]
    assert p1["hotpatchUpstream"] is True, "the upstream fetch is on by default"
    assert merge_policy(p1, {"hotpatchUpstream": False}, allowed)["hotpatchUpstream"] is False
    # agent 3.4: the mirror source is a policy CHOICE seeded from the environment, and the default
    # is the Internet Archive -- the whole point of the bundles.
    assert base["hotpatchSource"] == HOTPATCH_SOURCE and HOTPATCH_SOURCE in HOTPATCH_SOURCES
    assert os.environ.get("GIO_HOTPATCH_SOURCE") or default_policy()["hotpatchSource"] == "archive", \
        "archive.org is the default mirror source"
    assert merge_policy(p1, {"hotpatchSource": " CDN "}, allowed)["hotpatchSource"] == "cdn", \
        "the choice is trimmed and lower-cased before it is stored"
    assert merge_policy(p1, {"hotpatchSource": "archive"}, allowed)["hotpatchSource"] == "archive"
    for bad in ({"signup": {"versions": {"2.8": {"templates": ["post-gaa"]}}}},
                {"signup": {"versions": {"3.0": {}}}},
                {"signup": {"enabled": "yes"}},
                {"playerCommands": 1},
                {"hotpatchUpstream": "no"},
                {"hotpatchSource": "internet-archive"},
                {"hotpatchSource": ""},
                {"hotpatchSource": True},
                {"name": "x" * 65},
                {"signup": {"versions": {"1.6": {"maxPerDay": -1}}}},
                {"signup": {"versions": {"1.6": {"maxPerDay": True}}}},
                {"signup": {"versions": {"1.6": {"maxPerPlayer": -1}}}},
                {"signup": {"versions": {"1.6": {"maxPerPlayer": MAX_PER_PLAYER_MAX + 1}}}},
                {"signup": {"versions": {"1.6": {"maxPerPlayer": True}}}},
                {"signup": {"versions": {"1.6": {"maxPerPlayer": "5"}}}},
                []):
        try:
            merge_policy(p1, bad, allowed)
            raise AssertionError("merge_policy must refuse %r" % (bad,))
        except AgentError as e:
            assert e.status == 400
    # the public projection of status + policy exposes only the documented fields
    full = {"platform": "linux", "versions": {
        "1.6": {"present": True, "up": True, "healthy": True, "account": "aether", "passwordVerify": False,
                "provisionedAt": "2026-08-14 21:03:11", "dir": "/home/1.6_live", "project": "16_live",
                "advertisedIp": "198.51.100.171", "servicesDown": [],
                "templates": {"post-gaa": {"md5": "0123abcd", "createdAt": "2026-08-14 21:09:59", "error": None}}},
        "2.8": {"present": False, "dir": "/home/2.8_live"}}, "running": ["16_live"], "busy": None,
        "error": "docker said something"}
    pub = build_public_status(full, p1)
    assert pub["name"] == "My GIO" and pub["agent"] == "3" and pub["platform"] == "linux" and pub["playerCommands"]
    assert pub["versions"]["1.6"] == {"present": True, "up": True, "healthy": True, "defaultAccount": "aether",
                                      "passwordVerify": False, "generation": "2026-08-14 21:03:11"}, pub
    assert pub["versions"]["2.8"] == {"present": False}
    assert pub["signup"]["enabled"] and pub["signup"]["versions"]["1.6"]["maxPerDay"] == 50
    assert pub["signup"]["versions"]["1.6"]["maxPerPlayer"] == 5, "agent 3.7: the launcher shows N of maxPerPlayer"
    assert [t["id"] for t in pub["signup"]["versions"]["1.6"]["templates"]] == ["fresh", "post-gaa"]
    assert "2.8" not in pub["signup"]["versions"], "a version that is not present advertises no signup"
    # agent 3.5: a non-fresh template is offered only once it is imported on that stack (no record, a
    # record without createdAt, a failed import = omitted); 'fresh' needs no import and always stays
    for recs in (None, {}, {"post-gaa": {"md5": "0123abcd", "createdAt": None, "error": None}},
                 {"post-gaa": {"md5": "0123abcd", "createdAt": "2026-08-14 21:09:59", "error": "import failed"}},
                 {"post-gaa": "garbage"}, {"pre-gaa": {"md5": "x", "createdAt": "2026-08-14 21:09:59"}}):
        e16 = dict(full["versions"]["1.6"], templates=recs)
        pu = build_public_status(dict(full, versions=dict(full["versions"], **{"1.6": e16})), p1)
        assert [t["id"] for t in pu["signup"]["versions"]["1.6"]["templates"]] == ["fresh"], (recs, pu)
    # ...and a version whose list would come out EMPTY is left out of signup.versions altogether (the
    # shipped v1.0 launcher reads templates[0] unguarded); its status entry is unaffected
    ready28 = {"pre-gaa": {"md5": "4567cdef", "createdAt": "2026-09-21 10:00:00", "error": None}}
    for tpls, recs, want in ((["pre-gaa"], None, None),
                             (["pre-gaa"], {"pre-gaa": {"md5": "4567cdef", "createdAt": None, "error": "import failed"}},
                              None),
                             (["pre-gaa"], ready28, ["pre-gaa"]),
                             ([], ready28, None),
                             (["fresh", "pre-gaa"], None, ["fresh"]),
                             (["fresh", "pre-gaa"], ready28, ["fresh", "pre-gaa"])):
        p28 = merge_policy(p1, {"signup": {"versions": {"2.8": {"templates": tpls}}}}, allowed)
        e28 = {"present": True, "up": True, "healthy": True, "account": "aether", "passwordVerify": False,
               "provisionedAt": "2026-09-21 09:00:00", "templates": recs}
        pu = build_public_status(dict(full, versions=dict(full["versions"], **{"2.8": e28})), p28)
        got = pu["signup"]["versions"].get("2.8")
        assert (None if got is None else [t["id"] for t in got["templates"]]) == want, (tpls, recs, pu)
        assert pu["versions"]["2.8"]["present"] is True and pu["versions"]["2.8"]["up"] is True, pu
        assert [t["id"] for t in pu["signup"]["versions"]["1.6"]["templates"]] == ["fresh", "post-gaa"], pu
    assert "0123abcd" not in json.dumps(pub) and "21:09:59" not in json.dumps(pub), "the import record stays private"
    # a server without the default save (progress keep/fixes, account null) answers "" -- the
    # launcher hides the login and never falls back; the progress record itself stays private
    full_keep = {"platform": "linux", "state": {"ok": True}, "versions": {
        "1.6": {"present": True, "up": True, "healthy": True, "account": None, "progress": "keep",
                "defaultAccount": False, "passwordVerify": False, "provisionedAt": "2026-08-14 21:03:11"}}}
    pk = build_public_status(full_keep, p1)
    assert pk["versions"]["1.6"]["defaultAccount"] == "" and pk["versions"]["1.6"]["generation"] == "2026-08-14 21:03:11", pk
    assert "progress" not in json.dumps(pk) and "keep" not in json.dumps(pk), pk
    dumped = json.dumps(pub)
    for leak in ("/home/", "16_live", "198.51.100", "servicesDown", "docker said", "busy", "running"):
        assert leak not in dumped, leak
    # a docker error on the box becomes the fixed statusUnknown flag, never the text; no error = no flag
    assert pub["statusUnknown"] is True and "error" not in pub, pub
    assert "statusUnknown" not in build_public_status(dict(full, error=None), p1)
    # the hotpatch badge: only when the version ships a manifest, revisions only (no URL, no dir)
    full["versions"]["1.6"]["hotpatch"] = {"available": True, "enabled": True, "pending": False,
                                           "complete": True, "res": 3557509, "data": 3526661,
                                           "silence": 3266913, "url": "http://198.51.100.171:18080/x",
                                           "dir": "/var/lib/gio-agent/hotpatch", "voice": ["Japanese"]}
    pub2 = build_public_status(full, p1)
    assert pub2["versions"]["1.6"]["hotpatch"] == {"enabled": True, "res": 3557509, "data": 3526661,
                                                   "silence": 3266913, "voice": ["Japanese"]}, pub2
    assert "198.51.100" not in json.dumps(pub2) and "/var/lib" not in json.dumps(pub2)
    # enabled but pending (database half owed / URL unusable) = not advertised: no badge for players
    full["versions"]["1.6"]["hotpatch"]["pending"] = True
    assert build_public_status(full, p1)["versions"]["1.6"]["hotpatch"]["enabled"] is False
    full["versions"]["1.6"]["hotpatch"]["pending"] = False
    full["versions"]["1.6"]["hotpatch"]["available"] = False
    assert "hotpatch" not in build_public_status(full, p1)["versions"]["1.6"]
    ok("policy merge/validation + public status projection OK")

    # agent 3.7: accounts per launcher -- the key, the book (per version, per generation, dedup,
    # least-recently-used cap) and the limit check against it
    cid = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    assert signup_client_key(cid, "192.0.2.7") == "c:" + cid
    for bad in (None, "", "short", "x" * 65, "has space in it here", 12345, "../../etc/passwd/xxxxxx"):
        assert signup_client_key(bad, "192.0.2.7") == "ip:192.0.2.7", bad
    book = {"versions": {"2.8": {"provisionedAt": "2026-09-20 15:31:15"}}}
    assert signup_book_names(book, "2.8", "c:" + cid) == []
    assert signup_book_add(book, "2.8", "c:" + cid, "Friend1") == ["Friend1"]
    assert signup_book_add(book, "2.8", "c:" + cid, "friend1") == ["Friend1"], "case-insensitive: counted once"
    assert signup_book_add(book, "2.8", "c:" + cid, "Friend2") == ["Friend1", "Friend2"]
    assert signup_book_names(book, "2.8", "c:" + cid) == ["Friend1", "Friend2"]
    assert signup_book_names(book, "1.6", "c:" + cid) == [] and signup_book_names(book, "2.8", "ip:192.0.2.7") == []
    book["versions"]["2.8"]["provisionedAt"] = "2026-10-01 10:00:00"  # a default re-provision wiped them
    assert signup_book_names(book, "2.8", "c:" + cid) == [], "another generation counts nothing"
    assert signup_book_add(book, "2.8", "c:" + cid, "Friend3") == ["Friend3"]
    assert book["signupClients"]["2.8"]["gen"] == "2026-10-01 10:00:00"
    for i in range(5):
        signup_book_add(book, "2.8", "c:k%d" % i, "N%d" % i, cap=3)
    assert list(book["signupClients"]["2.8"]["clients"]) == ["c:k2", "c:k3", "c:k4"], "LRU cap"
    signup_book_add(book, "2.8", "c:k2", "N2b", cap=3)
    assert list(book["signupClients"]["2.8"]["clients"])[-1] == "c:k2", "a use moves the key to the end"
    assert signup_book_names({"signupClients": "garbage", "versions": {}}, "2.8", "c:x") == []
    assert signup_book_names({"signupClients": {"2.8": {"gen": None, "clients": {"c:x": ["A", 3, None]}}}},
                             "2.8", "c:x") == ["A"], "a hand-edited book is read defensively"
    saved_load = globals()["load_state"]
    try:
        globals()["load_state"] = lambda: json.loads(json.dumps(book))
        assert check_player_limit("2.8", "c:k2", {"maxPerPlayer": 3}) == 2
        for lim in (2, 0):
            try:
                check_player_limit("2.8", "c:k2", {"maxPerPlayer": lim})
                raise AssertionError("maxPerPlayer %d must refuse a launcher that created 2" % lim)
            except AgentError as e:
                assert e.status == 403 and e.code == "account_limit" and e.code in PUBLIC_ERROR_CODES, e
        assert check_player_limit("2.8", "c:unknown", {}) == 0, "no maxPerPlayer = the default"

        def _unreadable():
            raise StateUnreadable("state.json", "test")
        globals()["load_state"] = _unreadable
        try:
            check_player_limit("2.8", "c:k2", {"maxPerPlayer": 5})
            raise AssertionError("an unreadable state must refuse, never count zero")
        except AgentError as e:
            assert e.status == 503, e
    finally:
        globals()["load_state"] = saved_load
    ok("accounts per launcher OK (key, book per version + generation, dedup, LRU cap, limit)")

    # agent 3.7: an event re-opened from the closed window joins the running ones' begin_time
    q = events_open_sql("2014001, 5083001", "2050-01-01 00:00:00", "1998-06-28 03:59:59", "2026-09-10 14:43:35")
    assert "WHEN begin_time > NOW() THEN DATE_SUB(NOW(), INTERVAL 1 DAY)" in q, q
    assert "WHEN begin_time <= '1998-06-28 03:59:59' THEN '2026-09-10 14:43:35'" in q, q
    assert "ELSE begin_time END, end_time = '2050-01-01 00:00:00' WHERE schedule_id IN (2014001, 5083001)" in q, q
    for base in (None, "", "NULL", "2026-09-10", "2026-09-10 14:43:35'; DROP TABLE x; --"):
        q = events_open_sql("5083001", "2050-01-01 00:00:00", "1998-06-28 03:59:59", base)
        assert "WHEN begin_time <= '1998-06-28 03:59:59' THEN DATE_SUB(NOW(), INTERVAL 1 DAY)" in q, (base, q)
        assert "DROP" not in q, q
    ok("events: a parked event re-opens on the running events' begin_time (else yesterday)")

    # agent 3.7: a start imports the templates the stack holds no good record of, and only those
    with tempfile.TemporaryDirectory() as td:
        for n in ("a.sql", "b.sql", "c.sql", "d.sql"):
            with open(os.path.join(td, n), "w") as f:
                f.write(n)
        tpls = [{"id": i, "label": i, "sql": i[0] + ".sql", "md5": None} for i in ("aa", "bb", "cc", "dd", "ee")]
        tpls[4]["sql"] = "missing.sql"
        md5_a = _file_md5(os.path.join(td, "a.sql"))
        vstate = {"progress": "default", "templates": {
            "aa": {"md5": md5_a, "createdAt": "2026-09-29 00:00:00"},                # good
            "bb": {"md5": "0" * 32, "createdAt": "2026-09-29 00:00:00"},             # the dump changed
            "cc": {"error": "import failed", "createdAt": None}}}                     # failed; dd: none
        calls = []
        saved = {k: globals()[k] for k in ("version_state", "manifest_templates", "payload_dir",
                                            "ensure_templates", "version_state_set", "mysql_exec",
                                            "mysql_import")}
        try:
            globals()["version_state"] = lambda v: vstate
            globals()["manifest_templates"] = lambda v, man=None: tpls
            globals()["payload_dir"] = lambda v: td
            assert templates_owed("2.8") == ["bb", "cc", "dd"], templates_owed("2.8")
            globals()["ensure_templates"] = lambda v, j, only=None: calls.append((v, only))
            heal_templates("2.8", _Job())
            assert calls == [("2.8", ["bb", "cc", "dd"])], calls
            vstate["progress"] = "fixes"
            heal_templates("2.8", _Job())
            assert len(calls) == 1, "a 'fixes only' stack keeps its templates out"
            vstate["progress"] = "keep"
            # ensure_templates(only=...) keeps every other record and replaces the ones it redid
            globals()["ensure_templates"] = saved["ensure_templates"]
            written = {}
            globals()["version_state_set"] = lambda v, **kv: written.update(kv)

            class _R:
                returncode, stdout, stderr = 0, "COUNT(*)\n1\n", ""
            globals()["mysql_exec"] = lambda *a, **k: _R()
            globals()["mysql_import"] = lambda *a, **k: _R()
            ensure_templates("2.8", _Job(), only=["dd"])
            assert set(written["templates"]) == {"aa", "bb", "cc", "dd"}, written
            assert written["templates"]["aa"] == vstate["templates"]["aa"] and written["templates"]["dd"]["createdAt"]
            ensure_templates("2.8", _Job())
            assert set(written["templates"]) == {"aa", "bb", "cc", "dd", "ee"} and written["templates"]["ee"]["error"], \
                "without `only` every manifest template is redone and the records replaced whole"
        finally:
            globals().update(saved)
    ok("templates: a start imports only the owed ones (never imported / failed / dump changed), never on 'fixes'")

    # agent 3.7: a settled record of an event the list keeps open is dropped from a save, byte-exact
    # otherwise; the shipped saves carry none; the repair pass writes md5-guarded, backs up first
    def _ev(fn, v):
        return _pb_enc(fn << 3) + _pb_enc(v)

    def _eb(fn, b):
        return _pb_enc((fn << 3) | 2) + _pb_enc(len(b)) + b

    def _act(aid, sched, settled):
        return _eb(1, _ev(1, aid) + _eb(2, _ev(1, sched) + _eb(3, _ev(1, sched) + _ev(2, 1))
                                       + (_ev(5, 1) if settled else b"") + _ev(9, 1)))
    head, tail = _eb(1, _ev(1, 35) + _eb(3, b"aetherlul")), _eb(21, _ev(1, 1))
    pdb = head + _eb(20, _act(2014, 2014001, False) + _act(5083, 5083001, True) + _act(5082, 5082001, True)
                     + _ev(2, 7)) + tail
    want = head + _eb(20, _act(2014, 2014001, False) + _act(5082, 5082001, True) + _ev(2, 7)) + tail
    got, rm = strip_settled_activities(pdb, {2014001, 5083001})
    assert rm == [(5083, 5083001)] and got == want, (rm, got.hex(), want.hex())
    assert strip_settled_activities(got, {2014001, 5083001}) == (got, []), "idempotent"
    assert strip_settled_activities(pdb, {2014001}) == (pdb, []), "a closed event's settled record stays"
    zb, zrm = strip_settled_blob(b"ZLIB" + zlib.compress(pdb), {5083001})
    assert zb[:4] == b"ZLIB" and zlib.decompress(zb[4:]) == want and zrm == [(5083, 5083001)]
    assert strip_settled_blob(pdb, {5083001})[0] == want, "a raw (unwrapped) save works too"
    for junk in (b"ZLIB\x00\x01", b"\xff\xff\xff", b""):
        assert strip_settled_blob(junk, {5083001}) == (junk, []), junk
    for v in VERSIONS:
        man = read_manifest(v)
        if not man:
            continue
        ids = open_schedule_ids(v, man)
        for t in manifest_templates(v, man):
            with open(os.path.join(payload_dir(v), t["sql"]), encoding="utf-8") as f:
                dump = f.read()
            m = re.search(r"INSERT INTO `t_player_data_1`.*?VALUES\s*\(1,\s*'.*?(0x[0-9A-Fa-f]+)", dump, re.S)
            assert m, "no uid 1 save in %s %s" % (v, t["sql"])
            left = strip_settled_blob(bytes.fromhex(m.group(1)[2:]), ids)[1]
            assert not left, "the shipped %s %s save carries a settled record of an open event: %s" % (v, t["id"], left)
    with tempfile.TemporaryDirectory() as td:
        blob1 = b"ZLIB" + zlib.compress(pdb)
        clean = b"ZLIB" + zlib.compress(want)
        md5s = {1: hashlib.md5(blob1).hexdigest(), 11: hashlib.md5(clean).hexdigest()}
        writes, answer = [], {"rc": "1"}

        class _Rp:
            def __init__(self, out):
                self.returncode, self.stdout, self.stderr = 0, out, ""

        def fake_exec(_v, sql, db=None, timeout=None, password=None):
            if "FROM t_player_data_1" in sql:
                b64 = lambda b: base64.b64encode(b).decode()
                return _Rp("uid\tMD5(bin_data)\tTO_BASE64(bin_data)\n1\t%s\t%s\n11\t%s\t%s\n"
                           % (md5s[1], b64(blob1)[:76] + "\\n" + b64(blob1)[76:], md5s[11], b64(clean)))
            return _Rp("uid\tMD5(bin_data)\tTO_BASE64(bin_data)\n")

        def fake_import(_v, path, db, timeout=None):
            with open(path, encoding="utf-8") as f:
                writes.append(f.read())
            return _Rp("ROW_COUNT()\n%s\n" % answer["rc"])
        saved = {k: globals()[k] for k in ("mysql_exec", "mysql_import", "read_manifest", "STATE_PATH")}
        try:
            globals().update({"mysql_exec": fake_exec, "mysql_import": fake_import, "STATE_PATH": os.path.join(td, "state.json"),
                              "read_manifest": lambda _v: {"events": {"gaa": {"2014001": "x", "5083001": "y"}}}})
            j = _Job()
            assert repair_settled_activities("2.8", j) == 1, j.lines
            assert len(writes) == 1 and "WHERE uid = 1 AND MD5(bin_data) = '%s'" % md5s[1] in writes[0], writes
            new_hex = strip_settled_blob(blob1, {5083001})[0].hex()
            assert "SET bin_data = 0x%s " % new_hex in writes[0] and "t_player_data_1" in writes[0]
            backups = os.listdir(os.path.join(td, "backups"))
            assert len(backups) == 1 and blob1.hex() in open(os.path.join(td, "backups", backups[0])).read()
            answer["rc"] = "0"  # the save moved between the read and the write: left for the next pass
            j2 = _Job()
            assert repair_settled_activities("2.8", j2) == 0 and any("not rewritten" in l for l in j2.lines), j2.lines
            globals()["mysql_exec"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker is gone"))
            j3 = _Job()
            assert repair_settled_activities("2.8", j3) == 0 and "WARNING" in j3.lines[-1], "never raises"
        finally:
            globals().update(saved)
    ok("settled event records: dropped only for open events (byte-exact, idempotent, zlib/raw), none in the "
       "shipped saves, the repair writes md5-guarded after a backup and never raises")

    # Public job snapshots: no lines, generic 5xx text, 4xx text kept, result limited to 4 fields
    j = Job("signup", "1.6", job_id=secrets.token_urlsafe(16))
    j.log("secret path /home/1.6_live")
    j.state, j.error, j.error_status = "error", "MySQL: /home/1.6_live/x failed", 500
    snap = j.public_snapshot()
    assert snap["error"] == "server error, try later" and "lines" not in snap and "version" not in snap, snap
    assert snap["errorCode"] == "server_error", snap
    # a 5xx never exposes a code more specific than its (generic) text, whatever the AgentError said
    for status, code in ((503, "version_down"), (503, "template_not_ready"), (500, None), (502, "x")):
        j.error, j.error_status, j.error_code = "detail", status, code
        assert j.public_snapshot()["errorCode"] == "server_error" and j.public_snapshot()["error"] == "server error, try later"
    j.error, j.error_status, j.error_code = "name taken", 409, "name_taken"
    assert j.public_snapshot()["error"] == "name taken" and j.public_snapshot()["errorCode"] == "name_taken"
    j.error_code = None
    assert j.public_snapshot()["errorCode"] is None, "a 4xx without a code stays null"
    j.state, j.error, j.result = "done", None, {"name": "Bob", "uid": 5, "template": "pre-gaa", "nickname": "SwordGuy", "backup": "/x"}
    snap = j.public_snapshot()
    assert snap["result"] == {"name": "Bob", "uid": 5, "template": "pre-gaa", "nickname": "SwordGuy"}, snap
    assert snap["errorCode"] is None, snap
    assert set(snap) == {"id", "kind", "state", "elapsed", "error", "errorCode", "result"}, snap
    # _run_job keeps the AgentError's code
    jr = Job("signup", "1.6", job_id="coded")
    _run_job(jr, lambda _j: (_ for _ in ()).throw(AgentError(409, "name taken", code="name_taken")))
    for _ in range(200):
        if jr.state != "running":
            break
        time.sleep(0.01)
    assert jr.state == "error" and jr.error_code == "name_taken" and jr.public_snapshot()["errorCode"] == "name_taken"
    # _send_error carries the code (and only when there is one)
    sent = []
    fake = Handler.__new__(Handler)
    fake._send = lambda status, obj, retry_after=None: sent.append((status, obj, retry_after))
    fake._send_error(AgentError(429, "too many requests, try again later", retry_after=1200, code="quota_ip_hour"))
    fake._send_error(AgentError(400, "plain"))
    assert sent[0] == (429, {"error": "too many requests, try again later", "code": "quota_ip_hour",
                             "retryAfter": 1200}, 1200), sent
    assert sent[1] == (400, {"error": "plain"}, None), sent

    # Signup quota fairness on fakes (no docker, no sdk.db): a busy slot spends nothing; a taken name
    # found by the pre-check spends no create token; a busy race after the take is refunded; a spent
    # namecheck budget falls back to quota-then-lookup, where a taken name is NOT refunded but an
    # unreadable account database is (answered as a path-free server_error on both paths); the job
    # refunds a cheap failure before registering, never a taken name or a failed database probe.
    g = globals()
    faked = ("get_policy", "reserved_names", "version_state", "public_status", "_sdk_db_lookup", "current_job",
             "start_public_job", "LIMITER", "log_line", "is_bootstrapped", "mysql_reachable")
    saved_fair = {k: g[k] for k in faked}
    fs = {"busy": None, "race": False, "broken": False}

    def fake_lookup(_version, name):
        if fs["broken"]:
            raise AgentError(500, "The stack's account database is missing (/x/sdk.db).")
        return (7, name) if name.lower() == "taken1" else None

    def fake_start(kind, version, fn):
        if fs["race"]:
            raise public_busy_error()
        return Job(kind, version, job_id="fake-signup")

    try:
        g.update({"get_policy": lambda: {"signup": {"enabled": True, "versions": {
                      "1.6": {"templates": ["fresh", "pre-gaa"], "maxPerDay": 50}}}, "playerCommands": False},
                  "reserved_names": lambda _v: ["aether"], "version_state": lambda _v: {},
                  "public_status": lambda: {"versions": {"1.6": {"up": True}}},
                  "_sdk_db_lookup": fake_lookup, "current_job": lambda **_k: fs["busy"],
                  "start_public_job": fake_start, "LIMITER": RateLimiter(), "log_line": lambda *_a: None,
                  "is_bootstrapped": lambda _v: True, "mysql_reachable": lambda _v: False})
        hq = Handler.__new__(Handler)
        hq._send = lambda status, obj, retry_after=None: obj
        cip = "203.0.113.20"

        def create(name, template="fresh"):
            try:
                return "started" if hq._public_create({"version": "1.6", "name": name, "template": template}, cip) \
                    .get("job") else "?"
            except AgentError as e:
                return e.code

        def spent(key, limit):
            bucket = LIMITER._buckets.get(key)
            return 0 if bucket is None else int(round(limit - bucket[0]))

        fs["busy"] = Job("start", "1.6", job_id="admin-start")
        assert create("Fresh1") == "server_busy" and not LIMITER._buckets, "busy must spend no token at all"
        fs["busy"] = None
        assert create("aetheR") == "name_reserved" and create("x") == "name_invalid" and not LIMITER._buckets
        assert create("Fresh1", "pre-gaa") == "template_not_ready" and not LIMITER._buckets
        assert create("Taken1") == "name_taken"
        assert spent("create-h:" + cip, 3) == 0 and spent("namecheck:" + cip, 6) == 1, dict(LIMITER._buckets)
        fs["broken"] = True  # a missing sdk.db on the pre-check: coded, no server path, no create token
        try:
            hq._public_create({"version": "1.6", "name": "Fresh0", "template": "fresh"}, cip)
            raise AssertionError("an unreadable account database must fail the request")
        except AgentError as e:
            assert (e.status, e.code) == (500, "server_error") and "sdk.db" not in e.message, (e.code, e.message)
        assert spent("create-h:" + cip, 3) == 0 and spent("namecheck:" + cip, 6) == 2, dict(LIMITER._buckets)
        fs["broken"] = False
        assert create("Fresh1") == "started" and spent("create-h:" + cip, 3) == 1 and spent("create-d:*:1.6", 50) == 1
        fs["race"] = True
        assert create("Fresh2") == "server_busy"
        assert spent("create-h:" + cip, 3) == 1 and spent("create-d:" + cip, 5) == 1 and spent("create-d:*:1.6", 50) == 1, \
            "a busy race after the take must be refunded"
        fs["race"] = False
        while LIMITER.take_all(namecheck_rules(cip))[0]:
            pass  # spend the namecheck budget: the old order must still work
        assert create("Taken1") == "name_taken" and spent("create-h:" + cip, 3) == 2, \
            "a taken name on the fallback path is never refunded (it would be a free name oracle)"
        fs["broken"] = True
        try:
            hq._public_create({"version": "1.6", "name": "Fresh3", "template": "fresh"}, cip)
            raise AssertionError("an unreadable account database must fail the request")
        except AgentError as e:
            assert (e.status, e.code) == (500, "server_error") and "sdk.db" not in e.message, (e.code, e.message)
            assert spent("create-h:" + cip, 3) == 2, "a lookup failure after the take is refunded"
        fs["broken"] = False
        assert create("Fresh4") == "started" and spent("create-h:" + cip, 3) == 3
        assert create("Fresh5") == "quota_ip_hour" and spent("create-d:" + cip, 5) == 3, "a refusal spends no other bucket"
        # inside the job: a cheap failure before registration is refunded; a taken name or a failed
        # database probe (version_down = a docker exec holding the slot) never is
        jip = "203.0.113.30"
        q = signup_quota_rules(jip, "1.6", {"maxPerDay": 50})
        jd = Job("signup", "1.6", job_id="fair-job")
        kept = 0
        for name, reachable, template, want, refunded in (("Fresh8", True, "pre-gaa", "template_not_ready", True),
                                                          ("Fresh9", False, "fresh", "version_down", False),
                                                          ("Taken1", True, "fresh", "name_taken", False)):
            g["mysql_reachable"] = lambda _v, _r=reachable: _r
            assert LIMITER.take_all(q)[0] and spent("create-h:" + jip, 3) == kept + 1
            try:
                do_signup("1.6", jd, name, None, template, jip, refund=q)  # fails before _sdk_register: no network
                raise AssertionError("the job must fail with " + want)
            except AgentError as e:
                assert e.code == want, e.code
            kept += 0 if refunded else 1
            assert spent("create-h:" + jip, 3) == kept and spent("create-d:*:1.6", 50) == 3 + kept, \
                (name, dict(LIMITER._buckets))
    finally:
        g.update(saved_fair)
    ok("signup quota fairness OK (busy/pre-check/refund/namecheck fallback/in-job refund)")
    # the public registry: cap + TTL purge, never visible through the admin list
    with _jobs_lock:
        PUBLIC_JOBS.clear()
        PUBLIC_JOBS_ORDER.clear()
        for i in range(PUBLIC_JOB_CAP + 5):
            jj = Job("signup", "1.6", job_id="pub%d" % i)
            jj.state, jj.finished = "done", time.time() - (PUBLIC_JOB_TTL + 1 if i < 3 else 0)
            PUBLIC_JOBS[jj.id] = jj
            PUBLIC_JOBS_ORDER.append(jj.id)
        _purge_public_jobs()
        assert len(PUBLIC_JOBS_ORDER) == PUBLIC_JOB_CAP and "pub0" not in PUBLIC_JOBS and "pub54" in PUBLIC_JOBS
        assert not (set(PUBLIC_JOBS) & set(JOBS))
        PUBLIC_JOBS.clear()
        PUBLIC_JOBS_ORDER.clear()
    ok("public job snapshot sanitization + registry cap/TTL OK")

    # Admin account creation (agent 3.5) on fakes (no docker, no sdk.db, no network): no policy and no
    # quota (signup OFF, a template the policy never listed), every in-job refusal before anything is
    # registered, the password only in the job result (verify on) and never in a job line or the
    # journal, the rollback of a failed copy, the 400-only preflight, the route -- and do_signup
    # giving the same result through the shared _register_and_seed.
    faked_ac = ("get_policy", "read_manifest", "version_state", "require_state_readable", "is_bootstrapped",
                "mysql_reachable", "_sdk_db_lookup", "_sdk_register", "_resolve_player", "copy_save", "_mysql_q",
                "_signup_rollback", "LIMITER", "log_line", "start_job", "TOKEN")
    saved_ac = {k: g[k] for k in faked_ac}
    ac = {"accounts": {"taken1": (7, "Taken1")}, "registered": [], "copies": [], "rollbacks": [], "resolved": [],
          "journal": [], "policy": 0, "verify": False, "copyFail": False, "booted": True, "mysql": True,
          "started": [],
          "tpl": {"pre-gaa": {"md5": "x", "createdAt": "2026-09-21 10:00:00", "error": None}}}
    signup_policy = {"signup": {"enabled": False, "versions": {}}, "playerCommands": False}

    def ac_policy():
        ac["policy"] += 1
        return signup_policy

    def ac_register(_version, name, _job, password=None):
        ac["registered"].append((name, password))
        ac["accounts"][name.lower()] = (100 + len(ac["registered"]), name)

    def ac_resolve(_version, _job, spec, create):
        ac["resolved"].append((spec, create))
        return 1234

    def ac_copy(_version, _job, src_uid, dst_uid, src_db=PLAYER_DB, dry_run=False):
        if ac["copyFail"]:
            raise AgentError(500, "copy failed (selftest)")
        ac["copies"].append((src_uid, dst_uid, src_db))

    def ac_start(kind, version, fn, lock=True, beside_yielding=False):
        ac["started"].append((kind, version, fn, lock, beside_yielding))
        return Job(kind, version, job_id="ac-route")

    def ac_refused(fn, status, code):
        try:
            fn()
        except AgentError as e:
            assert (e.status, e.code) == (status, code), (e.status, e.code, e.message)
            return
        raise AssertionError("must refuse with %d %s" % (status, code))

    try:
        g.update({"get_policy": ac_policy,
                  "read_manifest": lambda _v: {"account": "aether", "templates": [
                      {"id": "pre-gaa", "label": "Before the Archon Quest", "sql": "tpl/pre.sql"},
                      {"id": "post-gaa", "label": "After the Archon Quest", "sql": "tpl/post.sql"}]},
                  "version_state": lambda _v: {"templates": ac["tpl"], "passwordVerify": ac["verify"]},
                  "require_state_readable": lambda: None,
                  "is_bootstrapped": lambda _v: ac["booted"], "mysql_reachable": lambda _v: ac["mysql"],
                  "_sdk_db_lookup": lambda _v, name: ac["accounts"].get(name.lower()),
                  "_sdk_register": ac_register, "_resolve_player": ac_resolve, "copy_save": ac_copy,
                  "_mysql_q": lambda _v, _sql: "SwordGuy\n",
                  "_signup_rollback": lambda _v, _j, uid, name=None: ac["rollbacks"].append((uid, name)),
                  "LIMITER": RateLimiter(),
                  "log_line": lambda *a, **_k: ac["journal"].append(" ".join(str(x) for x in a)),
                  "start_job": ac_start, "TOKEN": "selftest-token"})

        def lines_of(job):
            return job.snapshot()["lines"]

        # (a) signup OFF, a template only the manifest lists (the policy offers nothing): created, the
        # copy read from the template database, no quota bucket touched, the policy never consulted
        ja = Job("accountcreate", "1.6", job_id="ac-a")
        r = do_account_create("1.6", ja, "Friend1", None, "pre-gaa")
        assert r == {"name": "Friend1", "uid": 1234, "template": "pre-gaa", "nickname": "SwordGuy",
                     "passwordVerify": False, "passwordGenerated": False, "password": None}, r
        assert ac["copies"] == [(1, 1234, template_db_name("pre-gaa"))] and ac["resolved"] == [("Friend1", True)]
        assert not LIMITER._buckets and ac["policy"] == 0, "an admin creation takes no quota and reads no policy"
        assert any("Admin account creation: name=Friend1 version=1.6 template=pre-gaa" in l for l in lines_of(ja))
        # (g) verification off: the generated password went to the sdk but is never returned
        assert PASSWORD_RE.match(ac["registered"][-1][1] or ""), ac["registered"]
        # (b) fresh: no uid until the first login, nothing copied
        r = do_account_create("1.6", Job("accountcreate", "1.6", job_id="ac-b"), "Friend2", None, "fresh")
        assert r["uid"] is None and r["nickname"] is None and r["template"] == "fresh" and len(ac["copies"]) == 1, r
        # (c) a taken name (any case) never reaches the sdk; (d) neither does a template that is not
        # imported here (no record, or a failed import); nor a stack not installed / not running
        n_reg = len(ac["registered"])
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "TAKEN1", None, "fresh"),
                   409, "name_taken")
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "Friend3", None, "post-gaa"),
                   409, "template_not_ready")
        # ...and its text names the real, non-destructive ways to import (never a `default` prepare,
        # which wipes the player accounts)
        try:
            do_account_create("1.6", Job("accountcreate", "1.6"), "Friend3", None, "post-gaa")
        except AgentError as e:
            assert "/server/templates/ensure" in e.message and "Import" in e.message, e.message
            assert "prepare" not in e.message.lower() and "provision" not in e.message.lower(), e.message
        ac["tpl"]["post-gaa"] = {"md5": "y", "createdAt": "2026-09-21 10:00:00", "error": "import failed"}
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "Friend3", None, "post-gaa"),
                   409, "template_not_ready")
        ac["mysql"] = False
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "Friend3", None, "fresh"),
                   409, "version_down")
        ac["mysql"], ac["booted"] = True, False
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "Friend3", None, "fresh"),
                   409, None)
        ac["booted"] = True
        assert len(ac["registered"]) == n_reg and len(ac["copies"]) == 1, "a refusal registers nothing"
        # (e) verification on, no password typed: one is generated, handed to the sdk and returned --
        # and it appears in no job line and nowhere in the journal
        ac["verify"] = True
        je = Job("accountcreate", "1.6", job_id="ac-e")
        r = do_account_create("1.6", je, "Friend4", None, "pre-gaa")
        gen_pw = r["password"]
        assert isinstance(gen_pw, str) and PASSWORD_RE.match(gen_pw) and ac["registered"][-1] == ("Friend4", gen_pw)
        assert r["passwordVerify"] is True and r["passwordGenerated"] is True, r
        assert not any(gen_pw in l for l in lines_of(je)) and not any(gen_pw in l for l in ac["journal"]), \
            "the password is never logged"
        # (f) verification on, typed: echoed back, not "generated"
        jf = Job("accountcreate", "1.6", job_id="ac-f")
        r = do_account_create("1.6", jf, "Friend5", "Secret12", "fresh")
        assert r["password"] == "Secret12" and r["passwordGenerated"] is False and r["passwordVerify"] is True, r
        assert ac["registered"][-1] == ("Friend5", "Secret12") and not any("Secret12" in l for l in lines_of(jf))
        # (g) verification off, typed: stored (valid if verification is switched on later), not returned
        ac["verify"] = False
        r = do_account_create("1.6", Job("accountcreate", "1.6"), "Friend6", "Secret34", "fresh")
        assert r["password"] is None and r["passwordGenerated"] is False and r["passwordVerify"] is False, r
        assert ac["registered"][-1] == ("Friend6", "Secret34")
        # (h) the copy fails after the registration: the game side is rolled back, 500 progress_failed
        ac["copyFail"] = True
        ac_refused(lambda: do_account_create("1.6", Job("accountcreate", "1.6"), "Friend7", None, "pre-gaa"),
                   500, "progress_failed")
        assert ac["rollbacks"] == [(1234, "Friend7")], "the rollback names the account: %r" % ac["rollbacks"]
        # the real rollback's closing line (streamed live to the admin) names the ACCOUNT, never the version
        jr = Job("accountcreate", "1.6", job_id="ac-h")
        saved_ac["_signup_rollback"]("1.6", jr, 1234, name="Friend7")
        assert any("so 'Friend7' is a plain fresh account" in l for l in lines_of(jr)), lines_of(jr)
        assert not any("'1.6'" in l for l in lines_of(jr)), lines_of(jr)
        jr = Job("accountcreate", "1.6", job_id="ac-h2")
        saved_ac["_signup_rollback"]("1.6", jr, 1234)
        assert any("so the account is a plain fresh account" in l for l in lines_of(jr)), lines_of(jr)
        ac["copyFail"] = False
        assert not any("Secret" in l for l in ac["journal"]), "no password ever reaches the journal"
        assert not LIMITER._buckets and ac["policy"] == 0

        # (i) the preflight: 400s only (a 404 would read as "an older agent"), a signup's name rules
        for body, code in (({"version": "1.6", "name": "x"}, "name_invalid"),
                           ({"version": "1.6"}, "name_invalid"),
                           ({"version": "1.6", "name": None}, "name_invalid"),
                           ({"version": "1.6", "name": "aether"}, "name_reserved"),
                           ({"version": "1.6", "name": "AETHER"}, "name_reserved"),
                           ({"version": "1.6", "name": "relicBob"}, "name_reserved"),
                           ({"version": "1.6", "name": "Bob12", "template": "nope"}, "template_unavailable"),
                           ({"version": "1.6", "name": "Bob12", "password": "short"}, "password_invalid"),
                           ({"version": "1.6", "name": "Bob12", "password": "has space1"}, "password_invalid"),
                           ({"version": "1.6", "name": "Bob12", "password": 12345678}, "password_invalid"),
                           ({"version": "0.0", "name": "Bob12"}, "unknown_version"),
                           ({"name": "Bob12"}, "unknown_version")):
            ac_refused(lambda body=body: admin_create_preflight(body), 400, code)
        assert admin_create_preflight({"version": "1.6", "name": " Bob12 ", "password": ""}) == \
            ("1.6", "Bob12", None, "fresh")
        assert admin_create_preflight({"version": "1.6", "name": "Bob12", "password": None, "template": ""}) == \
            ("1.6", "Bob12", None, "fresh")
        # a manifest template passes even when not imported: readiness is the job's 409, not a 400
        assert admin_create_preflight({"version": " 1.6 ", "name": "Bob12", "password": "Secret12",
                                       "template": "post-gaa"}) == ("1.6", "Bob12", "Secret12", "post-gaa")

        # (j) the route: 202 kind accountcreate through _job_reply (default lock, never beside a yielding
        # job); a bad body is its 400 + code and starts nothing
        def ac_post(body):
            reply = []
            raw = json.dumps(body).encode()
            hh = Handler.__new__(Handler)
            hh.path, hh.rfile = "/server/account/create", io.BytesIO(raw)
            hh.headers = {"Authorization": "Bearer selftest-token", "Content-Length": str(len(raw))}
            hh._send = lambda status_, obj, retry_after=None: reply.append((status_, obj))
            hh.do_POST()
            assert len(reply) == 1, reply
            return reply[0]

        st_, obj = ac_post({"version": "1.6", "name": "Friend8", "template": "pre-gaa", "password": ""})
        assert st_ == 202 and obj == {"ok": True, "async": True, "job": "ac-route", "kind": "accountcreate",
                                      "version": "1.6"}, (st_, obj)
        assert len(ac["started"]) == 1 and ac["started"][0][:2] == ("accountcreate", "1.6") \
            and ac["started"][0][3] is True and ac["started"][0][4] is False, ac["started"]
        r = ac["started"][0][2](Job("accountcreate", "1.6", job_id="ac-j"))  # the job body the route queued
        assert r["name"] == "Friend8" and r["template"] == "pre-gaa" and r["password"] is None, r
        for body, code in (({"version": "1.6", "name": "x"}, "name_invalid"),
                           ({"version": "1.6", "name": "Bob12", "template": "nope"}, "template_unavailable"),
                           ({"version": "9.9", "name": "Bob12"}, "unknown_version")):
            st_, obj = ac_post(body)
            assert st_ == 400 and obj.get("code") == code, (body, st_, obj)
        assert len(ac["started"]) == 1, "a refused body starts no job"

        # (l) it never stops a service: /status keeps reporting servicesDown while it runs
        assert "accountcreate" in SERVICE_REPORT_JOBS and "start" not in SERVICE_REPORT_JOBS \
            and "stop" not in SERVICE_REPORT_JOBS

        # (k) do_signup through the same fakes: the four-field result it always gave (no password keys)
        signup_policy["signup"] = {"enabled": True, "versions": {"1.6": {"templates": ["fresh", "pre-gaa"],
                                                                         "maxPerDay": 50}}}
        n_copies = len(ac["copies"])
        r = do_signup("1.6", Job("signup", "1.6", job_id="ac-k1"), "Player5", "Secret12", "pre-gaa", "203.0.113.40")
        assert r == {"name": "Player5", "uid": 1234, "template": "pre-gaa", "nickname": "SwordGuy"}, r
        assert ac["registered"][-1] == ("Player5", "Secret12") and len(ac["copies"]) == n_copies + 1
        assert ac["copies"][-1] == (1, 1234, template_db_name("pre-gaa"))
        r = do_signup("1.6", Job("signup", "1.6", job_id="ac-k2"), "Player6", None, "fresh", "203.0.113.40")
        assert r == {"name": "Player6", "uid": None, "template": "fresh", "nickname": None}, r
        assert PASSWORD_RE.match(ac["registered"][-1][1] or ""), "a signup without a password gets a generated one"
        ac["copyFail"] = True
        ac_refused(lambda: do_signup("1.6", Job("signup", "1.6"), "Player7", None, "pre-gaa", "203.0.113.40"),
                   500, "progress_failed")
        assert ac["rollbacks"] == [(1234, "Friend7"), (1234, "Player7")], ac["rollbacks"]
        ac["copyFail"] = False

        # (m) agent 3.7: a launcher's account is counted the moment it is registered (a failed progress
        # copy still leaves it there, so it still counts), the next past maxPerPlayer is refused before
        # anything is written, another launcher is not affected (the real book, on the scratch state)
        signup_policy["signup"] = {"enabled": True, "versions": {"1.6": {
            "templates": ["fresh", "pre-gaa"], "maxPerDay": 50, "maxPerPlayer": 2}}}
        k1, k2 = signup_client_key("M" * 32, "203.0.113.41"), signup_client_key("N" * 32, "203.0.113.41")
        r = do_signup("1.6", Job("signup", "1.6", job_id="ac-m1"), "Player8", None, "fresh", "203.0.113.41", client=k1)
        assert r["name"] == "Player8" and signup_book_names(load_state(), "1.6", k1) == ["Player8"]
        ac["copyFail"] = True
        ac_refused(lambda: do_signup("1.6", Job("signup", "1.6"), "Player9", None, "pre-gaa", "203.0.113.41",
                                     client=k1), 500, "progress_failed")
        ac["copyFail"] = False
        assert signup_book_names(load_state(), "1.6", k1) == ["Player8", "Player9"], "the account exists: it counts"
        n_reg = len(ac["registered"])
        ac_refused(lambda: do_signup("1.6", Job("signup", "1.6"), "Player10", None, "fresh", "203.0.113.41",
                                     client=k1), 403, "account_limit")
        assert len(ac["registered"]) == n_reg, "refused before the registration"
        r = do_signup("1.6", Job("signup", "1.6", job_id="ac-m2"), "Player10", None, "fresh", "203.0.113.41", client=k2)
        assert r["name"] == "Player10" and signup_book_names(load_state(), "1.6", k2) == ["Player10"]
        r = do_signup("1.6", Job("signup", "1.6", job_id="ac-m3"), "Player11", None, "fresh", "203.0.113.41")
        assert r["name"] == "Player11", "no client (the admin path's shape): nothing counted, nothing refused"
        assert "Player11" not in json.dumps(load_state().get("signupClients"))
    finally:
        g.update(saved_ac)
    ok("admin account creation OK (no policy/quota, in-job 409s, password only in the result, rollback, "
       "400-only preflight, route, service report, do_signup unchanged)")

    # Windows bootstrap: the vendor bootstrap.bat's eight compose steps, as argv, in order, with
    # the two output checks where the one-shot containers' exit codes say nothing.
    plan = bootstrap_plan_windows(has_compose=True)
    runs = [s[1] for s in plan if s[0] == "run"]
    assert runs == [
        ["docker", "compose", "kill"], ["docker", "compose", "rm", "-f"],
        ["docker", "network", "prune", "-f"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "up", "preparevars"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "kill", "preparevars"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "rm", "-f", "preparevars"],
        ["docker", "compose", "up", "-d", "mysql", "redis", "phpmyadmin", "adminer"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "up", "preparedb"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "kill", "preparedb"],
        ["docker", "compose", "-f", "docker-preinstall.yml", "rm", "-f", "preparedb"],
        ["docker", "compose", "down"], ["docker", "compose", "up", "-d"]], runs
    kinds = [s[0] for s in plan]
    assert kinds[2:4] == ["rmtree", "rmtree"] and plan[2][1] == "mysql" and plan[3][1] == "redis"
    assert kinds.index("lock") == len(plan) - 3, "the lock is written after preparedb, before down/up"
    assert [s[2] for s in plan if s[0] == "run" and s[2]] == ["rendered", "database"]
    assert [s[1] for s in bootstrap_plan_windows(False) if s[0] == "run"][0] == ["docker", "network", "prune", "-f"]
    ok("Windows bootstrap step list OK (bootstrap.bat mirrored, output checks placed)")

    # GIO_AGENT_LISTEN parsing (once, at import): tolerant of every malformed spelling.
    assert parse_listen("0.0.0.0:18080") == ("0.0.0.0", 18080)
    assert parse_listen("[::]:18099") == ("::", 18099)
    assert parse_listen("18081") == ("127.0.0.1", 18081)
    assert parse_listen("0.0.0.0") == ("127.0.0.1", 18080)
    assert parse_listen("10.0.0.1:junk") == ("10.0.0.1", 18080)
    assert parse_listen("") == ("127.0.0.1", 18080)
    ok("listen address parsing OK")

    # Hotpatch mirror -- the shipped manifest(s): CDN layout, index lines complete, md5/size sane.
    HEX32 = re.compile(r"^[0-9a-f]{32}$")
    shipped = {}
    for v in VERSIONS:
        hman = read_hotpatch_manifest(v)
        if hman is None:
            continue
        shipped[v] = hman
        branch = hman["branch"]
        paths = [f["path"] for f in hman["files"]]
        assert len(paths) == len(set(paths)), "duplicate paths in the %s hotpatch manifest" % v
        for f in hman["files"]:
            assert f.get("role") in ("index", "payload"), f
            assert HEX32.match(f["md5"]), f
            assert isinstance(f["size"], int) and f["size"] > 0, f
            assert f["path"].startswith(("client_game_res/%s/" % branch, "client_design_data/%s/" % branch)), f
            assert ".." not in f["path"] and not f["path"].startswith("/"), f
        rroot = hotpatch_res_root(hman)
        idx = {f["path"][len(rroot):] for f in hman["files"] if f["role"] == "index" and f["path"].startswith(rroot)}
        for need in ("release_res_versions_external", "res_versions_external", "base_revision",
                     "script_version", "AudioAssets/audio_versions", "release_total_size"):
            assert need in idx, "index file %s missing from the %s res output" % (need, v)
        assert hotpatch_data_root(hman) + "data_versions" in paths
        assert hotpatch_data_root(hman, silence=True) + "data_versions" in paths
        assert hman["upstreams"] and all(u.startswith("https://") for u in hman["upstreams"])
        for part in ("res", "data", "silence"):
            assert hman[part]["output"] == "output_%d_%s" % (hman[part]["revision"], hman[part]["suffix"]), part
        # the rendering: CRLF-separated JSON lines, no trailing newline, official separators
        lines = render_res_lines(hman)
        assert not lines.endswith("\n") and "\r\n" in lines and "\n" not in lines.replace("\r\n", "")
        for line in lines.split("\r\n"):
            d = json.loads(line)
            assert set(d) == {"remoteName", "md5", "fileSize"} and line.startswith('{"remoteName": "'), line
        pc = json.loads(render_pc_version(hman))
        assert pc["md5"] == lines and pc["branch"] == branch and pc["version"] == hman["res"]["revision"]
        assert pc["release_total_size"] == "0" and pc["version_suffix"] == hman["res"]["suffix"]
        vt = json.loads(render_version_txt(hman, 3432262))
        assert vt["server"] == 3432262 and vt["client"] == hman["data"]["revision"]
        assert vt["client_silence"] == hman["silence"]["revision"]
        assert set(vt["client_md5"]) == {"PC"} and set(vt["client_silence_md5"]) == {"PC"}
        assert json.loads(vt["client_md5"]["PC"])["remoteName"] == "data_versions"
        assert json.loads(vt["client_silence_md5"]["PC"])["remoteName"] == "data_versions"
        assert vt["client_version_suffix"] == hman["data"]["suffix"]
        payload_b = sum(f["size"] for f in hman["files"])
        ok("hotpatch manifest %s OK (%d files, %.1f MB, res %d / data %d / silence %d; PC_version.txt + "
           "version.txt render and round-trip)" % (v, len(hman["files"]), payload_b / 1e6,
                                                   hman["res"]["revision"], hman["data"]["revision"],
                                                   hman["silence"]["revision"]))
    if "1.6" in shipped:
        assert len(shipped["1.6"]["files"]) == 37, len(shipped["1.6"]["files"])
        assert sum(f["size"] for f in shipped["1.6"]["files"] if f["role"] == "payload") == 126259414
        assert sum(1 for f in shipped["1.6"]["files"] if f["role"] == "payload") == 23
        assert (shipped["1.6"]["res"]["revision"], shipped["1.6"]["data"]["revision"],
                shipped["1.6"]["silence"]["revision"]) == (3557509, 3526661, 3266913)

    # Range header parsing: a-b, a-, -n; garbage -> None (whole file); unsatisfiable -> 416.
    assert parse_range(None, 100) is None and parse_range("", 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=-500", 100) == (0, 99)
    assert parse_range("bytes=10-500", 100) == (10, 99)
    for junk in ("bytes=", "bytes=-", "items=0-1", "bytes=0-1,5-6", "bytes=a-b", "0-1",
                 "bytes=" + "9" * 5000 + "-", "bytes=0-" + "1" * 30):  # int() of 4300+ digits raises on 3.11+
        assert parse_range(junk, 100) is None, junk
    for bad_r in ("bytes=100-", "bytes=100-200", "bytes=5-4", "bytes=-0"):
        try:
            parse_range(bad_r, 100)
            raise AssertionError("must be unsatisfiable: " + bad_r)
        except AgentError as e:
            assert e.status == 416
    ok("Range header parsing OK")

    # The enable/disable job on a fake stack with NO network and NO docker: the downloader is
    # replaced by a copy from hotpatch_1.6_ref/ (when that gitignored mirror is present) or by
    # synthetic bytes whose md5 the synthetic manifest carries; docker is "mute".
    saved_hp = (HOTPATCH_DIR, HOTPATCH_URL, PAYLOAD_DIR, STATE_PATH, ADVERTISED_IP, LISTEN_HOST,
                LISTEN_PORT, VERSIONS["1.6"]["dir"], running_projects, _fetch_to_file,
                HOTPATCH_BUNDLE_DIR, HOTPATCH_BUNDLES)
    saved_db = (mysql_exec, run_sql, _compose_stream, verify_stack, mysql_reachable, urllib.request.urlopen)
    ref_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hotpatch_1.6_ref")
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "1.6_live")
            os.makedirs(os.path.join(stack, "server", "data"))
            os.makedirs(os.path.join(stack, "server", "res"))
            with open(os.path.join(stack, "docker-compose.yml.tmpl"), "w") as f:
                f.write('ports:\n  - "%OUTER_IP%:21000:80"\n')
            with open(os.path.join(stack, ".env"), "w") as f:
                f.write("OUTER_IP=192.0.2.10\nMYSQL_ROOT_PASSWORD=x\n")
            with open(os.path.join(stack, "server", "data", "version.txt"), "w") as f:
                f.write("{}")
            with open(os.path.join(stack, "server", "data", "server_data_version.txt"), "w") as f:
                f.write("3432262")
            VERSIONS["1.6"]["dir"] = stack
            HOTPATCH_DIR = os.path.join(td, "hotpatch")
            HOTPATCH_URL = ""
            STATE_PATH = os.path.join(td, "state.json")
            PAYLOAD_DIR = os.path.join(td, "payloads")
            HOTPATCH_BUNDLE_DIR, HOTPATCH_BUNDLES = os.path.join(td, "bundles"), True
            ADVERTISED_IP, LISTEN_HOST, LISTEN_PORT = "198.51.100.171", "0.0.0.0", 18080
            running_projects = lambda: (None, "docker unavailable (selftest)")  # noqa: E731

            # synthetic manifest: 3 index files + 2 payloads with known bytes
            blobs = {}

            def synth(path, role, body):
                blobs[path] = body
                return {"path": path, "md5": hashlib.md5(body).hexdigest(), "size": len(body), "role": role}

            sres, sdat, ssil = "output_11_aa", "output_22_bb", "output_33_cc"
            sbase = "output_5_ee"
            vroot = "client_game_res/1.6_live/%s/client/StandaloneWindows64/AudioAssets/" % sbase
            # what the "CDN" holds under the BASE output: two voice languages, the pre-patch copy of a
            # patched pack, a pack with no language folder and a block -- only the first two are ever served
            vblobs = {vroot + "Japanese/VO_1.pck": os.urandom(700), vroot + "Japanese/VO_2.pck": os.urandom(900),
                      vroot + "Korean/VO_1.pck": os.urandom(500), vroot + "English(US)/VO_9.pck": os.urandom(300),
                      vroot + "Banks0.pck": os.urandom(200),
                      vroot + "Chinese/VO_1.pck": b"c1", vroot + "Chinese/VO_2.pck": b"c22",
                      vroot + "Chinese/VO_3.pck": b"c333", vroot + "Chinese/VO_4.pck": b"c4444",
                      vroot + "Chinese/VO_5.pck": b"c55555", vroot + "Chinese/VO_6.pck": b"c666666",
                      "client_game_res/1.6_live/%s/client/StandaloneWindows64/AssetBundles/blocks/00/7.blk" % sbase: b"blk"}

            def vline(name, patched=False, body=None):
                body = vblobs[vroot + name] if body is None else body
                row = {"remoteName": name, "md5": hashlib.md5(body).hexdigest(), "fileSize": len(body)}
                if patched:
                    row["isPatch"] = True
                return json.dumps(row, separators=(", ", ": "))

            sindex = "\r\n".join([
                vline("Banks0.pck"), vline("Japanese/VO_1.pck"), vline("Japanese/VO_2.pck"), vline("Korean/VO_1.pck"),
                vline("English(US)/VO_9.pck", patched=True, body=b"the patched bytes live in the advertised output"),
                '{"remoteName": "blocks/00/7.blk", "md5": "%s", "fileSize": 3}' % hashlib.md5(b"blk").hexdigest(),
                '{"remoteName": "Japanese/../../x.pck", "md5": "%s", "fileSize": 3}' % ("0" * 32),
                '{"remoteName": "Japanese/deep/x.pck", "md5": "%s", "fileSize": 3}' % ("0" * 32),
                '{"remoteName": "Japanese/bad_md5.pck", "md5": "zz", "fileSize": 3}', "not json at all",
            ] + [vline("Chinese/VO_%d.pck" % i) for i in range(1, 7)]).encode() + b"\r\n"
            rroot = "client_game_res/1.6_live/%s/client/StandaloneWindows64/" % sres
            droot = "client_design_data/1.6_live/%s/client/General/AssetBundles/" % sdat
            sroot = "client_design_data/1.6_live/%s/client_silence/General/AssetBundles/" % ssil
            sman = {"version": "1.6", "branch": "1.6_live", "upstreams": ["https://hk.invalid", "https://cn.invalid"],
                    "res": {"output": sres, "revision": 11, "suffix": "aa", "platform": "StandaloneWindows64",
                            "base": sbase},
                    "data": {"output": sdat, "revision": 22, "suffix": "bb"},
                    "silence": {"output": ssil, "revision": 33, "suffix": "cc"},
                    "voice": {"Japanese": {"files": 2, "bytes": 1600}, "Korean": {"files": 1, "bytes": 500},
                              "Chinese": {"files": 6, "bytes": 27}},
                    "files": [synth(rroot + "release_res_versions_external", "index", sindex),
                              synth(rroot + "base_revision", "index", b"11 aa"),
                              synth(rroot + "AssetBundles/blocks/00/1.blk", "payload", os.urandom(3000)),
                              synth(droot + "data_versions", "index", b'{"remoteName": "y"}\r\n'),
                              synth(droot + "blocks/00/2.blk", "payload", os.urandom(2000)),
                              synth(sroot + "data_versions", "index", b'{"remoteName": "z"}\r\n')]}
            os.makedirs(os.path.join(PAYLOAD_DIR, "1.6"))
            with open(os.path.join(PAYLOAD_DIR, "1.6", "hotpatch.json"), "w") as f:
                json.dump(sman, f)
            fetched_urls = []
            broken = set()

            disk_full, cancel_after = set(), set()
            # archive.org "bundles" (the bundle cases below): rel -> body; stop_mid = the admin presses
            # Stop while THIS transfer runs (half the body kept as .part, FetchStopped); resumed_at
            # records the Range a resumed call started from.
            bblobs, stop_mid, resumed_at = {}, set(), []

            def fake_fetch(url, dst, timeout=60, cap=None, expect_md5=None, expect_size=None, resume=False,
                           should_stop=None, progress=None):
                fetched_urls.append(url)
                rel = urllib.parse.unquote(url.split("/", 3)[3])
                if rel in broken:
                    raise urllib.error.URLError("simulated outage")
                if rel in disk_full:
                    raise MirrorWriteError("[Errno 28] No space left on device")
                if rel in cancel_after:
                    _voice_cancel.set()  # the admin presses Stop while this file is on its way
                src = os.path.join(ref_dir, *rel.split("/"))
                if rel in blobs or rel in vblobs or rel in bblobs:
                    body = blobs.get(rel, vblobs.get(rel, bblobs.get(rel)))
                elif os.path.isfile(src):
                    with open(src, "rb") as f:
                        body = f.read()
                else:
                    raise urllib.error.HTTPError(url, 404, "NoSuchKey", {}, None)
                if cap is not None and len(body) > cap:
                    raise ValueError("over cap")
                if expect_size is not None and len(body) != expect_size:
                    raise ValueError("size %d, expected %d" % (len(body), expect_size))
                if expect_md5 and hashlib.md5(body).hexdigest() != expect_md5:
                    raise ValueError("md5 mismatch")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                have = 0
                if resume and expect_size and os.path.isfile(dst + ".part"):
                    have = os.path.getsize(dst + ".part")  # continue a kept .part, like the real Range resume
                    resumed_at.append(have)
                if rel in stop_mid:
                    with open(dst + ".part", "ab" if have else "wb") as f:
                        f.write(body[have:len(body) // 2])
                    _voice_cancel.set()
                    raise FetchStopped("stopped on request")
                with open(dst + ".part", "ab" if have else "wb") as f:
                    f.write(body[have:])
                if progress is not None:
                    progress(len(body))
                replace_file(dst + ".part", dst)
                return len(body)

            _fetch_to_file = fake_fetch
            j = _Job()
            info = hotpatch_info("1.6")
            assert info["available"] and not info["enabled"] and not info["complete"] and info["files"]["cached"] == 0
            assert info["url"] == "http://198.51.100.171:18080/hotpatch" and info["problem"] is None, info
            # nothing mirrored yet: the voice languages come from the manifest's display totals
            assert info["voice"]["base"] == sbase and info["voice"]["indexed"] is False and info["voice"]["selected"] == []
            assert [(r["name"], r["files"], r["bytes"], r["cached"]) for r in info["voice"]["languages"]] == [
                ("Japanese", 2, 1600, 0), ("Korean", 1, 500, 0), ("Chinese", 6, 27, 0)], info["voice"]
            res = do_hotpatch("1.6", j, True)
            assert res == {"enabled": True, "pending": True, "fetched": 6, "url": info["url"],
                           "source": {"bundle": 0, "cdn": 6}}, res
            assert all(u.startswith("https://hk.invalid/") for u in fetched_urls), fetched_urls[:2]
            assert not any("Bundle skipped" in l for l in j.lines), "a bundle-less manifest is 3.3 behaviour: no noise"
            for path_, body in blobs.items():
                with open(hotpatch_local(path_), "rb") as f:
                    assert f.read() == body, path_
            pc_path = os.path.join(stack, PC_VERSION_TXT)
            vt_path = os.path.join(stack, DATA_VERSION_TXT)
            assert json.loads(read_text(pc_path)) == json.loads(render_pc_version(sman))
            vt = json.loads(read_text(vt_path))
            assert vt["server"] == 3432262 and vt["client"] == 22 and vt["client_silence"] == 33, vt
            assert read_text(vt_path + ".relic-orig") == "{}", "the vendor version.txt must be backed up once"
            hst = version_state("1.6")["hotpatch"]
            assert hst["enabled"] and hst["pending"] and hst["url"] == info["url"] and hst["res"] == 11, hst
            assert any("deferred to the next start" in l for l in j.lines), j.lines
            info = hotpatch_info("1.6")
            assert info["enabled"] and info["pending"] and info["complete"] and info["files"]["cached"] == 6
            brief = hotpatch_brief("1.6")
            assert brief == {"available": True, "enabled": True, "pending": True, "complete": True,
                             "res": 11, "data": 22, "silence": 33, "voice": [], "source": "cdn"}, brief
            assert any("Voice packs NOT served: Japanese, Korean, Chinese" in l for l in j.lines), j.lines
            # re-asserting on a stack that is not up: nothing to change, state stays pending
            assert apply_hotpatch("1.6", _Job(), with_sql=False) == 0
            assert version_state("1.6")["hotpatch"]["pending"] is True

            # what the token-less route may fetch on a miss: a listed file (md5/size known) anywhere,
            # an unlisted file only under the three ADVERTISED outputs with the small cap -- never
            # the base output, the rest of the branch, another branch or another tree
            pol = hotpatch_miss_policy(rroot + "AssetBundles/blocks/00/1.blk")
            assert pol is not None and pol[1] is not None and pol[2] is None, pol
            pol = hotpatch_miss_policy(rroot + "res_versions_medium")
            assert pol is not None and pol[1] is None and pol[2] == HOTPATCH_ONDEMAND_FILE_CAP, pol
            assert hotpatch_miss_policy(sroot + "blocks/00/9.blk")[1] is None
            assert hotpatch_miss_policy(droot + "blocks/00/9.blk")[1] is None
            for nope in ("client_game_res/1.6_live/output_1_base/client/StandaloneWindows64/x.blk",
                         "client_game_res/1.6_live/x", "client_design_data/1.6_live/output_99_zz/y",
                         "client_game_res/2.8_live/%s/x" % sres, "other/%s/x" % sres,
                         "client_game_res/1.6_live/%s" % sres):
                assert hotpatch_miss_policy(nope) is None, nope
            # the on-demand budget counts the UNLISTED files only: walked once, then kept per fetch
            assert ondemand_bytes(reset=True) == 0 and ondemand_bytes() == 0
            extra = hotpatch_local(rroot + "res_versions_medium")
            os.makedirs(os.path.dirname(extra), exist_ok=True)
            with open(extra, "wb") as f:
                f.write(b"m" * 1234)
            with open(extra + ".part", "wb") as f:
                f.write(b"p" * 99)  # a half-written temp file never counts
            assert ondemand_bytes(reset=True) == 0 and ondemand_bytes() == 1234
            assert ondemand_bytes(add=10) == 1244
            os.remove(extra)
            os.remove(extra + ".part")
            ondemand_bytes(reset=True)

            # VOICE PACKS of the base build. The verified release index is the allowlist: only
            # "<Lang>/<file>.pck" lines that are not isPatch; the admin's selection bounds the disk.
            vidx = hotpatch_voice_index(sman)
            assert {k: [e["name"] for e in v] for k, v in vidx.items()} == {
                "Japanese": ["Japanese/VO_1.pck", "Japanese/VO_2.pck"], "Korean": ["Korean/VO_1.pck"],
                "Chinese": ["Chinese/VO_%d.pck" % i for i in range(1, 7)]}, vidx
            jp1, jp2, ko1 = vroot + "Japanese/VO_1.pck", vroot + "Japanese/VO_2.pck", vroot + "Korean/VO_1.pck"
            # nobody selected a language: every base-output path is a 404 -- and a REAL pack is counted
            real_log, logged = globals()["log_line"], []
            globals()["log_line"] = logged.append
            try:
                for nope in (jp1, jp2, ko1, vroot + "English(US)/VO_9.pck", vroot + "Banks0.pck",
                             vroot + "Japanese/VO_3.pck", vroot + "Japanese/deep/x.pck", vroot + "Japanese/bad_md5.pck",
                             vroot.replace("AudioAssets/", "AssetBundles/blocks/00/7.blk"),
                             vroot.replace("AudioAssets/", "AudioAssets")):
                    assert hotpatch_miss_policy(nope) is None, nope
            finally:
                globals()["log_line"] = real_log
            vinfo = {r["name"]: r for r in hotpatch_info("1.6")["voice"]["languages"]}
            assert vinfo["Japanese"]["requested"] == 2 and vinfo["Japanese"]["requestedAt"] and vinfo["Korean"]["requested"] == 1
            assert [("Japanese voice packs" in l, "Korean voice packs" in l) for l in logged] == [
                (True, False), (False, True)], "one line per language an hour, however many packs a login asks for"
            assert hotpatch_info("1.6")["voice"]["indexed"] is True
            # an unknown language / a full disk refuse before anything is persisted or fetched
            for bad_langs, status in ((["Klingon"], 400), (["Japanese", "../x"], 400)):
                try:
                    do_hotpatch_voice("1.6", _Job(), bad_langs)
                    raise AssertionError("must refuse %r" % bad_langs)
                except AgentError as e:
                    assert e.status == status, (bad_langs, e.status, e.message)
            real_free = globals()["_mirror_free_bytes"]
            globals()["_mirror_free_bytes"] = lambda: HOTPATCH_DISK_RESERVE + 1599
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese"])
                raise AssertionError("1600 bytes do not fit into 1599 free ones")
            except AgentError as e:
                assert e.status == 409 and "free disk space" in e.message, e.message
            finally:
                globals()["_mirror_free_bytes"] = real_free
            assert "voice" not in version_state("1.6")["hotpatch"] and not os.path.exists(hotpatch_local(jp1))
            # the job: selection persisted, every pack of the language fetched with the INDEX's md5/size
            del fetched_urls[:]
            jv = _Job()
            res = do_hotpatch_voice("1.6", jv, ["Japanese", "Japanese"])
            assert res == {"voice": ["Japanese"], "fetched": 2, "bytes": 1600, "purged": [], "purgeFailed": [],
                           "source": {"bundle": 0, "cdn": 2}, "bundles": []}, res
            assert version_state("1.6")["hotpatch"]["voiceSource"] == {"Japanese": "cdn"}
            assert jv.yielding is True, "the download phase must let watchdog jobs and signups start"
            # a kept .part is a temp file, never a servable CDN name -- under any alias Windows accepts
            for alias in (".part", ".PART", ".Part", ".part.", ".part%20", ".part%20."):
                assert safe_hotpatch_path(jp1 + alias) is None, alias
            assert safe_hotpatch_path(jp1.replace("VO_1.pck", "VO_1PC~1.PAR")) is None
            assert safe_hotpatch_path(jp1) is not None
            vrow = {r["name"]: r for r in hotpatch_info("1.6")["voice"]["languages"]}["Japanese"]
            assert vrow["diskBytes"] == 1600 == vrow["cachedBytes"], vrow
            assert version_state("1.6")["hotpatch"]["voice"] == ["Japanese"] and version_state("1.6")["hotpatch"]["enabled"]
            for p_ in (jp1, jp2):
                with open(hotpatch_local(p_), "rb") as f:
                    assert f.read() == vblobs[p_], p_
            assert not os.path.exists(hotpatch_local(ko1)) and len(fetched_urls) == 2, fetched_urls
            vinfo = {r["name"]: r for r in hotpatch_info("1.6")["voice"]["languages"]}
            assert vinfo["Japanese"]["selected"] and vinfo["Japanese"]["cached"] == 2 and vinfo["Japanese"]["cachedBytes"] == 1600
            assert not vinfo["Korean"]["selected"] and hotpatch_brief("1.6")["voice"] == ["Japanese"]
            assert do_hotpatch_voice("1.6", _Job(), ["Japanese"])["fetched"] == 0, "cached packs are not fetched again"
            # the mirrored voice packs are the admin's bytes: never part of the anonymous budget
            assert ondemand_bytes(reset=True) == 0 and ondemand_bytes() == 0
            # a selected pack that is missing is fetched on demand: listed-style (md5 + size, no cap)
            os.remove(hotpatch_local(jp1))
            pol = hotpatch_miss_policy(jp1)
            assert pol is not None and pol[2] is None and pol[1]["voice"] == "Japanese", pol
            assert pol[1]["md5"] == hashlib.md5(vblobs[jp1]).hexdigest() and pol[1]["size"] == 700, pol
            assert hotpatch_miss_policy(ko1) is None, "a language nobody selected stays closed"
            assert hotpatch_miss_policy(vroot + "English(US)/VO_9.pck") is None, "a patched pack is never served from the base"
            # ... but only while the hotpatch is enabled, and only while the index is the official one
            version_state_set("1.6", hotpatch=dict(version_state("1.6")["hotpatch"], enabled=False))
            assert hotpatch_miss_policy(jp1) is None
            version_state_set("1.6", hotpatch=dict(version_state("1.6")["hotpatch"], enabled=True))
            with open(hotpatch_local(rroot + "release_res_versions_external"), "rb") as f:
                good_index = f.read()
            with open(hotpatch_local(rroot + "release_res_versions_external"), "wb") as f:
                f.write(good_index.replace(b"VO_1.pck", b"VO_X.pck"))  # same size, another md5
            assert hotpatch_voice_index(sman) == {} and hotpatch_miss_policy(jp1) is None, "a tampered index authorises nothing"
            with open(hotpatch_local(rroot + "release_res_versions_external"), "wb") as f:
                f.write(good_index)
            assert hotpatch_miss_policy(jp1) is not None
            # a failed pack fails the job AFTER the selection is saved; the good ones stay; purge=True
            # deletes the languages that are no longer selected
            broken.add(ko1)
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese", "Korean"])
                raise AssertionError("a failed pack must fail the job")
            except AgentError as e:
                assert e.status == 502 and "Korean/VO_1.pck" in e.message, e.message
            assert version_state("1.6")["hotpatch"]["voice"] == ["Japanese", "Korean"]
            assert os.path.isfile(hotpatch_local(jp1)), "the pack the miss test removed came back"
            broken.clear()
            res = do_hotpatch_voice("1.6", _Job(), ["Korean"], purge=True)
            assert res["voice"] == ["Korean"] and res["fetched"] == 1 and res["purged"] == ["Japanese"], res
            assert not os.path.exists(hotpatch_local(vroot + "Japanese")) and os.path.isfile(hotpatch_local(ko1))
            assert do_hotpatch_voice("1.6", _Job(), [])["voice"] == [] and os.path.isfile(hotpatch_local(ko1))
            assert hotpatch_miss_policy(jp1) is None
            # complete on disk but no longer selected: still served, and the Enable report says so
            jc = _Job()
            _log_voice_coverage("1.6", jc, sman)
            assert any("served from the cache although not selected: Korean" in l for l in jc.lines), jc.lines
            jn = _Job()
            do_hotpatch_voice("1.6", jn, [])
            assert any("selected for the 1.6 mirror: none" in l for l in jn.lines), jn.lines
            assert any("Still served from the cache although not selected: Korean" in l for l in jn.lines), jn.lines
            assert any("NOT served: Japanese, Chinese" in l for l in jc.lines), jc.lines
            # Stop (POST {"cancel": true}): the file on its way finishes, the run ends 409, the rest waits
            cancel_after.add(jp1)
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese"])
                raise AssertionError("a cancelled run must end in 409")
            except AgentError as e:
                assert e.status == 409 and "1 of 2 files" in e.message, e.message
            cancel_after.clear()
            assert os.path.isfile(hotpatch_local(jp1)) and not os.path.exists(hotpatch_local(jp2))
            assert version_state("1.6")["hotpatch"]["voice"] == ["Japanese"]
            # the local disk failing stops the run at once (507), nothing else is even asked for
            disk_full.add(jp2)
            del fetched_urls[:]
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese", "Chinese"])
                raise AssertionError("a local write failure must stop the job")
            except AgentError as e:
                assert e.status == 507 and "stopped" in e.message, (e.status, e.message)
            assert len(fetched_urls) == 1, fetched_urls
            disk_full.clear()
            # five failed files in a row = the CDN is not delivering: stop instead of grinding on
            broken.update(vroot + "Chinese/VO_%d.pck" % i for i in range(1, 7))
            del fetched_urls[:]
            try:
                do_hotpatch_voice("1.6", _Job(), ["Chinese"])
                raise AssertionError("must stop after five failures in a row")
            except AgentError as e:
                assert e.status == 502 and "Five files in a row" in e.message, e.message
            assert len({u.rsplit("/", 1)[1] for u in fetched_urls}) == 5, "the sixth pack is not even tried"
            broken.clear()
            do_hotpatch_voice("1.6", _Job(), [], purge=True)
            # the job slot: a YIELDING job blocks admin jobs, not the watchdog's kinds nor signups
            jy = Job("hotpatch-voice", "1.6")
            with _jobs_lock:
                JOBS[jy.id] = jy
                JOBS_ORDER.append(jy.id)
            try:
                assert current_job() is jy and current_job(skip_yielding=True) is jy
                jy.yielding = True
                assert current_job() is jy and current_job(skip_yielding=True) is None
                assert running_job_of_kind("hotpatch-voice") is jy
                try:
                    start_job("start", "1.6", lambda j_: None)
                    raise AssertionError("an admin job must still wait for the voice job")
                except AgentError as e:
                    assert e.status == 409
                try:
                    start_job("netfix", "1.6", lambda j_: None)
                    raise AssertionError("the ADMIN's netfix waits too: the rule is who starts it, not its kind")
                except AgentError as e:
                    assert e.status == 409
                ran = threading.Event()
                jr = start_job("revive", "1.6", lambda j_: ran.set(), beside_yielding=True)
                assert ran.wait(5), "a watchdog job must start beside a yielding job"
                for _ in range(50):
                    if jr.state != "running":
                        break
                    time.sleep(0.05)
                assert jr.state == "done", jr.state
                # twenty-five watchdog jobs later the RUNNING voice job is still in the registry
                for _ in range(25):
                    jw = start_job("revive", "1.6", lambda j_: None, beside_yielding=True)
                    for _t in range(100):
                        if jw.state != "running":
                            break
                        time.sleep(0.01)
                assert jy.id in JOBS and running_job_of_kind("hotpatch-voice") is jy and len(JOBS_ORDER) <= 21
            finally:
                jy.state = "done"

            # The stack UP (simulated docker + MySQL + compose): the database half runs. The decision
            # is persisted BEFORE the SQL/restart, a restart happens only when something changed (or a
            # previous attempt left it owed), a failed restart / SQL leaves "enabled + pending" (never
            # a half-applied stack under a state that says disabled), a second disable owes nothing.
            db_cols = ["", "", "", ""]
            sql_log, restarts, sql_fail = [], [], [False]

            def fake_mysql_exec(version, sql, db=None, timeout=300, password=None):
                if sql.startswith("SELECT resource_url"):
                    out = "resource_url\tresource_url_bak\tdata_url\tdata_url_bak\n" + "\t".join(db_cols) + "\n"
                    return subprocess.CompletedProcess([], 0, stdout=out, stderr="")
                return subprocess.CompletedProcess([], 0, stdout="1\n1\n", stderr="")

            def fake_run_sql(version, job, statements, tolerate=False):
                for s in statements:
                    sql_log.append(s)
                    if sql_fail[0]:
                        raise AgentError(500, "SQL failed: simulated")
                    m = re.search(r"resource_url='([^']*)'.*data_url='([^']*)'", s)
                    db_cols[:] = [m.group(1), m.group(1), m.group(2), m.group(2)]

            def fake_compose(directory, job, *args, **kw):
                restarts.append(args)
                return 0

            def no_network(*a, **k):
                raise urllib.error.URLError("no network in the selftest")

            mysql_exec, run_sql, _compose_stream = fake_mysql_exec, fake_run_sql, fake_compose
            verify_stack = lambda v, j, rounds=0: None  # noqa: E731
            mysql_reachable = lambda v: True  # noqa: E731
            running_projects = lambda: ([VERSIONS["1.6"]["project"]], None)  # noqa: E731
            urllib.request.urlopen = no_network  # the dispatch probe must not reach out
            open(os.path.join(stack, ".bootstrap.lock"), "w").close()
            write_text(os.path.join(stack, "docker-compose.yml"), "rendered")
            assert stack_up("1.6") == (True, None) and is_bootstrapped("1.6")
            # (1) the pending enable completes: SQL + one restart, pending -> False
            j3 = _Job()
            res = do_hotpatch("1.6", j3, True)
            want_cols = [info["url"] + "/client_game_res"] * 2 + [info["url"] + "/client_design_data/1.6_live"] * 2
            assert res["pending"] is False and db_cols == want_cols, (res, db_cols)
            assert len(restarts) == 1 and restarts[0][0] == "restart" and "dispatch" in restarts[0], restarts
            assert version_state("1.6")["hotpatch"]["pending"] is False
            assert any("dispatch check skipped" in l for l in j3.lines), j3.lines
            # (2) enable again with nothing to change: no UPDATE, no restart, nobody kicked
            j4 = _Job()
            res = do_hotpatch("1.6", j4, True)
            assert res["pending"] is False and len(restarts) == 1, restarts
            assert sum("UPDATE" in s for s in sql_log) == 1 and any("no restart" in l for l in j4.lines), j4.lines
            # (3) a restart that fails: the job fails, the state stays enabled + pending; the re-run
            #     owes the restart even though nothing changes any more
            write_text(pc_path, "{ not json")
            _compose_stream = lambda d, j, *a, **k: 1  # noqa: E731
            try:
                do_hotpatch("1.6", _Job(), True)
                raise AssertionError("a failed restart must fail the job")
            except AgentError as e:
                assert "restarting the services failed" in e.message, e.message
            hst = version_state("1.6")["hotpatch"]
            assert hst["enabled"] is True and hst["pending"] is True, hst
            assert json.loads(read_text(pc_path))["version"] == 11, "the files stay applied"
            _compose_stream = fake_compose
            res = do_hotpatch("1.6", _Job(), True)
            assert res["pending"] is False and len(restarts) == 2, restarts
            # (4) the SQL fails on an enable (the columns were reset): enabled + pending, files in
            #     place; what the next start runs (apply_hotpatch) completes it
            db_cols[:] = ["", "", "", ""]
            sql_fail[0] = True
            try:
                do_hotpatch("1.6", _Job(), True)
                raise AssertionError("a failed UPDATE must fail the job")
            except AgentError as e:
                assert "SQL failed" in e.message, e.message
            hst = version_state("1.6")["hotpatch"]
            assert hst["enabled"] is True and hst["pending"] is True and os.path.isfile(pc_path), hst
            sql_fail[0] = False
            assert apply_hotpatch("1.6", _Job(), with_sql=True) == 1 and db_cols == want_cols, db_cols
            assert version_state("1.6")["hotpatch"]["pending"] is False
            # (5) disable with the stack up: files + columns reverted, one restart; a second disable
            #     changes nothing and restarts nothing -- with the stack up or down (owes nothing)
            n_restarts = len(restarts)
            res = do_hotpatch("1.6", _Job(), False)
            assert res == {"enabled": False, "pending": False, "purged": False} and db_cols == ["", "", "", ""], (res, db_cols)
            assert len(restarts) == n_restarts + 1 and not os.path.isfile(pc_path)
            j5 = _Job()
            res = do_hotpatch("1.6", j5, False)
            assert res["pending"] is False and len(restarts) == n_restarts + 1, "a second disable must not restart"
            assert any("no restart" in l for l in j5.lines), j5.lines
            running_projects = lambda: (None, "docker unavailable (selftest)")  # noqa: E731
            res = do_hotpatch("1.6", _Job(), False)
            assert res["pending"] is False, "an already-reverted version owes no database half"
            # the vendor/disabled row is four EMPTY columns: read back as such, not as "no row"
            assert _region_urls("1.6") == ["", "", "", ""]
            assert hotpatch_sql("1.6", _Job(), None, sman) is False, "an already-empty row is not a change"
            assert hotpatch_sql("1.6", _Job(), info["url"], sman) is True and db_cols == want_cols
            db_cols[:] = ["", "", "", ""]
            # a quote or a backslash in the region name / URL cannot break out of the SQL literal
            assert _sql_q("a\\'b") == "a\\\\''b" and _sql_q("plain") == "plain"
            (mysql_exec, run_sql, _compose_stream, verify_stack, mysql_reachable,
             urllib.request.urlopen) = saved_db
            os.remove(os.path.join(stack, ".bootstrap.lock"))
            os.remove(os.path.join(stack, "docker-compose.yml"))
            version_state_set("1.6", hotpatch={})
            del fetched_urls[:]
            assert do_hotpatch("1.6", _Job(), True)["pending"] is True, "back to the stack-down shape"

            # a damaged cached file is the only one re-fetched; a corrupt render is rewritten
            with open(hotpatch_local(droot + "blocks/00/2.blk"), "ab") as f:
                f.write(b"junk")
            assert not hotpatch_info("1.6")["complete"]
            write_text(pc_path, "{ not json")
            del fetched_urls[:]
            res = do_hotpatch("1.6", _Job(), True)
            assert res["fetched"] == 1 and len(fetched_urls) == 1 and fetched_urls[0].endswith("/blocks/00/2.blk"), (res, fetched_urls)
            assert json.loads(read_text(pc_path))["version"] == 11
            assert read_text(vt_path + ".relic-orig") == "{}", "the backup must not be overwritten by a re-enable"
            # an upstream outage: the job fails naming the file, the good files stay, state untouched
            broken.add(rroot + "AssetBundles/blocks/00/1.blk")
            os.remove(hotpatch_local(rroot + "AssetBundles/blocks/00/1.blk"))
            before = version_state("1.6")["hotpatch"]
            try:
                do_hotpatch("1.6", _Job(), True)
                raise AssertionError("a failed download must fail the job")
            except AgentError as e:
                assert e.status == 502 and "1 of 6" in e.message and "blocks/00/1.blk" in e.message, e.message
            assert os.path.isfile(hotpatch_local(droot + "blocks/00/2.blk")), "good files must stay"
            assert version_state("1.6")["hotpatch"] == before
            broken.clear()
            # the mirror URL must be reachable by players: loopback host / tunnel listener refuse
            ADVERTISED_IP = ""
            write_env_value("1.6", "OUTER_IP", "127.0.0.1")
            for setup in (("", "0.0.0.0"), ("198.51.100.171", "127.0.0.1")):
                ADVERTISED_IP, LISTEN_HOST = setup
                try:
                    hotpatch_public_url("1.6", sman, strict=True)
                    raise AssertionError("must refuse: %r" % (setup,))
                except AgentError as e:
                    assert e.status == 409 and "Cannot enable" in e.message, e.message
                assert hotpatch_public_url("1.6", sman, strict=False)[0] is None
            HOTPATCH_URL = "https://files.example.com/gio"
            assert hotpatch_public_url("1.6", sman) == ("https://files.example.com/gio", None), "an explicit URL trusts the admin"
            HOTPATCH_URL = "https://" + "x" * 120 + ".example.com"
            try:
                hotpatch_public_url("1.6", sman)
                raise AssertionError("a URL over varchar(128) must be refused")
            except AgentError as e:
                assert "128" in e.message
            # the URL lands in a SQL literal and in the protobuf the client parses: allowlisted shape
            for badu in ("http://x\\'", "https://files.example.com/gio?x=1", "http://a b/x", "ftp://x.example.com",
                         "http://", "http://x.example.com/'; DROP", "http://x.example.com/a#b", "files.example.com"):
                HOTPATCH_URL = badu
                try:
                    hotpatch_public_url("1.6", sman, strict=True)
                    raise AssertionError("must refuse the URL %r" % badu)
                except AgentError as e:
                    assert e.status == 409 and "not a plain" in e.message, (badu, e.message)
                assert hotpatch_public_url("1.6", sman, strict=False)[0] is None
            HOTPATCH_URL = "http://[2001:db8::1]:8080/m"
            assert hotpatch_public_url("1.6", sman) == ("http://[2001:db8::1]:8080/m", None)
            HOTPATCH_URL = ""
            ADVERTISED_IP, LISTEN_HOST = "198.51.100.171", "0.0.0.0"
            write_env_value("1.6", "OUTER_IP", "192.0.2.10")
            # an enabled hotpatch whose URL became unusable is NOT advertised at the next start
            ADVERTISED_IP = ""
            write_env_value("1.6", "OUTER_IP", "127.0.0.1")
            j2 = _Job()
            assert apply_hotpatch("1.6", j2, with_sql=False) == 2 and not os.path.isfile(pc_path)
            assert read_text(vt_path) == "{}" and any("not usable" in l for l in j2.lines), j2.lines
            ADVERTISED_IP = "198.51.100.171"
            write_env_value("1.6", "OUTER_IP", "192.0.2.10")
            assert apply_hotpatch("1.6", _Job(), with_sql=False) == 2 and os.path.isfile(pc_path)
            # disable: files reverted, DB half pending (stack down), purge drops the mirrored branch
            res = do_hotpatch("1.6", _Job(), False, purge=True)
            assert res == {"enabled": False, "pending": True, "purged": True}, res
            assert not os.path.isfile(pc_path) and read_text(vt_path) == "{}"
            assert not os.path.isdir(os.path.join(HOTPATCH_DIR, "client_game_res", "1.6_live"))
            assert not os.path.isdir(os.path.join(HOTPATCH_DIR, "client_design_data", "1.6_live"))
            hst = version_state("1.6")["hotpatch"]
            assert hst["enabled"] is False and hst["pending"] is True and hst.get("disabledAt")
            assert hotpatch_brief("1.6")["complete"] is False
            assert apply_hotpatch("1.6", _Job(), with_sql=False) == 0, "a revert already on disk changes nothing"
            # a version.txt the agent never touched is left alone by a disable
            write_text(vt_path, '{"server": 1}')
            os.remove(vt_path + ".relic-orig")
            version_state_set("1.6", hotpatch={})
            assert remove_hotpatch_configs("1.6", _Job()) == 0 and read_text(vt_path) == '{"server": 1}'
            # path confinement for the token-less route
            os.makedirs(os.path.join(HOTPATCH_DIR, "client_game_res", "1.6_live"), exist_ok=True)
            for badp in ("../state.json", "client_game_res/../../x", "/etc/passwd", "a//b", "C:/x", "c:x",
                         "a\\b", "", ".", "..", "a/./b", "%2e%2e/x", "a%00b", "%2Fetc/passwd", "a/..b/c",
                         "client_game_res/1.6_live/%0d%0ax",
                         # a miss creates directories: depth, length and segment length are bounded
                         "client_game_res/1.6_live/" + "a/" * 20 + "b",
                         "client_game_res/1.6_live/" + "a" * 129,
                         "client_game_res/1.6_live/" + "ab/" * 100 + "c"):
                assert safe_hotpatch_path(badp) is None, badp
            # defence in depth: only the manifests' own subtrees are served (derived from the loaded manifests), and
            # never a dot name -- even when the mirror folder holds the files (a hand-set GIO_HOTPATCH_DIR over the
            # agent's or a stack's folder)
            assert hotpatch_served_roots() == ("client_design_data/1.6_live/", "client_game_res/1.6_live/"), \
                hotpatch_served_roots()
            exposed = ("config", "state.json", "agent/config", "1.6_live/creds.txt", ".env", "backups/x.json",
                       "client_game_res/.env", "client_game_res/1.6_live/.relic-provisioned",
                       "client_game_res/1.6_live/o/.git/config", "client_game_res/2.8_live/o/x.blk",
                       "other/1.6_live/o/x", "client_game_res/1.6_livex/o/y")
            made_dirs = []
            for p_ in exposed:
                made_dirs.append(_makedirs_tracked(os.path.dirname(hotpatch_local(p_))))
                write_text(hotpatch_local(p_), "GIO_AGENT_TOKEN=secret\n")
                assert safe_hotpatch_path(p_) is None, p_
            hp_replies = []
            hh_ = Handler.__new__(Handler)
            hh_.path, hh_.headers, hh_.client_address = "/hotpatch/config", {}, ("192.0.2.77", 5000)
            hh_._send = lambda status_, obj, retry_after=None: hp_replies.append((status_, obj))
            hh_.do_GET()
            assert hp_replies == [(404, {"error": "not found"})], hp_replies
            for p_ in exposed:
                _remove_quiet(hotpatch_local(p_))
            for created_ in reversed(made_dirs):
                _prune_dirs(created_)
            longest_real = "client_game_res/1.6_live/output_3557509_5979a935f8/client/StandaloneWindows64/AudioAssets/English(US)/VO_1.6_11.pck"
            assert safe_hotpatch_path(longest_real) is not None, "every real CDN path must pass"
            goodp = safe_hotpatch_path("client_game_res/1.6_live/o/client/StandaloneWindows64/AudioAssets/English%28US%29/VO_1.6_11.pck")
            assert goodp is not None and goodp[0].endswith("English(US)/VO_1.6_11.pck"), goodp
            assert goodp[1] == os.path.join(HOTPATCH_DIR, "client_game_res", "1.6_live", "o", "client",
                                            "StandaloneWindows64", "AudioAssets", "English(US)", "VO_1.6_11.pck")
            # ARCHIVE.ORG BUNDLES (agent 3.4): the same files as one .7z, tried BEFORE the per-file CDN
            # path. A fake bundle is a JSON "archive" the fake fetch serves and the fake extractor
            # unpacks into the staging folder: every listed file, one file NOT in the manifest and one
            # whose bytes are corrupted -- the import must take only what the manifest verifies.
            g_ = globals()
            saved_x = (g_["find_extractor"], g_["extract_archive"], g_["_mirror_free_bytes"])
            g_["find_extractor"] = lambda: ("/fake/7zz", "7z")
            burl = "https://archive.org/download/test/1.6_hotpatch.7z"
            brel, jrel, krel, crel = ("download/test/1.6_%s.7z" % n for n in ("hotpatch", "voice_Japanese", "voice_Korean", "voice_Chinese"))
            corrupt = droot + "blocks/00/2.blk"
            extra = "client_game_res/1.6_live/%s/client/StandaloneWindows64/extra.bin" % sres
            bblobs[brel] = json.dumps({"files": sorted(blobs), "corrupt": [corrupt], "unlisted": [extra]}).encode()
            bblobs[jrel] = json.dumps({"files": [jp1, jp2]}).encode()                      # CDN-relative layout
            bblobs[krel] = json.dumps({"flat": ["Korean/VO_1.pck"]}).encode()               # "<Lang>/<file>.pck" layout
            bblobs[crel] = json.dumps({"flat": ["Chinese/VO_%d.pck" % i for i in range(1, 6)],
                                       "flatbad": ["Chinese/VO_6.pck"]}).encode()          # one pack with a wrong md5

            def bspec(rel, **extra_keys):
                d_ = {"url": "https://archive.org/" + rel, "size": len(bblobs[rel]), "md5": hashlib.md5(bblobs[rel]).hexdigest()}
                d_.update(extra_keys)
                return d_

            sman_b = dict(sman, bundles={"hotpatch": bspec(brel, files=6),
                                         "voice": {"Japanese": bspec(jrel), "Korean": bspec(krel), "Chinese": bspec(crel),
                                                   "Klingon": {"url": "nope"}}})
            with open(os.path.join(PAYLOAD_DIR, "1.6", "hotpatch.json"), "w") as f:
                json.dump(sman_b, f)
            stop_in_extract = []

            def fake_extract(archive, dest, job, should_stop=None, timeout=None):
                with open(archive, "rb") as f:
                    plan = json.loads(f.read().decode())
                os.makedirs(dest, exist_ok=True)

                def put(rel, body):
                    p = os.path.join(dest, *rel.split("/"))
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "wb") as f:
                        f.write(body)

                for rel in plan.get("files", []):
                    body = blobs.get(rel, vblobs.get(rel))
                    put(rel, bytes(len(body)) if rel in plan.get("corrupt", []) else body)
                for rel in plan.get("flat", []):
                    put(rel, vblobs[vroot + rel])
                for rel in plan.get("flatbad", []):
                    put(rel, b"x" * len(vblobs[vroot + rel]))
                for rel in plan.get("unlisted", []):
                    put(rel, b"not in the manifest")
                if stop_in_extract:
                    _voice_cancel.set()
                if should_stop is not None and should_stop():
                    raise ExtractStopped("stopped on request")
                return 0.1

            g_["extract_archive"] = fake_extract
            assert hotpatch_bundle_spec(sman_b)["name"] == "1.6_live_hotpatch.7z"
            assert hotpatch_bundle_spec(sman_b, "English(US)") is None and hotpatch_bundle_spec(sman_b, "Klingon") is None
            assert hotpatch_bundle_spec(sman_b, "Korean")["name"] == "1.6_live_voice_Korean.7z"
            assert hotpatch_bundle_spec(sman, "Japanese") is None, "a bundle-less manifest names no bundle"
            # the plan reads the POLICY, so pin it here: a box whose GIO_HOTPATCH_SOURCE seeds "cdn"
            # would otherwise refuse every bundle before the manifest is ever looked at.
            set_policy({"hotpatchSource": "archive"})
            assert hotpatch_bundle_plan(None, 10, 10, None) == (False, "this manifest names no bundle")
            assert hotpatch_bundle_plan(hotpatch_bundle_spec(sman_b), 4, 10, None)[1].startswith("only ")
            assert hotpatch_bundle_plan(hotpatch_bundle_spec(sman_b), 5, 10, None) == (True, None)
            # ... and the admin's own choice overrules a perfectly usable bundle: "cdn" means file by
            # file, and the reason is the one the job logs.
            set_policy({"hotpatchSource": "cdn"})
            assert hotpatch_source() == "cdn"
            usable_cdn, why_cdn = hotpatch_bundle_plan(hotpatch_bundle_spec(sman_b), 5, 10, None)
            assert usable_cdn is False and "CDN" in why_cdn, why_cdn
            set_policy({"hotpatchSource": "archive"})
            assert hotpatch_source() == "archive"
            assert hotpatch_bundle_plan(hotpatch_bundle_spec(sman_b), 5, 10, None) == (True, None)

            def empty_mirror():
                for sub in ("client_game_res", "client_design_data"):
                    _rmtree_quiet(os.path.join(HOTPATCH_DIR, sub))
                version_state_set("1.6", hotpatch={})
                del fetched_urls[:]

            # (a) an empty mirror: the bundle first, only the corrupted file from the CDN; the unlisted
            #     file never enters the mirror, the staging folder and the archive are gone
            empty_mirror()
            ja = _Job()
            res = do_hotpatch("1.6", ja, True)
            assert res["source"] == {"bundle": 5, "cdn": 1} and res["fetched"] == 6 and res["pending"], res
            assert fetched_urls == [burl, "https://hk.invalid/" + corrupt], fetched_urls
            assert version_state("1.6")["hotpatch"]["source"] == "bundle+cdn"
            assert hotpatch_info("1.6")["source"] == "bundle+cdn" and hotpatch_brief("1.6")["source"] == "bundle+cdn"
            assert not os.path.exists(hotpatch_local(extra)), "an unlisted file must never enter the mirror"
            assert not [n for n in os.listdir(HOTPATCH_BUNDLE_DIR) if n.startswith("stage-")], os.listdir(HOTPATCH_BUNDLE_DIR)
            assert not os.path.exists(os.path.join(HOTPATCH_BUNDLE_DIR, "1.6_live_hotpatch.7z")), "the archive goes after the import"
            assert any("Unpacked 1.6_live_hotpatch.7z: 5 files imported and verified, 1 ignored (not in the manifest), "
                       "1 mismatched" in l for l in ja.lines), ja.lines
            for path_, body in blobs.items():
                with open(hotpatch_local(path_), "rb") as f:
                    assert f.read() == body, path_
            binfo = hotpatch_info("1.6")["bundles"]
            assert binfo["enabled"] is True and binfo["extractor"] == "7zz" and binfo["dir"] == HOTPATCH_BUNDLE_DIR, binfo
            assert binfo["hotpatch"] == {"url": burl, "size": len(bblobs[brel]), "present": False, "partial": 0}, binfo
            assert sorted(binfo["voice"]) == ["Chinese", "Japanese", "Korean"], binfo["voice"]
            assert "bundles" not in json.dumps(build_public_status(g_["status"](), get_policy())), "never public"
            # (b) one file missing: below half the bytes -> file by file, no bundle
            os.remove(hotpatch_local(rroot + "AssetBundles/blocks/00/1.blk"))
            del fetched_urls[:]
            jb = _Job()
            res = do_hotpatch("1.6", jb, True)
            assert res["source"] == {"bundle": 0, "cdn": 1} and fetched_urls == ["https://hk.invalid/" + rroot + "AssetBundles/blocks/00/1.blk"], (res, fetched_urls)
            assert any("Bundle skipped: only" in l and "file by file is cheaper" in l for l in jb.lines), jb.lines
            assert version_state("1.6")["hotpatch"]["source"] == "cdn"
            res = do_hotpatch("1.6", _Job(), True)
            assert res["source"] == {"bundle": 0, "cdn": 0} and version_state("1.6")["hotpatch"]["source"] == "cached", res
            # (c) the bundle download fails (network): two tries, then every file from the CDN
            empty_mirror()
            broken.add(brel)
            jc = _Job()
            res = do_hotpatch("1.6", jc, True)
            assert res["source"] == {"bundle": 0, "cdn": 6} and fetched_urls[:2] == [burl, burl] and len(fetched_urls) == 8, fetched_urls
            assert all(u.startswith("https://hk.invalid/") for u in fetched_urls[2:])
            assert any("The bundle could not be used (" in l and "simulated outage" in l for l in jc.lines), jc.lines
            assert version_state("1.6")["hotpatch"]["source"] == "cdn"
            broken.clear()
            # (d) no extractor on the box: skipped with the reason, the panel says so
            empty_mirror()
            g_["find_extractor"] = lambda: None
            jd_ = _Job()
            res = do_hotpatch("1.6", jd_, True)
            assert res["source"] == {"bundle": 0, "cdn": 6} and burl not in fetched_urls, (res, fetched_urls)
            assert any("Bundle skipped: no 7-Zip / bsdtar tool" in l for l in jd_.lines), jd_.lines
            assert hotpatch_info("1.6")["bundles"]["extractor"] is None
            g_["find_extractor"] = lambda: ("/fake/7zz", "7z")
            # (e) bundles switched off
            empty_mirror()
            HOTPATCH_BUNDLES = False
            je = _Job()
            assert do_hotpatch("1.6", je, True)["source"] == {"bundle": 0, "cdn": 6} and burl not in fetched_urls
            assert any("Bundle skipped: bundles are switched off" in l for l in je.lines), je.lines
            assert hotpatch_info("1.6")["bundles"]["enabled"] is False
            HOTPATCH_BUNDLES = True
            # (f) enough disk for the files, not for the bundle on top of them
            empty_mirror()
            total_b_ = sum(f["size"] for f in sman["files"])
            g_["_mirror_free_bytes"] = lambda: total_b_ + HOTPATCH_DISK_RESERVE + 1
            jf = _Job()
            assert do_hotpatch("1.6", jf, True)["source"] == {"bundle": 0, "cdn": 6} and burl not in fetched_urls
            assert any("Bundle skipped: not enough free disk for the bundle" in l for l in jf.lines), jf.lines
            g_["_mirror_free_bytes"] = saved_x[2]
            # (g) the LOCAL disk fails while the bundle downloads: 507 stops the job at once
            empty_mirror()
            disk_full.add(brel)
            try:
                do_hotpatch("1.6", _Job(), True)
                raise AssertionError("a local write failure must stop the job")
            except AgentError as e:
                assert e.status == 507 and "stopped" in e.message and fetched_urls == [burl], (e.message, fetched_urls)
            disk_full.clear()
            empty_mirror()
            assert do_hotpatch("1.6", _Job(), True)["source"] == {"bundle": 5, "cdn": 1}
            # (h) voice packs: a CDN-relative Japanese bundle imports both packs, a flat Korean one via
            #     the alt map; of the Chinese bundle the pack with a wrong md5 is discarded -> CDN
            do_hotpatch_voice("1.6", _Job(), [], purge=True)
            del fetched_urls[:]
            jh = _Job()
            res = do_hotpatch_voice("1.6", jh, ["Japanese", "Korean", "Chinese"])
            assert res["source"] == {"bundle": 8, "cdn": 1} and res["bundles"] == ["Japanese", "Korean", "Chinese"], res
            assert res["fetched"] == 9 and res["bytes"] == 1600 + 500 + 27, res
            assert fetched_urls == ["https://archive.org/" + jrel, "https://archive.org/" + krel, "https://archive.org/" + crel,
                                    "https://hk.invalid/" + vroot + "Chinese/VO_6.pck"], fetched_urls
            for p_ in (jp1, jp2, ko1) + tuple(vroot + "Chinese/VO_%d.pck" % i for i in range(1, 7)):
                with open(hotpatch_local(p_), "rb") as f:
                    assert f.read() == vblobs[p_], p_
            assert jh.yielding is True
            vsrc = version_state("1.6")["hotpatch"]["voiceSource"]
            assert vsrc == {"Japanese": "bundle", "Korean": "bundle", "Chinese": "bundle+cdn"}, vsrc
            vrows = {r["name"]: r for r in hotpatch_info("1.6")["voice"]["languages"]}
            assert vrows["Japanese"]["source"] == "bundle" and vrows["Chinese"]["source"] == "bundle+cdn", vrows
            assert not [n for n in os.listdir(HOTPATCH_BUNDLE_DIR) if n.startswith("stage-") or n.endswith(".7z")]
            assert not os.path.exists(hotpatch_local(vroot + "English(US)/VO_9.pck"))
            # (i) Stop while the bundle downloads: 409, the .part stays and the next run resumes it
            do_hotpatch_voice("1.6", _Job(), [], purge=True)
            stop_mid.add(jrel)
            del fetched_urls[:]
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese"])
                raise AssertionError("a cancelled bundle download must end in 409")
            except AgentError as e:
                assert e.status == 409 and e.message.startswith("Stopped on request"), e.message
            jpart = os.path.join(HOTPATCH_BUNDLE_DIR, "1.6_live_voice_Japanese.7z.part")
            assert os.path.isfile(jpart) and 0 < os.path.getsize(jpart) < len(bblobs[jrel])
            assert hotpatch_info("1.6")["bundles"]["voice"]["Japanese"]["partial"] == os.path.getsize(jpart)
            assert not os.path.exists(hotpatch_local(jp1))
            stop_mid.clear()
            del resumed_at[:]
            ji = _Job()
            res = do_hotpatch_voice("1.6", ji, ["Japanese"])
            assert res["source"] == {"bundle": 2, "cdn": 0} and fetched_urls == ["https://archive.org/" + jrel] * 2, fetched_urls
            assert resumed_at == [len(bblobs[jrel]) // 2] and any("resuming at" in l for l in ji.lines), (resumed_at, ji.lines)
            assert not os.path.exists(jpart) and os.path.isfile(hotpatch_local(jp2))
            # (j) Stop during the extraction: 409, the staging folder is gone, the verified archive is
            #     kept -- the next run unpacks it without a download
            do_hotpatch_voice("1.6", _Job(), [], purge=True)
            stop_in_extract.append(1)
            del fetched_urls[:]
            try:
                do_hotpatch_voice("1.6", _Job(), ["Japanese"])
                raise AssertionError("a stop during the extraction must end in 409")
            except AgentError as e:
                assert e.status == 409 and e.message.startswith("Stopped on request"), e.message
            assert not [n for n in os.listdir(HOTPATCH_BUNDLE_DIR) if n.startswith("stage-")]
            assert os.path.isfile(os.path.join(HOTPATCH_BUNDLE_DIR, "1.6_live_voice_Japanese.7z"))
            assert hotpatch_info("1.6")["bundles"]["voice"]["Japanese"]["present"] is True
            stop_in_extract.clear()
            del fetched_urls[:]
            jj = _Job()
            assert do_hotpatch_voice("1.6", jj, ["Japanese"])["source"] == {"bundle": 2, "cdn": 0} and fetched_urls == []
            assert any("already on disk and verified" in l for l in jj.lines), jj.lines
            # Disable + purge takes the bundle leftovers of the branch with it
            for n in ("1.6_live_voice_Korean.7z.part", "1.6_live_hotpatch.7z"):
                with open(os.path.join(HOTPATCH_BUNDLE_DIR, n), "wb") as f:
                    f.write(b"leftover")
            os.makedirs(os.path.join(HOTPATCH_BUNDLE_DIR, "stage-1.6_live_hotpatch.7z-1", "x"))
            do_hotpatch("1.6", _Job(), False, purge=True)
            assert os.listdir(HOTPATCH_BUNDLE_DIR) == [], os.listdir(HOTPATCH_BUNDLE_DIR)
            # the manifest-listed voice bundles of the shipped manifests parse, and the shipped hotpatch
            # bundle names every manifest file
            for v_, man_ in sorted(shipped.items()):
                sp = hotpatch_bundle_spec(man_)
                assert sp and sp["url"].startswith("https://archive.org/download/") and sp["size"] > 1e8, (v_, sp)
                assert man_["bundles"]["hotpatch"]["files"] == len(man_["files"]), v_
                for lang in man_.get("voice") or {}:
                    vs_ = hotpatch_bundle_spec(man_, lang)
                    assert vs_ and vs_["size"] > 1e9 and "/" not in vs_["name"], (v_, lang, vs_)
                    assert urllib.parse.unquote(vs_["url"]).endswith("_voice_%s.7z" % lang), (v_, lang, vs_["url"])
            g_["find_extractor"], g_["extract_archive"] = saved_x[0], saved_x[1]
            with open(os.path.join(PAYLOAD_DIR, "1.6", "hotpatch.json"), "w") as f:
                json.dump(sman, f)
            version_state_set("1.6", hotpatch={})
            # (k) the REAL tool on this box, when there is one that reads more than .7z (7zr does not):
            #     a tiny .tar (bsdtar) or .zip (7z family, Bandizip) through the real extract_archive
            #     proves the command line and the poll loop; the kill path is exercised with
            #     should_stop already true (a tiny archive may finish before the first poll, so
            #     either outcome is accepted there).
            found = saved_x[0]()
            if found and not (found[1] == "7z" and os.path.basename(found[0]).lower().startswith("7zr")):
                import tarfile
                import zipfile
                smoke = os.path.join(td, "smoke")
                os.makedirs(smoke)
                if found[1] == "bsdtar":
                    arc = os.path.join(smoke, "t.tar")
                    with tarfile.open(arc, "w") as tf:
                        inner = os.path.join(smoke, "b.txt")
                        with open(inner, "wb") as f:
                            f.write(b"hello")
                        tf.add(inner, arcname="a/b.txt")
                else:
                    arc = os.path.join(smoke, "t.zip")
                    with zipfile.ZipFile(arc, "w") as zf:
                        zf.writestr("a/b.txt", b"hello")
                jk = _Job()
                secs = saved_x[1](arc, os.path.join(smoke, "out"), jk)
                with open(os.path.join(smoke, "out", "a", "b.txt"), "rb") as f:
                    assert f.read() == b"hello"
                assert secs >= 0 and any(l.startswith("$ ") for l in jk.lines), jk.lines
                try:
                    saved_x[1](arc, os.path.join(smoke, "out2"), _Job(), should_stop=lambda: True)
                except ExtractStopped:
                    pass
                try:
                    saved_x[1](os.path.join(smoke, "missing.7z"), os.path.join(smoke, "out3"), _Job())
                    raise AssertionError("a missing archive must fail the extraction")
                except ExtractError as e:
                    assert "exited with code" in str(e), e
                ok("real extractor smoke OK (%s, kind %s: extract, stop, failure)" % (os.path.basename(found[0]), found[1]))
            else:
                print("selftest: no 7z/bsdtar tool (or only 7zr) on this box -- the real extraction smoke was skipped")
            ok("hotpatch bundles OK (bundle first + CDN for the rest, 50% rule, no tool / off / disk / 404 / 507 "
               "fallbacks, voice bundles in both layouts + wrong-md5 pack, stop mid-download resumes, stop mid-extract "
               "keeps the archive, purge drops the leftovers, shipped manifests' bundles)")

            # the real manifest against the byte-exact reference mirror, when it is on this machine
            if "1.6" in shipped and os.path.isdir(ref_dir):
                with open(os.path.join(PAYLOAD_DIR, "1.6", "hotpatch.json"), "w") as f:
                    json.dump(shipped["1.6"], f)
                version_state_set("1.6", hotpatch={})
                res = do_hotpatch("1.6", _Job(), True)
                assert res["fetched"] == 37 and res["pending"], res
                assert hotpatch_info("1.6")["files"]["cachedBytes"] == 126259414 + 884958  # payload + index
                assert json.loads(read_text(pc_path))["version"] == 3557509
                ok("hotpatch job OK against hotpatch_1.6_ref (37 files copied, every md5 verified)")
            else:
                print("selftest: hotpatch_1.6_ref not present -- the real-manifest copy check was skipped")
            # the shipped manifests' voice totals = the non-patched "<Lang>/<x>.pck" lines of the
            # official release index (when the reference mirrors are on this machine)
            for v_, man_ in sorted(shipped.items()):
                idx_path = os.path.join(os.path.dirname(ref_dir), "hotpatch_%s_ref" % v_,
                                        *(hotpatch_res_root(man_) + "release_res_versions_external").split("/"))
                assert hotpatch_base_audio_root(man_), "the %s manifest must name its base output" % v_
                if not os.path.isfile(idx_path):
                    continue
                real = _parse_voice_index(idx_path)
                assert sorted(real) == ["Chinese", "English(US)", "Japanese", "Korean"], sorted(real)
                assert man_.get("voice") == {k: {"files": len(e), "bytes": sum(x["size"] for x in e)}
                                             for k, e in real.items()}, (v_, man_.get("voice"))
                ok("hotpatch %s voice totals match the official release index (%s)"
                   % (v_, ", ".join("%s %d" % (k, len(e)) for k, e in sorted(real.items()))))
        finally:
            (HOTPATCH_DIR, HOTPATCH_URL, PAYLOAD_DIR, STATE_PATH, ADVERTISED_IP, LISTEN_HOST,
             LISTEN_PORT, VERSIONS["1.6"]["dir"], running_projects, _fetch_to_file,
             HOTPATCH_BUNDLE_DIR, HOTPATCH_BUNDLES) = saved_hp
            (mysql_exec, run_sql, _compose_stream, verify_stack, mysql_reachable,
             urllib.request.urlopen) = saved_db
            ondemand_bytes(reset=True)
    ok("hotpatch enable/disable job OK (fake stack, no network, no docker: mirror + configs + "
       "pending state + failure + refusals + purge)")
    ok("hotpatch job with the stack up OK (simulated MySQL/compose: state persisted before the SQL, "
       "restart only when something changed or is owed, failed restart/SQL stay pending, "
       "double disable owes nothing, empty row read as empty)")
    ok("hotpatch path confinement + miss policy + on-demand budget OK")
    ok("hotpatch voice packs OK (index allowlist, admin selection, job + purge, on-demand only while "
       "enabled, tampered index, disk guard, refused-request counter)")

    # _fetch_to_file touches the disk only after the upstream answered: a 404 / dead CDN creates
    # no directory, a transfer that dies half-way (or over the cap) takes its directories away again.
    saved_open = urllib.request.urlopen
    with tempfile.TemporaryDirectory() as td:
        try:
            deep = os.path.join(td, "a", "b", "c", "file.bin")

            def dead(req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, 404, "NoSuchKey", {}, None)

            class Resp:
                def __init__(self, chunks, status=200, headers=None):
                    self.chunks = list(chunks)
                    self.status = status
                    self.headers = headers or {}

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self, n):
                    if not self.chunks:
                        return b""
                    c = self.chunks.pop(0)
                    if isinstance(c, Exception):
                        raise c
                    return c

            urllib.request.urlopen = dead
            try:
                _fetch_to_file("https://hk.invalid/x", deep)
                raise AssertionError("a 404 must raise")
            except urllib.error.HTTPError:
                pass
            assert not os.path.exists(os.path.join(td, "a")), "a 404 must not create directories"
            urllib.request.urlopen = lambda req, timeout=0: Resp([b"ab", socket.timeout("stalled")])
            try:
                _fetch_to_file("https://hk.invalid/x", deep)
                raise AssertionError("a stalled transfer must raise")
            except FetchCut as e:
                assert isinstance(e, OSError), "callers retry it like any network error"
            assert not os.path.exists(os.path.join(td, "a")), "a failed transfer must remove what it created"
            urllib.request.urlopen = lambda req, timeout=0: Resp([b"ab", b"cd"])
            try:
                _fetch_to_file("https://hk.invalid/x", deep, cap=3)
                raise AssertionError("over the cap must raise")
            except ValueError as e:
                assert "cap" in str(e)
            assert not os.path.exists(os.path.join(td, "a"))
            assert _fetch_to_file("https://hk.invalid/x", deep, expect_md5=hashlib.md5(b"abcd").hexdigest(),
                                  expect_size=4) == 4
            with open(deep, "rb") as f:
                assert f.read() == b"abcd"
            assert not os.path.exists(deep + ".part")
            # a DECLARED length that can never pass is refused before a byte is streamed or a
            # directory made (an unlisted 300 MB pack must not be pulled four times up to the cap)
            big = Resp([b"x" * 10], headers={"Content-Length": "10"})
            urllib.request.urlopen = lambda req, timeout=0: big
            for kw in ({"cap": 9}, {"expect_size": 11, "expect_md5": "0" * 32}):
                try:
                    _fetch_to_file("https://hk.invalid/x", os.path.join(td, "r", "f.bin"), **kw)
                    raise AssertionError("must be refused: %r" % kw)
                except FetchRefused:
                    pass
                assert len(big.chunks) == 1 and not os.path.exists(os.path.join(td, "r")), kw
            # resume (voice packs): what the NETWORK cut keeps its .part and continues by Range; the
            # md5 covers the whole file; an upstream that ignores the Range restarts cleanly; a bad
            # splice is thrown away like any other corruption
            want = {"expect_md5": hashlib.md5(b"abcdef").hexdigest(), "expect_size": 6, "resume": True}
            vo = os.path.join(td, "v", "VO.pck")
            asked = []

            def ranged(*bodies):
                todo = list(bodies)

                def opener(req, timeout=0):
                    asked.append(req.get_header("Range"))
                    return todo.pop(0)
                return opener

            urllib.request.urlopen = ranged(Resp([b"abc", socket.timeout("stalled")]), Resp([b"def"], status=206))
            try:
                _fetch_to_file("https://hk.invalid/v", vo, **want)
                raise AssertionError("the cut transfer must raise")
            except FetchCut:
                pass
            with open(vo + ".part", "rb") as f:
                assert f.read() == b"abc", "a transfer the network cut keeps its .part"

            def cdn_down(req, timeout=0):
                raise urllib.error.URLError("unreachable")

            resume_opener = urllib.request.urlopen
            urllib.request.urlopen = cdn_down  # a dead CDN costs nothing (no hashing) and keeps the .part
            try:
                _fetch_to_file("https://hk.invalid/v", vo, **want)
                raise AssertionError("must raise")
            except urllib.error.URLError:
                pass
            assert os.path.getsize(vo + ".part") == 3
            urllib.request.urlopen = resume_opener
            assert _fetch_to_file("https://hk.invalid/v", vo, **want) == 6 and asked == [None, "bytes=3-"], asked
            with open(vo, "rb") as f:
                assert f.read() == b"abcdef"
            assert not os.path.exists(vo + ".part")
            os.remove(vo)
            with open(vo + ".part", "wb") as f:
                f.write(b"abc")
            urllib.request.urlopen = ranged(Resp([b"abcdef"], status=200))  # the Range was ignored
            assert _fetch_to_file("https://hk.invalid/v", vo, **want) == 6
            with open(vo, "rb") as f:
                assert f.read() == b"abcdef"
            os.remove(vo)
            with open(vo + ".part", "wb") as f:
                f.write(b"XYZ")
            urllib.request.urlopen = ranged(Resp([b"def"], status=206))
            try:
                _fetch_to_file("https://hk.invalid/v", vo, **want)
                raise AssertionError("a wrong splice must fail the md5")
            except ValueError as e:
                assert "md5" in str(e)
            assert not os.path.exists(vo + ".part") and not os.path.exists(vo)
            # without resume nothing is kept, as before
            urllib.request.urlopen = lambda req, timeout=0: Resp([b"abc", socket.timeout("stalled")])
            try:
                _fetch_to_file("https://hk.invalid/v", vo, expect_md5=want["expect_md5"], expect_size=6)
                raise AssertionError("must raise")
            except FetchCut:
                pass
            assert not os.path.exists(vo + ".part")
            # a COMPLETE .part (only the rename never happened) is verified and adopted, no request
            with open(vo + ".part", "wb") as f:
                f.write(b"abcdef")
            urllib.request.urlopen = cdn_down
            assert _fetch_to_file("https://hk.invalid/v", vo, **want) == 6 and not os.path.exists(vo + ".part")
            os.remove(vo)
            # should_stop (a cancelled voice job): FetchStopped, the .part stays for the next run
            calls = []
            urllib.request.urlopen = lambda req, timeout=0: Resp([b"abc", b"def"])
            try:
                _fetch_to_file("https://hk.invalid/v", vo, should_stop=lambda: bool(calls) or calls.append(1), **want)
                raise AssertionError("must stop")
            except FetchStopped:
                pass
            with open(vo + ".part", "rb") as f:
                assert f.read() == b"abc"
            os.remove(vo + ".part")
            # a rename refused because dst is open elsewhere: accepted only when dst IS the file
            # (size AND md5); a same-size corrupt dst is "in use", and the complete .part waits
            real_replace = globals()["replace_file"]

            def refused(src, dst_, attempts=5, delay=0.2):
                raise PermissionError(13, "in use (selftest)")

            globals()["replace_file"] = refused
            try:
                with open(vo, "wb") as f:
                    f.write(b"abcdef")
                urllib.request.urlopen = lambda req, timeout=0: Resp([b"abcdef"])
                assert _fetch_to_file("https://hk.invalid/v", vo, **want) == 6 and not os.path.exists(vo + ".part")
                with open(vo, "wb") as f:
                    f.write(b"ABCDEF")  # same size, wrong bytes: what a force re-fetch is there to repair
                urllib.request.urlopen = lambda req, timeout=0: Resp([b"abcdef"])
                try:
                    _fetch_to_file("https://hk.invalid/v", vo, **want)
                    raise AssertionError("a corrupt dst that cannot be replaced must not pass as verified")
                except MirrorBusyError:
                    pass
                with open(vo + ".part", "rb") as f:
                    assert f.read() == b"abcdef", "the complete, verified .part waits for the next run"
            finally:
                globals()["replace_file"] = real_replace
            os.remove(vo)
            os.remove(vo + ".part")
            # the LOCAL side failing is no network error: MirrorWriteError, no .part left behind
            blocker = os.path.join(td, "blocker")
            open(blocker, "w").close()
            urllib.request.urlopen = lambda req, timeout=0: Resp([b"abcdef"])
            try:
                _fetch_to_file("https://hk.invalid/v", os.path.join(blocker, "x", "VO.pck"), **want)
                raise AssertionError("a folder that cannot be made must raise")
            except MirrorWriteError:
                pass
            HOTPATCH_DIR_saved, HOTPATCH_DIR = HOTPATCH_DIR, blocker
            try:
                hotpatch_fetch(["https://hk.invalid"], "x/VO.pck", expect={"md5": want["expect_md5"], "size": 6}, resume=True)
                raise AssertionError("must raise")
            except AgentError as e:
                assert e.status == 507, (e.status, e.message)
            finally:
                HOTPATCH_DIR = HOTPATCH_DIR_saved
        finally:
            urllib.request.urlopen = saved_open
    ok("_fetch_to_file touches the disk only after the upstream answered (404 / outage / cap leave no directory)")
    ok("_fetch_to_file refuses a declared length that cannot pass; resume keeps a cut .part and continues by Range")
    ok("_fetch_to_file: only a NETWORK cut keeps the .part (a dead CDN costs no hashing, a complete .part is "
       "adopted, should_stop keeps it); a local write failure is MirrorWriteError -> 507, no retry")

    # The agent state file: a broken state.json is never silently reset and never turns a routine
    # Start into a re-import; recovery markers are saved strictly. Temp dir + fakes, no docker.
    g = globals()
    faked_st = ("STATE_PATH", "PROVISION_MODE", "TOKEN", "read_manifest", "running_projects", "mysql_exec",
                "_compose_stream", "wait_for_mysql", "_write_state_atomic", "log_line", "current_job",
                "_read_state_bytes")
    saved_st = {k: g[k] for k in faked_st}
    saved_rec = dict(_STATE_RECOVERY)
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    with tempfile.TemporaryDirectory() as td:
        try:
            sp = os.path.join(td, "state.json")
            stack = os.path.join(td, "1.6_live")
            os.makedirs(stack)
            for name in ("docker-compose.yml", ".bootstrap.lock"):
                open(os.path.join(stack, name), "w").close()
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            g.update({"STATE_PATH": sp, "PROVISION_MODE": "once", "log_line": lambda *_a: None,
                      "read_manifest": lambda _v: {"files": [], "mysql": []},
                      "running_projects": lambda: (None, "docker unavailable (selftest)"),
                      "current_job": lambda **_k: None})
            _STATE_RECOVERY.update(degraded=None, corruptCopy=None)

            def state_bytes():
                with open(sp, "rb") as f:
                    return f.read()

            def put(data):
                with open(sp, "wb") as f:
                    f.write(data)

            def refused(fn, status, code):
                try:
                    fn()
                except AgentError as e:
                    assert e.status == status and e.code == code, (status, code, e.status, e.code, e.message)
                    return
                raise AssertionError("must be refused with %d %s" % (status, code))

            def disk_full(_data):
                raise OSError(28, "No space left on device")

            # a missing file is a fresh agent (and a fresh stack still provisions); a BOM is tolerated
            assert load_state() == {} and needs_provision("1.6") is True
            put(b'\xef\xbb\xbf{"lastStarted": "1.6"}')
            assert load_state() == {"lastStarted": "1.6"}

            # a torn write: every writer refuses with 503 and the bytes stay exactly as they were
            torn = b'{"versions": {"1.6": {"provisionedAt": "2026-08-14 21:03:11", "runni'
            put(torn)
            try:
                load_state()
                raise AssertionError("a torn state file must not read as {}")
            except StateUnreadable as e:
                assert "JSON" in e.reason, e.reason
            refused(lambda: state_set(lastStarted="1.6"), 503, "state_unreadable")
            refused(lambda: version_state_set("1.6", runningOk=False), 503, "state_unreadable")
            refused(lambda: version_state_set("1.6", strict=True, runningOk=False), 503, "state_unreadable")
            refused(lambda: set_policy({"playerCommands": True}), 503, "state_unreadable")
            refused(lambda: version_state("1.6"), 503, "state_unreadable")
            refused(lambda: needs_provision("1.6"), 503, "state_unreadable")
            refused(lambda: do_start("1.6", _Job()), 503, "state_unreadable")
            refused(lambda: do_setup("1.6", _Job()), 503, "state_unreadable")
            assert version_state_set("1.6", best_effort=True, runningOk=False) is False, "the stop path logs and skips"
            assert state_bytes() == torn, "a writer must never save over a file it could not read"
            assert _STATE_RECOVERY["degraded"] and state_readable_for("selftest loop") is False
            # readers degrade: policy fail-closed (signup + commands + upstream forced off, whatever the
            # defaults say), facts unknown, no generation and no signup for players
            pol = get_policy()
            assert not pol["signup"]["enabled"] and not pol["playerCommands"] and not pol["hotpatchUpstream"], pol
            full = g["status"]()  # `status` is a local loop name earlier in selftest()
            assert full["state"]["ok"] is False and full["state"]["degraded"] and "JSON" in full["state"]["error"], full["state"]
            assert full["versions"]["1.6"]["provisioned"] is None and full["versions"]["1.6"]["passwordVerify"] is None
            pub = build_public_status(full, p1)  # p1: signup + player commands ON (policy check above)
            assert pub["signup"]["enabled"] is False and pub["playerCommands"] is False, pub
            assert pub["versions"]["1.6"]["generation"] is None, pub
            assert secrets_info("1.6")["changedAt"] is None, "the secrets stay readable for a manual repair"
            # non-dict JSON and a malformed "versions" are unreadable too, never {}
            for bad in (b"[1, 2]", b'"x"', b'{"versions": []}', b'{"versions": {"1.6": 3}}'):
                put(bad)
                try:
                    load_state()
                    raise AssertionError("must be unreadable: %r" % bad)
                except StateUnreadable:
                    pass
            ok("state file: unreadable -> writers refuse (503, bytes untouched), stop path skips, readers degrade")

            # save: the current parsable file becomes .bak, no .tmp is left, the file reads again
            put(b'{"lastStarted": "2.8"}')
            assert state_set(lastStarted="1.6") is True and not _STATE_RECOVERY["degraded"]
            assert load_state() == {"lastStarted": "1.6"} and not os.path.exists(sp + ".tmp")
            with open(sp + ".bak", "rb") as f:
                assert json.loads(f.read()) == {"lastStarted": "2.8"}
            # a failed write: a convenience fact logs and goes on, a strict one is a 507 with a code
            g["_write_state_atomic"] = disk_full
            assert state_set(lastStarted="2.8") is False
            refused(lambda: version_state_set("1.6", strict=True, runningOk=False), 507, "state_write_failed")
            # ...and it stops the secrets job and a start before anything touches docker or MySQL
            calls = []
            g.update({"mysql_exec": lambda *a, **k: calls.append("mysql") or subprocess.CompletedProcess([], 0, stdout="1\n", stderr=""),
                      "_compose_stream": lambda *a, **k: calls.append("compose") or 0,
                      "wait_for_mysql": lambda *a, **k: calls.append("wait"),
                      "running_projects": lambda: ([VERSIONS["1.6"]["project"]], None)})
            with open(os.path.join(stack, ".env"), "w") as f:
                f.write("MYSQL_ROOT_PASSWORD=OldRoot_1234\nOUTER_IP=192.0.2.10\n")
            with open(os.path.join(stack, "docker-compose.yml.tmpl"), "w") as f:
                f.write("cmd: redis-server --requirepass Internal_1234\n")
            env_before = read_text(os.path.join(stack, ".env"))
            refused(lambda: do_secrets("1.6", _Job(), {"mysqlRoot": "NewRoot_1234"}), 507, "state_write_failed")
            assert calls == [] and read_text(os.path.join(stack, ".env")) == env_before, calls
            refused(lambda: do_start("1.6", _Job(), provision="none"), 507, "state_write_failed")
            assert calls == [], "no down, no compose before runningOk=False is on record"
            g["_write_state_atomic"] = saved_st["_write_state_atomic"]
            g["running_projects"] = lambda: (None, "docker unavailable (selftest)")
            assert load_state() == {"lastStarted": "1.6"}
            ok("state file: save keeps a .bak; strict marker failure aborts secrets/start before docker or MySQL (507)")

            # the provisioned marker survives a lost state file and re-seeds the same generation
            put(b"{}")
            write_provisioned_marker("1.6", "2026-08-14 21:03:11")
            j = _Job()
            assert needs_provision("1.6", j) is False and any(PROVISIONED_MARKER in l for l in j.lines), j.lines
            assert version_state("1.6")["provisionedAt"] == "2026-08-14 21:03:11"
            os.remove(provisioned_marker_path("1.6"))
            put(b"{}")
            assert needs_provision("1.6") is True, "no marker, no record, no guard: the first provision still runs"
            # migration: a record without a marker gets one, once (v2: progress back-filled as default)
            put(json.dumps({"versions": {"1.6": {"provisionedAt": "2026-08-01 10:00:00"}}}).encode())
            assert migrate_provisioned_markers() == 1 and read_provisioned_marker("1.6") == \
                {"at": "2026-08-01 10:00:00", "progress": "default", "defaultAccount": True}
            assert migrate_provisioned_markers() == 0
            # a fresh bootstrap (it wipes mysql/) forgets both (and the progress record with them)
            forget_provisioned("1.6", _Job())
            vs16 = version_state("1.6")
            assert read_provisioned_marker("1.6") is None and not vs16.get("provisionedAt")
            assert vs16.get("progress") is None and vs16["defaultAccount"] is False, vs16
            ok("provisioned marker: re-seeds a lost provisionedAt, migrated at start, forgotten by a fresh bootstrap")

            # startup: a read failure that clears before the second read leaves a HEALTHY file alone
            healthy = json.dumps({"policy": {"playerCommands": True}, "lastStarted": "1.6",
                                  "versions": {"1.6": {"runningOk": True, "passwordVerify": True}}}).encode()
            put(healthy)
            with open(sp + ".bak", "w") as f:
                json.dump({"lastStarted": "2.8"}, f)
            flaky = []

            def flaky_read(path, attempts=5, delay=0.2):
                if path == sp and not flaky:
                    flaky.append(path)
                    raise StateUnreadable(path, "permission denied (selftest)")
                return saved_st["_read_state_bytes"](path, attempts, delay)
            g["_read_state_bytes"] = flaky_read
            assert recover_state_at_startup(now=1757929900) == "ok" and flaky
            g["_read_state_bytes"] = saved_st["_read_state_bytes"]
            assert state_bytes() == healthy and not _STATE_RECOVERY["degraded"]
            assert not [n for n in os.listdir(td) if n.startswith("state.json.corrupt-")], "a healthy file is never a corrupt copy"
            ok("startup recovery: a transient read failure never rolls a healthy state file back to .bak")

            # startup: unreadable + a good .bak -> corrupt copy kept, .bak restored, runningOk cleared, guard set
            good = {"policy": {"playerCommands": True}, "lastStarted": "1.6",
                    "versions": {"1.6": {"runningOk": True, "txtFixes": "applied"}}}
            with open(sp + ".bak", "w") as f:
                json.dump(good, f)
            # the sdk still enforces a verification the .bak does not know about
            os.makedirs(os.path.join(stack, "sdk", "data"))
            with open(os.path.join(stack, "sdk", "data", "config.json.tmpl"), "w") as f:
                f.write('{\n  "auth": {\n    "enable_password_verify": true\n  }\n}\n')
            put(torn)
            assert recover_state_at_startup(now=1757930000) == "restored"
            st = load_state()
            assert st["versions"]["1.6"]["runningOk"] is False and st["policy"] == {"playerCommands": True}, st
            assert st["versions"]["1.6"]["passwordVerify"] is True, st
            copy = st["restoredFromBackup"]["corruptCopy"]
            assert copy.startswith("state.json.corrupt-") and os.sep not in copy, copy
            with open(os.path.join(td, copy), "rb") as f:
                assert f.read() == torn
            assert g["status"]()["state"]["restoredFromBackup"]["corruptCopy"] == copy
            j = _Job()
            assert needs_provision("1.6", j) is False and any("restored from a backup" in l for l in j.lines), j.lines
            release_provision_guard(_Job())
            assert load_state().get("restoredFromBackup"), "a version without a record keeps the guard"
            write_provisioned_marker("1.6", "2026-08-14 21:03:11")
            release_provision_guard(_Job())
            assert "restoredFromBackup" not in load_state()
            os.remove(provisioned_marker_path("1.6"))
            # switch mode: a rolled-back lastStarted must not re-import a provisioned stack while guarded
            g["PROVISION_MODE"] = "switch"
            prov16 = {"1.6": {"provisionedAt": "2026-08-14 21:03:11"}}
            put(json.dumps({"lastStarted": "2.8", "versions": prov16,
                            "restoredFromBackup": {"at": "2026-09-15 12:00:00", "corruptCopy": copy,
                                                   "reason": "selftest"}}).encode())
            j = _Job()
            assert needs_provision("1.6", j) is False and any("restored from a backup" in l for l in j.lines), j.lines
            put(json.dumps({"lastStarted": "2.8", "versions": prov16}).encode())
            assert needs_provision("1.6") is True, "unguarded, switch mode still re-imports on a version switch"
            g["PROVISION_MODE"] = "once"
            # startup: unreadable and no usable .bak -> left in place (never missing), degraded
            with open(sp + ".bak", "wb") as f:
                f.write(b"{nope")
            put(torn)
            assert recover_state_at_startup(now=1757930100) == "degraded"
            assert state_bytes() == torn and _STATE_RECOVERY["degraded"] and g["status"]()["state"]["degraded"]
            assert len([n for n in os.listdir(td) if n.startswith("state.json.corrupt-")]) == 1, \
                "the same broken bytes reuse their earlier copy"
            ok("startup recovery: .bak restored (corrupt copy, runningOk cleared, guard) or left in place degraded")

            # POST /agent/state/reset {"confirm": true}: token-only, refused while a job runs
            refused(lambda: reset_state(False), 400, None)
            g["current_job"] = lambda **_k: Job("start", "1.6", job_id="busy-selftest")
            refused(lambda: reset_state(True), 409, None)
            g["current_job"] = lambda **_k: None
            g["TOKEN"] = "selftest-token"
            reply = []
            hh = Handler.__new__(Handler)
            hh.path, hh.rfile = "/agent/state/reset", io.BytesIO(b'{"confirm": true}')
            hh.headers = {"Authorization": "Bearer selftest-token", "Content-Length": "17"}
            hh._send = lambda status_, obj, retry_after=None: reply.append((status_, obj))
            hh.do_POST()
            assert reply and reply[0][0] == 200 and reply[0][1]["ok"], reply
            rst = reply[0][1]["state"]
            assert rst["ok"] and not rst["degraded"] and rst["reset"]["corruptCopy"] == reply[0][1]["corruptCopy"], rst
            assert not _STATE_RECOVERY["degraded"] and state_set(lastStarted="1.6") is True
            assert needs_provision("1.6") is False, "after a reset a version with no record is not imported automatically"
            assert version_state("1.6").get("passwordVerify") is True, "a reset keeps the verification the sdk enforces"
            # a marked stack reads as provisioned right after the reset, with the value the next Start re-seeds
            assert g["status"]()["versions"]["1.6"]["provisioned"] is False
            write_provisioned_marker("1.6", "2026-08-14 21:03:11")
            s16 = g["status"]()["versions"]["1.6"]
            assert s16["provisioned"] is True and s16["provisionedAt"] == "2026-08-14 21:03:11", s16
            assert build_public_status(g["status"](), p1)["versions"]["1.6"]["generation"] == "2026-08-14 21:03:11"
            os.remove(provisioned_marker_path("1.6"))
            # a file that reads again is never reset from a stale screen (409, degraded note cleared); force can
            before = state_bytes()
            _STATE_RECOVERY["degraded"] = "stale screen (selftest)"
            refused(lambda: reset_state(True), 409, "state_readable")
            assert state_bytes() == before and not _STATE_RECOVERY["degraded"]
            reply.clear()
            hh.rfile = io.BytesIO(b'{"confirm": true, "force": true}')
            hh.headers = {"Authorization": "Bearer selftest-token", "Content-Length": "32"}
            hh.do_POST()
            assert reply and reply[0][0] == 200 and reply[0][1]["corruptCopy"], reply
            ok("state reset route OK (confirm, 409 while busy or readable, force, corrupt copy, guard, "
               "passwordVerify kept, marker in /status)")
        finally:
            g.update(saved_st)
            _STATE_RECOVERY.clear()
            _STATE_RECOVERY.update(saved_rec)
            _state_skip_logged.clear()
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_

    # The advertised address (agent 3.2). The database half is recorded apart from the XML half, so an
    # XML-only pass never strands the old WAN IP in dispatch_url / the gacha URLs, and a half-failed
    # pass re-run over rows that already hold the new IP never corrupts them. Then the DDNS follower:
    # address filter, boot fallback, two-check confirmation, never a netfix for a stopped version.
    # Temp dir + a fake MySQL that evaluates the REPLACE chains + fake compose/start_job; no DNS, no docker.
    faked_adv = ("STATE_PATH", "ADVERTISED_IP", "ADVERTISED_HOST", "ADVERTISED_ALLOW_PRIVATE", "BIND_IP",
                 "mysql_exec", "running_projects", "_compose_stream", "verify_stack", "start_job",
                 "log_line", "_is_global_ip", "read_hotpatch_manifest")
    saved_adv = {k: g[k] for k in faked_adv}
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    saved_follow = (dict(_ADVERTISE), dict(_next_follow), dict(_follow_refused), dict(_follow_failures))
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "1.6_live")
            conf = os.path.join(stack, "server", "dispatch", "conf")
            os.makedirs(conf)
            open(os.path.join(stack, ".bootstrap.lock"), "w").close()
            compose_path = os.path.join(stack, "docker-compose.yml")
            write_text(compose_path, 'ports:\n  - "192.0.2.57:21000:80"\n')
            write_text(os.path.join(stack, ".env"), "OUTER_IP=192.0.2.57\n")
            xml_path = os.path.join(conf, "dispatch.xml")
            write_text(xml_path, '<dispatch outer_ip="192.0.2.57" peer="172.10.3.8" />\n')
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            logs = []
            g.update({"STATE_PATH": os.path.join(td, "state.json"), "ADVERTISED_HOST": "", "BIND_IP": "",
                      "ADVERTISED_ALLOW_PRIVATE": False, "log_line": logs.append,
                      "read_hotpatch_manifest": lambda _v: None})
            _ADVERTISE.update(checkedAt=None, error=None, pending=None, loggedError=None)
            _next_follow.clear()
            _follow_refused.clear()
            _follow_failures.clear()

            # a fake MySQL holding the seven client-facing URL columns; an UPDATE is evaluated (the
            # REPLACE chain, innermost first) or fails as a whole, like the real statement
            cols = {"t_region_config": ["dispatch_url"],
                    "t_gacha_newbie_url_config": ["gacha_prob_url", "gacha_record_url"],
                    "t_gacha_schedule_config": ["gacha_prob_url", "gacha_record_url",
                                                "gacha_prob_url_oversea", "gacha_record_url_oversea"]}
            dbv = {t: {c: "http://127.0.0.1:21000/%s/%s" % (t, c) for c in cs} for t, cs in cols.items()}
            fail = {}  # table -> the stderr its UPDATE answers
            innermost = re.compile(r"REPLACE\('([^']*)', '([^']*)', '([^']*)'\)")

            def fake_mysql(version, sql, db=None, timeout=300, password=None):
                m = re.match(r"UPDATE \w+\.(\w+) SET (.*)$", sql, re.S)
                if not m:
                    return subprocess.CompletedProcess([], 0, stdout="", stderr="")
                table = m.group(1)
                if table in fail:
                    return subprocess.CompletedProcess([], 1, stdout="", stderr=fail[table])
                new_vals = {}
                for part in re.split(r", (?=\w+ = REPLACE\()", m.group(2)):
                    col, expr = part.split(" = ", 1)
                    expr = expr.replace("(%s, " % col, "('%s', " % dbv[table][col], 1)
                    n = 1
                    while n:
                        expr, n = innermost.subn(lambda r: "'%s'" % r.group(1).replace(r.group(2), r.group(3)), expr)
                    new_vals[col] = expr.strip("'")
                dbv[table].update(new_vals)
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")

            def all_db():
                return [u for t in dbv.values() for u in t.values()]

            def db_is(ip):
                return all(u.startswith("http://%s:21000/" % ip) for u in all_db())

            mysql_exec = fake_mysql
            # (1) a full pass: XML + database rewritten (the vendor 127.0.0.1 too), both halves recorded
            ADVERTISED_IP = "198.51.100.171"
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            vs = version_state("1.6")
            assert vs["advertisedIp"] == vs["advertisedIpSql"] == "198.51.100.171" and vs["advertisedIpSqlOwed"] == [], vs
            assert db_is("198.51.100.171"), dbv
            assert 'outer_ip="198.51.100.171"' in read_text(xml_path) and "172.10.3.8" in read_text(xml_path)
            assert not advertised_lags("1.6") and advertised_sql_current("1.6")
            # (2) the address changes while the stack is stopped: XML only (netfix on a stopped stack) --
            #     the old WAN IP stays owed, and the next database pass still replaces it
            ADVERTISED_IP = "203.0.113.17"
            apply_advertised_ip("1.6", _Job(), with_sql=False)
            vs = version_state("1.6")
            assert vs["advertisedIp"] == "203.0.113.17" and vs["advertisedIpSql"] == "198.51.100.171", vs
            assert vs["advertisedIpSqlOwed"] == ["198.51.100.171"] and db_is("198.51.100.171"), (vs, dbv)
            assert advertised_lags("1.6") and not advertised_sql_current("1.6")
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            assert db_is("203.0.113.17"), "the old WAN IP must not be stranded in the database: %r" % dbv
            assert version_state("1.6")["advertisedIpSql"] == "203.0.113.17" and not advertised_lags("1.6")
            # (3) a failed UPDATE does not advance advertisedIpSql; the new IP is a prefix of the old one
            ADVERTISED_IP = "203.0.113.1"
            fail["t_gacha_schedule_config"] = "ERROR 2013 (HY000): Lost connection to server during query"
            j3 = _Job()
            apply_advertised_ip("1.6", j3, with_sql=True)
            vs = version_state("1.6")
            assert vs["advertisedIpSql"] == "203.0.113.17" and vs["advertisedIpSqlOwed"] == ["203.0.113.17"], vs
            assert dbv["t_region_config"]["dispatch_url"].startswith("http://203.0.113.1:")
            assert dbv["t_gacha_schedule_config"]["gacha_prob_url"].startswith("http://203.0.113.17:"), dbv
            assert any("did not complete" in l for l in j3.lines) and any("(ignored)" in l for l in j3.lines), j3.lines
            del fail["t_gacha_schedule_config"]
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            assert db_is("203.0.113.1") and advertised_sql_current("1.6"), dbv
            # (4) the other way round: an owed old IP that is a PREFIX of the new one, re-run over rows the
            #     failed pass already moved -- a bare REPLACE would turn 203.0.113.17 into 203.0.113.177
            ADVERTISED_IP = "203.0.113.17"
            fail["t_gacha_newbie_url_config"] = "ERROR 2002 (HY000): Can't connect to local server through socket"
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            assert dbv["t_region_config"]["dispatch_url"].startswith("http://203.0.113.17:") and not advertised_sql_current("1.6")
            del fail["t_gacha_newbie_url_config"]
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            assert db_is("203.0.113.17") and not any("203.0.113.177" in u for u in all_db()), dbv
            chain = _sql_ip_rewrite("c", ["192.0.2.57", "203.0.113.17"], "203.0.113.1")
            assert chain.index("'203.0.113.17'") < chain.index("'203.0.113.1', '{relic-advertised}'"), \
                "an old IP containing the new one is rewritten before the new one is parked: " + chain
            # (5) a table this version does not have counts as done -- there is nothing in it to rewrite
            ADVERTISED_IP = "198.51.100.171"
            fail["t_gacha_newbie_url_config"] = ("ERROR 1146 (42S02) at line 1: Table "
                                                 "'hk4e_db_config.t_gacha_newbie_url_config' doesn't exist")
            j5 = _Job()
            apply_advertised_ip("1.6", j5, with_sql=True)
            assert advertised_sql_current("1.6") and any("not in this version" in l for l in j5.lines), j5.lines
            del fail["t_gacha_newbie_url_config"]
            # (the fake kept rows a version without that table would not have: align them)
            dbv["t_gacha_newbie_url_config"] = {c: "http://198.51.100.171:21000/n/" + c for c in cols["t_gacha_newbie_url_config"]}
            fail["t_region_config"] = "ERROR 1146 (42S02) at line 1: Table 'x' doesn't exist"
            try:
                run_sql("1.6", _Job(), ["UPDATE hk4e_db_deploy_config.t_region_config SET dispatch_url = 'x'"])
                raise AssertionError("missing_ok is opt-in: a plain run_sql must still fail loudly")
            except AgentError as e:
                assert "SQL failed" in e.message
            del fail["t_region_config"]
            # (6) the record is lost (a state reset): the address dispatch.xml holds is still an old IP, so a
            #     pass to a new address rewrites the XMLs AND the database rows the lost record knew about
            assert DISPATCH_OUTER_IP_RE.search("<outer_ip> 203.0.113.5</outer_ip>").group(1) == "203.0.113.5"
            assert dispatch_outer_ip("1.6") == "198.51.100.171"
            saved_rec6 = dict(_STATE_RECOVERY)
            try:
                with open(STATE_PATH, "wb") as f:
                    f.write(b"{torn")
                assert reset_state(True)["ok"] and not version_state("1.6"), load_state()
                ADVERTISED_IP = "203.0.113.50"
                apply_advertised_ip("1.6", _Job(), with_sql=True)
                assert 'outer_ip="203.0.113.50"' in read_text(xml_path) and db_is("203.0.113.50"), (read_text(xml_path), dbv)
            finally:
                _STATE_RECOVERY.clear()
                _STATE_RECOVERY.update(saved_rec6)
            ADVERTISED_IP = "198.51.100.171"  # back to what the steps below expect the stack to hold
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            assert db_is("198.51.100.171") and not advertised_lags("1.6"), dbv
            ok("advertised IP: the database half is recorded apart (XML-only pass, failed UPDATE, missing "
               "table), the REPLACE chain is prefix-safe over rows already moved, a lost record falls back to "
               "the address dispatch.xml holds")

            # -- the DDNS follower --
            assert _advertised_config({"GIO_ADVERTISED_HOST": " game.example.com. ",
                                       "GIO_ADVERTISED_IP": "198.51.100.171"}) == ("game.example.com", "", True)
            assert _advertised_config({"GIO_ADVERTISED_IP": " 198.51.100.171 "}) == ("", "198.51.100.171", False)
            assert _advertised_config({"GIO_ADVERTISED_HOST": " "}) == ("", "", False)
            assert (_parse_check_every("5"), _parse_check_every(""), _parse_check_every("x"),
                    _parse_check_every("300")) == (30, 120, 120, 300)
            # the filter with ipaddress' real verdicts -- documentation ranges are not public either
            for bad in ("10.0.0.5", "192.168.0.10", "172.16.4.4", "100.64.0.1", "127.0.0.1", "169.254.1.1",
                        "0.0.0.0", "0.1.2.3", "224.0.0.1", "240.0.0.1", "198.51.100.171", "2001:db8::1", "x", ""):
                assert not advertisable_ip(bad, False), bad
            for priv in ("10.0.0.5", "192.168.0.10", "172.16.4.4"):
                assert advertisable_ip(priv, True), priv
            for never in ("100.64.0.1", "127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "198.18.0.1"):
                assert not advertisable_ip(never, True), never
            doc_nets = (ipaddress.ip_network("198.51.100.0/24"), ipaddress.ip_network("203.0.113.0/24"))
            g["_is_global_ip"] = lambda ip: ip.is_global or any(ip in n for n in doc_nets)

            def answers(*addrs):
                return lambda _host: list(addrs)

            def gaierror(_host):
                raise socket.gaierror(-2, "Name or service not known")

            r = _resolve_advertised("game.example.com", answers("10.0.0.5", "100.64.1.1", "127.0.0.1"), allow_private=False)
            assert r[0] is None and "ALLOW_PRIVATE" in r[1], r
            assert _resolve_advertised("game.example.com", answers("10.0.0.5", "100.64.1.1", "127.0.0.1"),
                                       allow_private=True) == ("10.0.0.5", None)
            multi = answers("203.0.113.9", "198.51.100.171", "203.0.113.9")
            assert _resolve_advertised("game.example.com", multi, current="203.0.113.9") == ("203.0.113.9", None), \
                "the current A record is kept (no flapping between records)"
            assert _resolve_advertised("game.example.com", multi, current="203.0.113.50") == ("198.51.100.171", None)
            assert "cannot resolve" in _resolve_advertised("game.example.com", gaierror)[1]
            assert "no IPv4" in _resolve_advertised("game.example.com", answers())[1]
            gate = threading.Event()
            assert "no DNS answer" in _resolve_advertised("game.example.com", lambda _h: gate.wait(5) and [], timeout=0.2)[1]
            assert "not returned yet" in _resolve_advertised("game.example.com", answers("203.0.113.9"))[1], \
                "never a second lookup thread while one hangs"
            gate.set()
            _advertise_lookup["thread"].join(5)
            assert _resolve_advertised("game.example.com", answers("203.0.113.9")) == ("203.0.113.9", None)
            ok("advertised host: config precedence (host wins), address filter (private/CGNAT/loopback/reserved, "
               "ALLOW_PRIVATE), keep-current among A records, lookup timeout without piling threads")

            # boot: never confirmed + DNS down -> nothing advertised, yet the box stays "reachable"
            ADVERTISED_HOST, ADVERTISED_IP = "game.example.com", ""
            fake_t = [0.0]

            def fake_sleep(s):
                fake_t[0] += s

            assert resolve_advertised_at_boot(gaierror, budget=6, sleep=fake_sleep, clock=lambda: fake_t[0]) == "unresolved"
            assert fake_t[0] >= 4, "a fast-failing lookup is retried inside the budget"
            assert ADVERTISED_IP == "" and advertise_intent() and reachable_intent()
            write_text(compose_path, 'ports:\n  - "127.0.0.1:21000:80"\n')
            assert compose_binds_loopback("1.6"), "an unresolved host keeps the loopback-render guard armed"
            write_text(compose_path, 'ports:\n  - "192.0.2.57:21000:80"\n')
            BIND_IP = "127.0.0.1"
            try:
                ensure_bind_ip("1.6", _Job())
                raise AssertionError("a loopback bind IP with an (unresolved) advertised host must be refused")
            except AgentError as e:
                assert "loopback" in e.message, e.message
            BIND_IP = ""
            assert hotpatch_public_url("1.6", {"branch": "1.6_live"}, strict=False)[0] is None, \
                "an unresolved host never falls back to a LAN mirror URL"
            assert apply_advertised_ip("1.6", _Job()) == 0
            assert resolve_advertised_at_boot(answers("203.0.113.9"), sleep=fake_sleep, clock=lambda: fake_t[0]) == "resolved"
            assert ADVERTISED_IP == "203.0.113.9" and load_state()["advertisedResolvedIp"] == "203.0.113.9"
            ADVERTISED_IP = ""
            assert resolve_advertised_at_boot(gaierror, budget=3, sleep=fake_sleep, clock=lambda: fake_t[0]) == "fallback"
            assert ADVERTISED_IP == "203.0.113.9", "DNS down at boot: the last confirmed address"
            ok("advertised host at boot: resolved + remembered, fallback to advertisedResolvedIp, an unresolved "
               "host keeps reachable intent (bind guard, loopback-render guard, no LAN mirror URL)")

            # the watchdog -- nothing running: two identical answers, then only the address is swapped
            started, busy = [], [False]

            def fake_start_job(kind, version, fn, **_kw):
                if busy[0]:
                    raise AgentError(409, "busy (selftest)")
                started.append((kind, version, fn))
                return Job(kind, version, job_id="follow-%d" % len(started))

            g["start_job"] = fake_start_job
            running_projects = lambda: ([], None)  # noqa: E731
            _ADVERTISE.update(pending=None, error=None, loggedError=None)
            new = answers("203.0.113.50")
            assert advertise_tick(new) == "pending" and ADVERTISED_IP == "203.0.113.9"
            assert advertise_tick(gaierror) == "error" and _ADVERTISE["pending"] is None and _ADVERTISE["error"], \
                "a failed lookup breaks the run of identical answers"
            assert advertise_tick(new) == "pending" and _ADVERTISE["pending"] == "203.0.113.50"
            assert advertise_tick(new) == "swapped" and ADVERTISED_IP == "203.0.113.50" and not started
            assert load_state()["advertisedResolvedIp"] == "203.0.113.50" and _ADVERTISE["error"] is None
            # listed by compose but not vouched for by runningOk: a stopped stack for the follower
            running_projects = lambda: ([VERSIONS["1.6"]["project"]], None)  # noqa: E731
            version_state_set("1.6", runningOk=False)
            assert advertise_tick(new) == "idle" and not started, "never a netfix for a version that is not up"
            # a running stack: a 409 keeps the confirmation, the next sweep starts the netfix job
            ADVERTISED_IP = "198.51.100.171"  # what the stack holds (step 5): nothing lags
            version_state_set("1.6", runningOk=True)
            nxt = answers("203.0.113.60")
            busy[0] = True
            assert advertise_tick(nxt) == "pending"
            assert advertise_tick(nxt) == "busy" and _ADVERTISE["pending"] == "203.0.113.60"
            busy[0] = False
            assert advertise_tick(nxt) == "job" and [(k, v) for k, v, _f in started] == [("netfix", "1.6")]
            assert ADVERTISED_IP == "198.51.100.171", "the address changes inside the job, under _op_lock"
            assert _next_follow["1.6"] > time.time() + 800 and _ADVERTISE["pending"] is None
            verified, restarted = [], []
            verify_stack = lambda v, j, rounds=0: verified.append(v)  # noqa: E731
            _compose_stream = lambda d, j, *a, **k: restarted.append(a) or 0  # noqa: E731
            jf = _Job()
            with _op_lock:
                res = started[0][2](jf)
            assert res == {"advertisedIp": "203.0.113.60", "netfix": True, "restarted": True}, (res, jf.lines)
            assert ADVERTISED_IP == "203.0.113.60" and db_is("203.0.113.60"), dbv
            assert 'outer_ip="203.0.113.60"' in read_text(xml_path) and verified == ["1.6"]
            assert restarted and restarted[0][0] == "restart" and not advertised_lags("1.6"), restarted
            # a follow whose database half fails: the job fails, the backoff holds, the catch-up retries
            started.clear()
            _next_follow.clear()
            fail["t_region_config"] = "ERROR 2013 (HY000): Lost connection to server during query"
            nxt2 = answers("203.0.113.70")
            assert advertise_tick(nxt2) == "pending" and advertise_tick(nxt2) == "job"
            n_restarts, n_verified = len(restarted), len(verified)
            try:
                with _op_lock:
                    started[0][2](_Job())
                raise AssertionError("a follow whose database half failed must fail its job")
            except AgentError as e:
                assert e.status == 500 and "database half" in e.message and "15 minutes" in e.message, e.message
            assert ADVERTISED_IP == "203.0.113.70" and advertised_lags("1.6")
            assert len(restarted) == n_restarts + 1, "the first follow is the full netfix: it restarts"
            assert _follow_failures["1.6"]["count"] == 1 and "database half" in (follow_error() or ""), _follow_failures
            assert advertise_tick(nxt2) == "idle" and len(started) == 1, "no retry inside the backoff"
            # the database half keeps failing: every catch-up retries only the database -- no restart, no
            # kicked players -- and waits twice as long as the one before
            for n, wait in ((2, 1800), (3, 3600)):
                _next_follow["1.6"] = 0
                assert advertise_tick(nxt2) == "idle", "the failure backoff outlasts the plain one"
                _follow_failures["1.6"]["retryAt"] = 0
                assert advertise_tick(nxt2) == "job" and len(started) == n
                jr = _Job()
                try:
                    with _op_lock:
                        started[-1][2](jr)
                    raise AssertionError("the database half still fails")
                except AgentError as e:
                    assert "%d minutes" % (wait // 60) in e.message, e.message
                assert len(restarted) == n_restarts + 1 and any("not restarting" in l for l in jr.lines), jr.lines
                assert _follow_failures["1.6"]["count"] == n and _follow_failures["1.6"]["retryAt"] > time.time() + wait - 60
            _next_follow["1.6"] = 0
            assert not _follow_blocked("1.6") and _follow_blocked("1.6", retry=True), \
                "a confirmed address change is not held back by the failure backoff"
            _follow_failures["9.9"] = {"count": 30}
            assert _follow_failed("9.9", "selftest") == ADVERTISE_RETRY_MAX
            del _follow_failures["9.9"]
            # the database answers again: the retry completes it and restarts the services once
            del fail["t_region_config"]
            _follow_failures["1.6"]["retryAt"] = 0
            assert advertise_tick(nxt2) == "job" and len(started) == 4, "the catch-up retries after the backoff"
            with _op_lock:
                assert started[-1][2](_Job()) == {"advertisedIp": "203.0.113.70", "netfix": True, "restarted": True}
            assert len(restarted) == n_restarts + 2 and len(verified) == n_verified + 2, (restarted, verified)
            assert db_is("203.0.113.70") and not advertised_lags("1.6") and follow_error() is None, dbv
            # healed by something else (a start, a manual netfix): the next sweep forgets the failures
            _follow_failures["1.6"] = {"count": 2, "retryAt": time.time() + 900, "error": "selftest"}
            _next_follow["1.6"] = 0
            assert advertise_tick(nxt2) == "idle" and "1.6" not in _follow_failures
            # a stack never advertised before whose first database half fails (the address arrived while it
            # ran, or its start raced MySQL): no old IP is owed, yet the failure must stay visible and the
            # catch-up must retry it -- the half is recorded as never done
            vrec = load_state()
            for k in ("advertisedIp", "advertisedIpSql", "advertisedIpSqlOwed"):
                vrec["versions"]["1.6"].pop(k, None)
            save_state(vrec, strict=True)
            fail["t_region_config"] = "ERROR 2013 (HY000): Lost connection to server during query"
            apply_advertised_ip("1.6", _Job(), with_sql=True)
            vs = version_state("1.6")
            assert "advertisedIpSql" in vs and vs["advertisedIpSql"] is None and advertised_lags("1.6"), vs
            started.clear()
            _next_follow.clear()
            _follow_failures["1.6"] = {"count": 1, "retryAt": 0, "error": "selftest"}
            assert advertise_tick(nxt2) == "job" and len(started) == 1 and follow_error() == "selftest", \
                "a failed first database half is retried, and its failure is not wiped as healed"
            del fail["t_region_config"]
            n_restarts = len(restarted)
            with _op_lock:
                assert started[-1][2](_Job()) == {"advertisedIp": "203.0.113.70", "netfix": True, "restarted": True}
            assert len(restarted) == n_restarts + 1 and not advertised_lags("1.6") and follow_error() is None
            # a skipped database half over a pre-3.2 record already vouching for the address changes nothing
            vrec = load_state()
            vrec["versions"]["1.6"].pop("advertisedIpSql")
            save_state(vrec, strict=True)
            apply_advertised_ip("1.6", _Job(), with_sql=False)
            assert "advertisedIpSql" not in version_state("1.6") and not advertised_lags("1.6"), version_state("1.6")
            version_state_set("1.6", advertisedIpSql="203.0.113.70")
            # a change confirmed inside the plain backoff is only swapped; the config files are then behind
            # the address, so the first sweep after the plain backoff starts the job -- never held by the
            # failure backoff meant for database-only retries
            started.clear()
            _next_follow["1.6"] = time.time() + 600
            _follow_failures["1.6"] = {"count": 3, "retryAt": time.time() + 3600, "error": "selftest"}
            nxt4 = answers("203.0.113.85")
            assert advertise_tick(nxt4) == "pending" and advertise_tick(nxt4) == "swapped" and not started
            assert ADVERTISED_IP == "203.0.113.85" and advertise_tick(nxt4) == "idle" and not started
            _next_follow["1.6"] = 0
            assert advertise_tick(nxt4) == "job" and len(started) == 1, "stale config files wait only the plain backoff"
            started.clear()
            _next_follow.clear()
            _follow_failures.clear()
            ADVERTISED_IP = "203.0.113.70"
            # a stack gone by the time its job runs is not netfixed; the address is swapped all the same
            running_projects = lambda: ([], None)  # noqa: E731
            n_restarts = len(restarted)
            with _op_lock:
                assert do_advertise_follow("1.6", _Job(), "203.0.113.75") == {"advertisedIp": "203.0.113.75", "netfix": False}
            assert ADVERTISED_IP == "203.0.113.75" and len(restarted) == n_restarts
            ADVERTISED_IP = "203.0.113.70"
            running_projects = lambda: ([VERSIONS["1.6"]["project"]], None)  # noqa: E731
            # a loopback-rendered stack: no netfix job (it would 409 every sweep), refusal logged once
            write_text(compose_path, 'ports:\n  - "127.0.0.1:21000:80"\n')
            started.clear()
            _next_follow.clear()
            del logs[:]
            nxt3 = answers("203.0.113.80")
            assert advertise_tick(nxt3) == "pending" and advertise_tick(nxt3) == "swapped" and not started
            assert advertise_tick(nxt3) == "idle" and advertise_tick(nxt3) == "idle" and not started
            assert sum("127.0.0.1" in l for l in logs) == 1, logs
            write_text(compose_path, 'ports:\n  - "192.0.2.57:21000:80"\n')
            # admin /status carries the follower; /public/status carries no address at all
            running_projects = lambda: ([], None)  # noqa: E731
            full = g["status"]()
            assert full["advertisedHost"] == "game.example.com" and full["advertisedIp"] == "203.0.113.80", full
            assert full["advertisedCheckedAt"] and full["advertisedError"] is None and full["advertisedPending"] is None
            assert full["versions"]["1.6"]["advertisedIpSql"] == "203.0.113.70", full["versions"]["1.6"]
            pub = json.dumps(build_public_status(full, get_policy()))
            assert "203.0.113" not in pub and "game.example.com" not in pub and "advertised" not in pub, pub
            ADVERTISED_HOST = ""
            assert advertise_tick(nxt3) == "off"
            ok("advertise watchdog: two-check confirmation, 409 keeps it, swap only for stopped / unvouched / "
               "loopback-rendered stacks, follow job re-applies both halves + verify, a failed follow backs off "
               "then catches up; /public/status carries no address")
        finally:
            g.update(saved_adv)
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_
            _ADVERTISE.clear()
            _ADVERTISE.update(saved_follow[0])
            _next_follow.clear()
            _next_follow.update(saved_follow[1])
            _follow_refused.clear()
            _follow_refused.update(saved_follow[2])
            _follow_failures.clear()
            _follow_failures.update(saved_follow[3])

    # SERVER PACKAGE DOWNLOAD (agent 3.4): the shipped catalogues, the validator, the extractor
    # command lines, the probe rule, the bundle-dir guard, the nested 2.8 layout heal, the status
    # brief (never public) and a dry run of the fetch job with a fake downloader + extractor.
    # A catalogue is OPTIONAL by contract (no stack.json = this version cannot be downloaded, the
    # admin brings the package) -- an installed box whose GIO_PAYLOAD_DIR predates 3.4 must not have
    # its upgrade aborted here; what IS shipped is validated.
    for v in VERSIONS:
        cat = read_stack_catalog(v)
        if cat is None:
            log_line("selftest: no stack catalogue for %s (a version that cannot be downloaded) -- skipped" % v)
            continue
        assert validate_stack_catalog(cat), "payloads/%s/stack.json invalid" % v
        assert cat["version"] == v and cat["topDir"] == "%s_live" % v and cat["label"] == "Internet Archive", cat
        assert urllib.parse.urlsplit(cat["url"]).hostname == "archive.org" and cat["extractedSize"] > cat["size"], cat
    for v, strays in (("2.8", ["data.txt"]), ("1.6", [])):
        cat = read_stack_catalog(v)
        assert cat is None or cat["strays"] == strays, (v, cat)
    good = {"url": "https://archive.org/download/x/y.7z", "size": 10, "md5": "a" * 32, "extractedSize": 30,
            "topDir": "1.6_live", "strays": ["data.txt"]}
    for bad in (dict(good, url="http://archive.org/x.7z"), dict(good, url="file:///x.7z"), dict(good, url="https://a b/x"),
                dict(good, topDir="../x"), dict(good, topDir="a/b"), dict(good, topDir="a\\b"), dict(good, topDir=".hidden"),
                dict(good, topDir=""), dict(good, md5="a" * 31), dict(good, md5="g" * 32), dict(good, size=0),
                dict(good, size=True), dict(good, size="10"), dict(good, strays=["a/b"]), dict(good, strays="data.txt"),
                dict(good, extractedSize=-1), "nope", None):
        try:
            validate_stack_catalog(bad)
            raise AssertionError("must refuse %r" % (bad,))
        except ValueError:
            pass
    assert validate_stack_catalog(dict(good, strays=[])) and validate_stack_catalog({k: x for k, x in good.items() if k != "strays"})
    assert extract_argv("/usr/bin/7zz", "/a.7z", "/out") == ["/usr/bin/7zz", "x", "-y", "-bd", "-o/out", "/a.7z"]
    for t in ("7z", "7za", "7zr"):
        assert extract_argv("/usr/bin/" + t, "/a.7z", "/out") == ["/usr/bin/" + t, "x", "-y", "-bd", "-o/out", "/a.7z"], t
    assert extract_argv("/usr/bin/bsdtar", "/a.7z", "/out") == ["/usr/bin/bsdtar", "-xf", "/a.7z", "-C", "/out"]
    assert extract_argv(r"C:\Windows\System32\tar.exe", r"C:\a.7z", r"C:\out") == \
        [r"C:\Windows\System32\tar.exe", "-xf", r"C:\a.7z", "-C", r"C:\out"]
    assert extract_argv(r"C:\Program Files\7-Zip\7z.exe", r"C:\a.7z", r"C:\out") == \
        [r"C:\Program Files\7-Zip\7z.exe", "x", "-y", "-bd", r"-oC:\out", r"C:\a.7z"]
    assert extract_argv(r"C:\Program Files\Bandizip\bz.exe", r"C:\a.7z", r"C:\out") == \
        [r"C:\Program Files\Bandizip\bz.exe", "x", "-y", r"-o:C:\out", r"C:\a.7z"]
    assert _extractor_kind("tar") == "bsdtar" and _extractor_kind("7ZZ") == "7z" and _extractor_kind("BZ.EXE") == "bz"
    assert probe_authority("0.0.0.0:18080") == "127.0.0.1:18080" and probe_authority("[::]:18080") == "127.0.0.1:18080"
    assert probe_authority("192.0.2.10:18081") == "192.0.2.10:18081"
    assert probe_authority("[2001:db8::1]:18080") == "[2001:db8::1]:18080"
    assert probe_authority("") == "127.0.0.1:18080" and probe_authority("18081") == "127.0.0.1:18081"
    assert _cli_list("--fetch", ["--config", "c", "--fetch", "1.6", "2.8", "--log", "x"]) == ["1.6", "2.8"]
    assert _cli_list("--fetch", ["--fetch"]) == [] and _cli_list("--fetch", ["--fetch", "2.8", "--config", "c"]) == ["2.8"]
    assert _bundle_dir_inside_mirror("/var/lib/gio-agent/hotpatch/bundles", "/var/lib/gio-agent/hotpatch")
    assert _bundle_dir_inside_mirror("/var/lib/gio-agent/hotpatch", "/var/lib/gio-agent/hotpatch/")
    assert not _bundle_dir_inside_mirror("/var/lib/gio-agent/hotpatch-bundles", "/var/lib/gio-agent/hotpatch")
    ok("stack catalogues + validator, extractor argv, probe authority, --fetch argv, bundle-dir guard OK")

    with tempfile.TemporaryDirectory() as td:
        stack = os.path.join(td, "2.8_live")
        nested = os.path.join(stack, "server", "data", "2.8_live-output_9464149-server-data")
        fixture = ((nested, ("txt", "ConstValueData.txt"), "nested const"),
                   (nested, ("txt", "MaterialDeleteData.txt"), "vendor's copy"),
                   (nested, ("json", "a", "b.json"), "nested json"),
                   (nested, ("lua", "common", "x.lua"), "nested lua"),
                   (nested, ("server_data_version.txt",), "9464149"),
                   (nested, ("notes.md",), "not hoisted"),
                   (os.path.join(stack, "server", "data"), ("json", "a", "b.json"), "older json"),
                   (os.path.join(stack, "server", "data"), ("json", "only_top.json"), "top only"),
                   (os.path.join(stack, "server", "data"), ("txt", "MaterialDeleteData.txt"), "agent's copy"))
        for base, rel, body in fixture:
            p = os.path.join(base, *rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(body)
        owned = {"server/data/txt/MaterialDeleteData.txt"}
        logs = []
        n = heal_nested_server_data(stack, owned, logs.append)
        assert n == 4, (n, logs)
        data = os.path.join(stack, "server", "data")
        for rel, body in ((("txt", "ConstValueData.txt"), "nested const"), (("json", "a", "b.json"), "nested json"),
                          (("lua", "common", "x.lua"), "nested lua"), (("server_data_version.txt",), "9464149"),
                          (("txt", "MaterialDeleteData.txt"), "agent's copy"), (("json", "only_top.json"), "top only")):
            with open(os.path.join(data, *rel)) as f:
                assert f.read() == body, (rel, f.read())
        assert os.path.isdir(nested) and os.listdir(nested) == ["notes.md"], "a leftover the server never reads stays"
        assert any("not empty after the hoist" in l for l in logs), logs
        assert heal_nested_server_data(stack, owned, logs.append) == 0, "idempotent"
        os.remove(os.path.join(nested, "notes.md"))
        assert heal_nested_server_data(stack, owned, logs.append) == 0 and not os.path.exists(nested), "emptied -> removed"
        flat = os.path.join(td, "1.6_live", "server", "data", "txt")
        os.makedirs(flat)
        open(os.path.join(flat, "ConstValueData.txt"), "w").close()
        n_logs = len(logs)
        assert heal_nested_server_data(os.path.join(td, "1.6_live"), set(), logs.append) == 0
        assert os.path.isfile(os.path.join(flat, "ConstValueData.txt")) and len(logs) == n_logs, "a flat tree: silent no-op"
        assert heal_nested_server_data(os.path.join(td, "nothing"), set(), logs.append) == 0 and len(logs) == n_logs
    ok("nested server/data layout heal OK (hoist, owned keeps the top copy, nested wins, leftover logged, "
       "idempotent, flat tree + missing data no-op)")

    g = globals()
    faked_f = ("PAYLOAD_DIR", "STATE_PATH", "find_extractor", "extract_archive", "_fetch_to_file", "log_line",
               "running_projects", "read_manifest", "FETCH_DISK_RESERVE", "_free_bytes_at")
    saved_f = {k: g[k] for k in faked_f}
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    saved_sleep = time.sleep
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "srv", "1.6_live")
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            g.update({"PAYLOAD_DIR": os.path.join(td, "payloads"), "STATE_PATH": os.path.join(td, "state.json"),
                      "log_line": lambda *_a: None, "running_projects": lambda: ([], None),
                      "read_manifest": lambda _v: None, "FETCH_DISK_RESERVE": 1 << 20,
                      "find_extractor": lambda: ("/fake/7zz", "7z")})
            body = b"7z\xbc\xaf\x27\x1c fake archive bytes " * 100
            cat = {"version": "1.6", "url": "https://archive.org/download/test/1.6_live_gio.7z", "size": len(body),
                   "md5": hashlib.md5(body).hexdigest(), "extractedSize": 2 * len(body), "topDir": "1.6_live",
                   "label": "Internet Archive", "strays": ["data.txt"]}
            os.makedirs(os.path.join(PAYLOAD_DIR, "1.6"))
            with open(os.path.join(PAYLOAD_DIR, "1.6", "stack.json"), "w") as f:
                json.dump(cat, f)
            assert fetch_brief("1.6") == {"available": True, "size": len(body), "host": "archive.org", "topDir": "1.6_live",
                                          "part": 0, "archive": False, "tool": "7zz", "fetchedAt": None}, fetch_brief("1.6")
            assert fetch_brief("2.8") == {"available": False, "size": 0, "host": None, "topDir": None, "part": 0,
                                          "archive": False, "tool": "7zz", "fetchedAt": None}, fetch_brief("2.8")
            os.makedirs(os.path.dirname(stack))
            with open(archive_path("1.6") + ".part", "wb") as f:
                f.write(b"abc")
            assert fetch_brief("1.6")["part"] == 3
            # never in the public projection -- not from a hand-made status, not from status() itself
            full = {"versions": {"1.6": {"present": False, "fetch": fetch_brief("1.6")},
                                 "2.8": {"present": True, "up": False, "fetch": {"available": True, "host": "archive.org"}}},
                    "state": {"ok": True}, "platform": "linux"}
            pub = build_public_status(full, default_policy())
            assert "fetch" not in pub["versions"]["1.6"] and "fetch" not in pub["versions"]["2.8"], pub
            assert "archive.org" not in json.dumps(pub) and pub["versions"]["1.6"] == {"present": False}
            g["running_projects"] = lambda: (None, "docker unavailable (selftest)")
            full = g["status"]()  # (a local `status` shadows the function inside selftest)
            assert full["versions"]["1.6"]["fetch"]["available"] and full["versions"]["1.6"]["fetch"]["part"] == 3, full
            assert full["versions"]["2.8"]["fetch"]["available"] is False
            assert "fetch" not in json.dumps(build_public_status(full, default_policy()))
            os.remove(archive_path("1.6") + ".part")
            # the synchronous refusals
            g["find_extractor"] = lambda: None
            try:
                fetch_preflight("1.6")
                raise AssertionError("no extractor must be refused")
            except AgentError as e:
                assert e.status == 409 and "extractor" in e.message, e.message
            g["find_extractor"] = lambda: ("/fake/7zz", "7z")
            for v_, want in (("2.8", 404), ("3.0", 400)):
                try:
                    fetch_preflight(v_)
                    raise AssertionError("must refuse %s" % v_)
                except AgentError as e:
                    assert e.status == want, (v_, e.status, e.message)
            os.makedirs(stack)
            with open(os.path.join(stack, "junk.txt"), "w") as f:
                f.write("x")
            try:
                fetch_preflight("1.6")
                raise AssertionError("a non-empty non-stack dir must be refused")
            except AgentError as e:
                assert e.status == 409 and "not a server stack" in e.message, e.message
            os.remove(os.path.join(stack, "junk.txt"))
            g["_free_bytes_at"] = lambda _p: 10
            try:
                fetch_preflight("1.6")
                raise AssertionError("a full disk must be refused")
            except AgentError as e:
                assert e.status == 409 and "Not enough free disk space" in e.message, e.message
            g["_free_bytes_at"] = saved_f["_free_bytes_at"]
            with open(os.path.join(PAYLOAD_DIR, "1.6", "stack.json"), "w") as f:
                json.dump(dict(cat, topDir="../x"), f)
            try:
                fetch_preflight("1.6")
                raise AssertionError("an invalid catalogue must be refused")
            except AgentError as e:
                assert e.status == 500 and "invalid" in e.message, e.message
            with open(os.path.join(PAYLOAD_DIR, "1.6", "stack.json"), "w") as f:
                json.dump(cat, f)
            assert fetch_preflight("1.6")[1] == "/fake/7zz", "an EMPTY target dir is fine"
            # the job, with a fake downloader (writes the catalogue's bytes) and a fake extractor
            fetched, extracted, fail_extract = [], [], []

            def fake_fetch2(url, dst, timeout=60, cap=None, expect_md5=None, expect_size=None, resume=False,
                            should_stop=None, progress=None):
                fetched.append(url)
                assert expect_md5 == cat["md5"] and expect_size == len(body) and resume and should_stop is not None
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst + ".part", "wb") as f:
                    f.write(body)
                if progress is not None:
                    progress(len(body))
                replace_file(dst + ".part", dst)
                return len(body)

            def fake_extract2(archive, dest, job, should_stop=None, timeout=None):
                extracted.append(archive)
                assert os.path.isfile(archive) and os.path.basename(dest) == ".relic-extract-1.6_live", dest
                if fail_extract:
                    raise ExtractError("7zz exited with code 1: simulated")
                top = os.path.join(dest, "1.6_live")
                deep = os.path.join(top, "server", "data", "1.6_live-output_1-server-data", "txt")
                os.makedirs(deep)
                for p_, text in ((os.path.join(top, "docker-compose.yml.tmpl"), "services: {}"),
                                 (os.path.join(top, "bootstrap.sh"), "#!/bin/sh"),
                                 (os.path.join(deep, "ConstValueData.txt"), "k\tv"),
                                 (os.path.join(dest, "data.txt"), "stray")):
                    with open(p_, "w") as f:
                        f.write(text)
                return 0.5

            g["_fetch_to_file"], g["extract_archive"] = fake_fetch2, fake_extract2
            # (1) the extractor fails: 500, the verified archive is KEPT, present stays False, no temp folder
            fail_extract.append(1)
            jf = _Job()
            try:
                do_fetch("1.6", jf)
                raise AssertionError("a failed extraction must fail the job")
            except AgentError as e:
                assert e.status == 500 and "kept" in e.message and "simulated" in e.message, e.message
            assert os.path.isfile(archive_path("1.6")) and not is_present("1.6") and not os.path.exists(extract_tmp("1.6"))
            assert fetched == [cat["url"]] and fetch_brief("1.6")["archive"] is True
            fail_extract.clear()
            # (2) the retry adopts the verified archive (no download), unpacks, renames, heals the nested
            #     layout, repairs the exec bits, drops the stray + temp + archive, records the fetch
            jd = _Job()
            res = do_fetch("1.6", jd)
            assert res == {"fetched": True, "downloaded": False, "bytes": len(body), "healed": 1,
                           "execBits": 1 if os.name == "posix" else 0, "dir": stack}, res
            assert is_present("1.6") and jd.yielding is True and fetched == [cat["url"]], "adopted: no second download"
            assert not os.path.exists(archive_path("1.6")) and not os.path.exists(extract_tmp("1.6"))
            assert os.path.isfile(os.path.join(stack, "server", "data", "txt", "ConstValueData.txt"))
            assert not os.path.exists(os.path.join(os.path.dirname(stack), "data.txt")), "the stray went with the temp folder"
            vs = version_state("1.6")
            assert vs.get("fetchedAt") and vs.get("fetchedFrom") == "archive.org", vs
            assert fetch_brief("1.6")["fetchedAt"] == vs["fetchedAt"] and fetch_brief("1.6")["archive"] is False
            assert any("pre-seeded" in l for l in jd.lines) and any("Installed into" in l for l in jd.lines), jd.lines
            assert any("Healed the nested server/data layout (1 files hoisted)" in l for l in jd.lines), jd.lines
            # (3) a second call is refused: already present -- force too (a present stack is never overwritten)
            for force in (False, True):
                try:
                    fetch_preflight("1.6", force)
                    raise AssertionError("a present stack must be refused")
                except AgentError as e:
                    assert e.status == 409 and "already on this server" in e.message, e.message
            # (4) a full run on a fresh target: downloaded, and present flips only AFTER the rename
            shutil.rmtree(stack)
            seen = []

            def spying_extract(archive, dest, job, should_stop=None, timeout=None):
                seen.append(is_present("1.6"))
                return fake_extract2(archive, dest, job, should_stop, timeout)

            g["extract_archive"] = spying_extract
            del fetched[:]
            res = do_fetch("1.6", _Job())
            assert res["downloaded"] is True and seen == [False] and is_present("1.6") and fetched == [cat["url"]], (res, seen)
            # (5) Stop mid-download: 409 "Stopped on request", the .part stays, nothing present
            shutil.rmtree(stack)

            def stopping_fetch(url, dst, timeout=60, cap=None, expect_md5=None, expect_size=None, resume=False,
                               should_stop=None, progress=None):
                fetched.append(url)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst + ".part", "wb") as f:
                    f.write(body[:100])
                _fetch_cancel.set()
                raise FetchStopped("stopped on request")

            g["_fetch_to_file"] = stopping_fetch
            try:
                do_fetch("1.6", _Job())
                raise AssertionError("a cancelled download must end in 409")
            except AgentError as e:
                assert e.status == 409 and e.message.startswith("Stopped on request") and "run it again" in e.message, e.message
            assert os.path.getsize(archive_path("1.6") + ".part") == 100 and not is_present("1.6")
            assert fetch_brief("1.6")["part"] == 100
            # (6) a checksum mismatch: 502 and nothing kept (as _fetch_to_file left it)
            os.remove(archive_path("1.6") + ".part")

            def bad_fetch(url, dst, **kw):
                fetched.append(url)
                raise ValueError("md5 x, expected y")

            g["_fetch_to_file"] = bad_fetch
            try:
                do_fetch("1.6", _Job())
                raise AssertionError("a checksum mismatch must end in 502")
            except AgentError as e:
                assert e.status == 502 and "Checksum mismatch" in e.message, e.message
            assert not os.path.exists(archive_path("1.6")) and not is_present("1.6")
            # (7) network cuts: retried with a doubling wait (5, 10 s ...), then the transfer completes
            cuts = [2]

            def cutting_fetch(url, dst, timeout=60, cap=None, expect_md5=None, expect_size=None, resume=False,
                              should_stop=None, progress=None):
                fetched.append(url)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if cuts[0]:
                    cuts[0] -= 1
                    with open(dst + ".part", "ab") as f:
                        f.write(b"x" * 10)
                    raise FetchCut("connection lost mid-transfer (timed out)")
                with open(dst + ".part", "wb") as f:
                    f.write(body)
                replace_file(dst + ".part", dst)
                return len(body)

            g["_fetch_to_file"], g["extract_archive"] = cutting_fetch, fake_extract2
            time.sleep = lambda _s: None  # the backoff waits are not worth real seconds here
            del fetched[:]
            jc = _Job()
            res = do_fetch("1.6", jc)
            assert res["downloaded"] is True and is_present("1.6") and len(fetched) == 3, (res, fetched)
            assert any("retrying in 5 s" in l for l in jc.lines) and any("retrying in 5 s (10 of" in l or "retrying in 5 s" in l for l in jc.lines), jc.lines
            assert sum("connection lost" in l for l in jc.lines) == 2, jc.lines
            # (8) a bundle-style HTTP 404 from the archive host: 502 at once, no retry loop
            shutil.rmtree(stack)
            del fetched[:]

            def gone_fetch(url, dst, **kw):
                fetched.append(url)
                raise urllib.error.HTTPError(url, 404, "NoSuchKey", {}, None)

            g["_fetch_to_file"] = gone_fetch
            try:
                do_fetch("1.6", _Job())
                raise AssertionError("a 404 must end in 502")
            except AgentError as e:
                assert e.status == 502 and "HTTP 404" in e.message and len(fetched) == 1, (e.message, fetched)
        finally:
            time.sleep = saved_sleep
            g.update(saved_f)
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_
            _fetch_cancel.clear()
    ok("server package download OK (brief never public, preflight refusals, failed extractor keeps the archive, "
       "pre-seeded archive adopted, present flips after the rename, stop keeps the .part, checksum 502, "
       "cut retries with backoff, 404 no retry)")

    # Prepare-the-server progress choice (agent 3.4) on fakes (no docker, no MySQL): default | keep |
    # fixes stages + record, the v2 marker (re-seed, migration, forget), needs_provision's keep/fixes
    # rule, do_setup's decisions, do_start's pathfinding re-assert, the pending-password gate, the
    # routes' 400s and /status + the public projection of a server without the default save.
    g = globals()
    faked_p = ("STATE_PATH", "PAYLOAD_DIR", "PROVISION_MODE", "TXT_FIXES_MODE", "TOKEN", "log_line", "read_manifest",
               "running_projects", "current_job", "down_all", "install_files", "mysql_exec", "mysql_import",
               "ensure_templates", "run_sql", "apply_events", "apply_tower_schedule", "tower_mark_applied",
               "apply_advertised_ip", "apply_hotpatch", "verify_stack", "ensure_exec_bits", "ensure_data_layout",
               "compose_binds_loopback", "_compose_stream", "wait_for_mysql", "ensure_muip_key",
               "set_account_password", "do_bootstrap", "do_provision", "apply_pathfinding", "reconcile_pathfinding")
    saved_p = {k: g[k] for k in faked_p}
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "1.6_live")
            os.makedirs(stack)
            for name in ("docker-compose.yml", ".bootstrap.lock"):
                open(os.path.join(stack, name), "w").close()
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            os.makedirs(os.path.join(td, "payloads", "1.6", "mysql"))
            with open(os.path.join(td, "payloads", "1.6", "mysql", "hk4e_db_user.sql"), "w") as f:
                f.write("-- dump\n")
            man = {"account": "aether", "password": "123123123",
                   "files": [{"src": "sdk/data/sdk.db", "dst": "sdk/data/sdk.db", "stage": "save"},
                             {"src": "redis/dump.rdb", "dst": "redis/dump.rdb", "stage": "save"},
                             {"src": "txt/MaterialDeleteData.txt", "dst": "server/data/txt/MaterialDeleteData.txt",
                              "stage": "save", "tsv": True},
                             {"src": "txt/NewActivityCondData.txt", "dst": "server/data/txt/NewActivityCondData.txt",
                              "stage": "txt_fix", "tsv": True}],
                   "mysql": [{"src": "mysql/hk4e_db_user.sql", "db": "hk4e_db_user", "recreate": True}],
                   "templates": []}
            calls = {"stages": [], "imports": [], "templates": 0, "pw": []}
            done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            sp = os.path.join(td, "state.json")

            def put(data):
                with open(sp, "wb") as f:
                    f.write(data)

            g.update({"STATE_PATH": sp, "PAYLOAD_DIR": os.path.join(td, "payloads"), "PROVISION_MODE": "once",
                      "TXT_FIXES_MODE": "now", "log_line": lambda *_a: None,
                      "read_manifest": lambda _v: man if _v == "1.6" else None,
                      "running_projects": lambda: ([], None), "current_job": lambda **_k: None,
                      "down_all": lambda *a, **k: None,
                      "install_files": lambda _v, _j, stages: calls["stages"].append(set(stages)) or len(stages),
                      "mysql_exec": lambda *a, **k: done,
                      "mysql_import": lambda _v, _src, db: calls["imports"].append(db) or done,
                      "ensure_templates": lambda _v, _j: calls.__setitem__("templates", calls["templates"] + 1) or {},
                      "run_sql": lambda *a, **k: None, "apply_events": lambda *a, **k: None,
                      "apply_tower_schedule": lambda *a, **k: None, "tower_mark_applied": lambda *a, **k: None,
                      "apply_advertised_ip": lambda *a, **k: False, "apply_hotpatch": lambda *a, **k: False,
                      "verify_stack": lambda *a, **k: None, "ensure_exec_bits": lambda *a, **k: 0,
                      "ensure_data_layout": lambda *a, **k: 0, "compose_binds_loopback": lambda _v: False,
                      "_compose_stream": lambda *a, **k: 0, "wait_for_mysql": lambda *a, **k: None,
                      "ensure_muip_key": lambda *a, **k: None,
                      "set_account_password": lambda _v, _j, name, _pw: calls["pw"].append(name),
                      "apply_pathfinding": lambda *a, **k: False, "reconcile_pathfinding": lambda *a, **k: False})
            assert parse_progress(None) == "default" and parse_progress("") == "default" and parse_progress(" Keep ") == "keep"
            for bad in ("nope", 1, True, ["keep"]):
                try:
                    parse_progress(bad)
                    raise AssertionError("parse_progress must refuse %r" % (bad,))
                except AgentError as e:
                    assert e.status == 400 and "progress must be one of default, keep, fixes" == e.message
            assert [effective_stage(i) for i in man["files"]] == ["save", "save", "config", "txt_fix"], \
                "a save+tsv entry counts as config"

            # (A) keep on a fresh stack: no save, no import, templates yes, no pre-made account anywhere
            put(b"{}")
            j = _Job()
            res = do_provision("1.6", j, progress="keep")
            assert res["provisioned"] and res["progress"] == "keep" and res["defaultAccount"] is False \
                and res["txtFixes"] == "applied", res
            assert calls["stages"][-1] == {"config", "txt_fix"} and calls["imports"] == [] and calls["templates"] == 1, calls
            vs = version_state("1.6")
            gen_keep = vs["provisionedAt"]
            assert gen_keep and vs["progress"] == "keep" and vs["defaultAccount"] is False, vs
            assert read_provisioned_marker("1.6") == {"at": gen_keep, "progress": "keep", "defaultAccount": False}
            assert any("No pre-made account" in l for l in j.lines) and not any("Game account" in l for l in j.lines), j.lines
            assert any("progress: keep" in l for l in j.lines), j.lines
            st16 = g["status"]()["versions"]["1.6"]
            assert st16["account"] is None and st16["progress"] == "keep" and st16["defaultAccount"] is False, st16
            assert build_public_status(g["status"](), default_policy())["versions"]["1.6"]["defaultAccount"] == ""
            assert needs_provision("1.6") is False, "a keep stack is never auto-imported"
            g["PROVISION_MODE"] = "switch"
            state_set(lastStarted="2.8")
            assert needs_provision("1.6") is False, "...not even by switch on a version switch"
            g["PROVISION_MODE"] = "once"
            g["do_provision"] = lambda *a, **k: (_ for _ in ()).throw(AssertionError("do_provision must not run"))
            res = do_start("1.6", _Job(), provision="auto")
            assert res["started"] and res["provisioned"] is False, res
            g["do_provision"] = saved_p["do_provision"]

            # (B) fixes over the keep stack: config + txt fixes only (txtFixes=later ignored), no templates,
            # the generation is kept
            version_state_set("1.6", provisionedAt="2026-08-01 10:00:00")
            calls["templates"] = 0
            res = do_provision("1.6", _Job(), progress="fixes", txt_fixes="later")
            assert res["progress"] == "fixes" and res["txtFixes"] == "applied" and res["defaultAccount"] is False, res
            assert calls["stages"][-1] == {"config", "txt_fix"} and calls["imports"] == [] and calls["templates"] == 0, calls
            assert version_state("1.6")["provisionedAt"] == "2026-08-01 10:00:00", "fixes keeps the generation"
            assert read_provisioned_marker("1.6")["progress"] == "fixes"

            # (C) default over the keep/fixes stack: the save + the import, a new generation, the account is back
            j = _Job()
            res = do_provision("1.6", j, progress="default")
            assert res == {"provisioned": True, "progress": "default", "defaultAccount": True, "txtFixes": "applied"}, res
            assert calls["stages"][-1] == {"save", "config", "txt_fix"} and calls["imports"] == ["hk4e_db_user"] \
                and calls["templates"] == 1, calls
            vs = version_state("1.6")
            assert vs["provisionedAt"] != "2026-08-01 10:00:00" and vs["progress"] == "default" and vs["defaultAccount"] is True, vs
            with open(provisioned_marker_path("1.6")) as f:
                assert f.read() == "%s\nprogress=default\ndefaultAccount=1\n" % vs["provisionedAt"]
            assert any("Game account: aether" in l for l in j.lines), j.lines
            st16 = g["status"]()["versions"]["1.6"]
            assert st16["account"] == "aether" and st16["progress"] == "default" and st16["defaultAccount"] is True, st16
            assert build_public_status(g["status"](), default_policy())["versions"]["1.6"]["defaultAccount"] == "aether"

            # (D) keep over a default stack: no import, generation + defaultAccount kept (the save is still there)
            version_state_set("1.6", provisionedAt="2026-08-01 10:00:00")
            del calls["imports"][:]
            res = do_provision("1.6", _Job(), progress="keep")
            assert res["defaultAccount"] is True and calls["imports"] == [], res
            assert version_state("1.6")["provisionedAt"] == "2026-08-01 10:00:00", "keep keeps the generation"
            assert read_provisioned_marker("1.6") == {"at": "2026-08-01 10:00:00", "progress": "keep", "defaultAccount": True}
            assert g["status"]()["versions"]["1.6"]["account"] == "aether", "the default save is still in the database"

            # (E) the config stage is never reverted (the real revert_files over .orig files)
            txt_dir = os.path.join(stack, "server", "data", "txt")
            os.makedirs(txt_dir)
            for name in ("MaterialDeleteData.txt", "NewActivityCondData.txt"):
                write_text(os.path.join(txt_dir, name), "agent\n")
                write_text(os.path.join(txt_dir, name + ".orig"), "vendor\n")
            assert revert_files("1.6", _Job(), {"txt_fix"}) == 1
            assert read_text(os.path.join(txt_dir, "MaterialDeleteData.txt")) == "agent\n", "config is never reverted"
            assert read_text(os.path.join(txt_dir, "NewActivityCondData.txt")) == "vendor\n"

            # (F) the marker re-seeds the WHOLE record: a v2 keep marker keeps the stack out of auto-import
            put(b"{}")
            _write_stack_text(provisioned_marker_path("1.6"), "2026-08-14 21:03:11\nprogress=keep\ndefaultAccount=0\n")
            j = _Job()
            assert needs_provision("1.6", j) is False and any("progress keep" in l for l in j.lines), j.lines
            vs = version_state("1.6")
            assert vs["provisionedAt"] == "2026-08-14 21:03:11" and vs["progress"] == "keep" and vs["defaultAccount"] is False, vs
            assert g["status"]()["versions"]["1.6"]["account"] is None
            put(b"{}")
            _write_stack_text(provisioned_marker_path("1.6"), "2026-08-14 21:03:11\n")
            assert read_provisioned_marker("1.6") == {"at": "2026-08-14 21:03:11", "progress": "default", "defaultAccount": True}, \
                "a one-line marker of an older agent reads as the default save"
            assert needs_provision("1.6") is False and version_state("1.6")["progress"] == "default" \
                and version_state("1.6")["defaultAccount"] is True
            put(b'{"versions": {"1.6": {"provisionedAt": "2026-08-14 21:03:11"}}}')
            _write_stack_text(provisioned_marker_path("1.6"), "2026-08-14 21:03:11\nprogress=fixes\ndefaultAccount=0\n")
            st16 = g["status"]()["versions"]["1.6"]
            assert st16["progress"] == "fixes" and st16["defaultAccount"] is False and st16["account"] is None, \
                "status reads the marker's record when the state lacks it"

            # (G) migration at start: a record without progress is back-filled, a one-line marker rewritten, once
            os.remove(provisioned_marker_path("1.6"))
            put(b'{"versions": {"1.6": {"provisionedAt": "2026-08-01 10:00:00"}}}')
            assert migrate_provisioned_markers() == 1
            vs = version_state("1.6")
            assert vs["progress"] == "default" and vs["defaultAccount"] is True, vs
            with open(provisioned_marker_path("1.6")) as f:
                assert f.read() == "2026-08-01 10:00:00\nprogress=default\ndefaultAccount=1\n"
            assert migrate_provisioned_markers() == 0
            _write_stack_text(provisioned_marker_path("1.6"), "2026-08-01 10:00:00\n")
            version_state_set("1.6", progress="keep", defaultAccount=False)
            assert migrate_provisioned_markers() == 1
            assert read_provisioned_marker("1.6") == {"at": "2026-08-01 10:00:00", "progress": "keep", "defaultAccount": False}
            assert migrate_provisioned_markers() == 0

            # (H) a fresh bootstrap forgets the record: progress gone, defaultAccount False
            forget_provisioned("1.6", _Job())
            vs = version_state("1.6")
            assert not vs.get("provisionedAt") and vs.get("progress") is None and vs["defaultAccount"] is False, vs
            assert read_provisioned_marker("1.6") is None

            # (I) the pending password is dropped, not retried forever, on a stack without the account
            version_state_set("1.6", passwordPending=True, defaultAccount=False)
            j = _Job()
            apply_pending_password("1.6", j)
            assert version_state("1.6")["passwordPending"] is False and calls["pw"] == [] \
                and any("No pre-made account" in l for l in j.lines), (j.lines, calls["pw"])
            version_state_set("1.6", passwordPending=True, defaultAccount=True)
            apply_pending_password("1.6", _Job())
            assert calls["pw"] == ["aether"] and version_state("1.6")["passwordPending"] is False

            # (J) do_setup: the bootstrap outcome + the record decide what do_provision is asked for
            prov_calls = []
            g["do_provision"] = lambda _v, _j, **kw: prov_calls.append(kw) or dict(kw, provisioned=True)
            put(b"{}")
            g["do_bootstrap"] = lambda _v, _j, force=False: "fresh"
            do_setup("1.6", _Job(), progress="keep")
            assert prov_calls[-1] == {"txt_fixes": None, "fresh": True, "progress": "keep"}, prov_calls
            g["do_bootstrap"] = lambda _v, _j, force=False: None
            version_state_set("1.6", provisionedAt="2026-08-01 10:00:00", progress="default", defaultAccount=True)
            assert do_setup("1.6", _Job()) == {"provisioned": False, "alreadyReady": True}, "default on a ready stack: nothing redone"
            do_setup("1.6", _Job(), progress="fixes")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "fixes"}, "keep/fixes on a ready stack: a non-destructive re-run"
            g["do_bootstrap"] = lambda _v, _j, force=False: "healed"
            do_setup("1.6", _Job(), progress="default")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "default", "keep_db": True}, "healed default over default keeps the database"
            version_state_set("1.6", progress="keep", defaultAccount=False)
            do_setup("1.6", _Job(), progress="default")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "default", "keep_db": False}, "healed default over keep imports"
            do_setup("1.6", _Job(), progress="keep")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "keep", "keep_db": False}
            # (J2) THE data-loss case: the state record is gone (a restored .bak, a reset, a wipe)
            # but the stack's own marker still says default/1. A heal must read the MARKER-merged
            # record -- reading state alone made "Prepare the server" re-import the save over a live
            # player database. needs_provision re-seeds the state from the marker first, so the
            # decision must be taken after that, never from a value captured before it.
            put(b"{}")
            write_provisioned_marker("1.6", "2026-08-01 10:00:00", "default", True)
            do_setup("1.6", _Job(), progress="default")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "default", "keep_db": True}, \
                "healed default with the record only in the marker MUST keep the database: %s" % prov_calls[-1]
            # the same for a keep marker: the admin's "default" then really does import
            put(b"{}")
            write_provisioned_marker("1.6", "2026-08-01 10:00:00", "keep", False)
            do_setup("1.6", _Job(), progress="default")
            assert prov_calls[-1] == {"txt_fixes": None, "progress": "default", "keep_db": False}, prov_calls[-1]
            # and provision_record itself: state wins where it has a value, the marker fills the rest
            put(b"{}")
            write_provisioned_marker("1.6", "2026-08-01 10:00:00", "default", True)
            rec = provision_record("1.6")
            assert rec["provisionedAt"] == "2026-08-01 10:00:00" and rec["progress"] == "default" \
                and rec["defaultAccount"] is True, rec
            version_state_set("1.6", progress="keep", defaultAccount=False)
            rec = provision_record("1.6")
            assert rec["progress"] == "keep" and rec["defaultAccount"] is False, rec
            os.remove(provisioned_marker_path("1.6"))
            g["do_provision"] = saved_p["do_provision"]

            # (K) do_start re-asserts the stored pathfinding decision before the staged up; an already-up
            # stack being disabled is reconciled, a down one is covered by the up -d that follows
            pf_calls = []
            g["apply_pathfinding"] = lambda _v, _j, on: pf_calls.append(("apply", on)) or True
            g["reconcile_pathfinding"] = lambda _v, _j, on: pf_calls.append(("reconcile", on)) or True
            version_state_set("1.6", pathfinding=False, provisionedAt="2026-08-01 10:00:00", progress="keep")
            g["running_projects"] = lambda: ([VERSIONS["1.6"]["project"]], None)
            do_start("1.6", _Job(), provision="none")
            assert pf_calls == [("apply", False), ("reconcile", False)], pf_calls
            del pf_calls[:]
            g["running_projects"] = lambda: ([], None)
            do_start("1.6", _Job(), provision="none")
            assert pf_calls == [("apply", False)], pf_calls
            del pf_calls[:]
            version_state_set("1.6", pathfinding=True)
            g["running_projects"] = lambda: ([VERSIONS["1.6"]["project"]], None)
            do_start("1.6", _Job(), provision="none")
            assert pf_calls == [("apply", True)], "enable on an up stack: the up -d below starts it"

            # (L) the routes: a bad progress / pathfinding / enabled is a 400 before any job exists
            g["TOKEN"] = "selftest-token"
            reply = []
            hh = Handler.__new__(Handler)
            hh._send = lambda status_, obj, retry_after=None: reply.append((status_, obj))
            for path, body, text in (("/server/setup", b'{"version": "1.6", "progress": "nope"}', "progress must be one of"),
                                     ("/server/provision", b'{"version": "1.6", "progress": "all"}', "progress must be one of"),
                                     ("/server/setup", b'{"version": "1.6", "pathfinding": "yes"}', "pathfinding must be"),
                                     ("/server/pathfinding", b'{"version": "1.6", "enabled": 1}', "enabled must be")):
                del reply[:]
                hh.path, hh.rfile = path, io.BytesIO(body)
                hh.headers = {"Authorization": "Bearer selftest-token", "Content-Length": str(len(body))}
                hh.do_POST()
                assert reply and reply[0][0] == 400 and text in reply[0][1]["error"], (path, reply)
        finally:
            g.update(saved_p)
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_
    ok("provisioning progress OK (default/keep/fixes stages + record, marker v2 re-seed/migration/forget, "
       "never auto-imported, setup decisions incl. the marker-merged heal, pending-password gate, "
       "route 400s, account hidden without the save)")

    # Pathfinding server on/off (agent 3.4): the profile edit in the vendor's syntax (CRLF and LF,
    # idempotent, round-trip byte-exact, oaserver untouched, the inline form read), then the job on
    # fakes: both files + state, the compose argv on a running stack, a failing compose, the
    # dead_services filter and the Prepare-time decision applied before the bootstrap renders.
    tmpl_crlf = ("version: '3'\r\nservices:\r\n  mysql:\r\n    image: mariadb\r\n    ports:\r\n"
                 "      - \"%OUTER_IP%:3306:3306\"\r\n"
                 "  oaserver:\r\n    profiles:\r\n      - donotstart\r\n    image: debian:11\r\n"
                 "  pathfindingserver:\r\n    image: debian:11\r\n    restart: on-failure\r\n    expose:\r\n"
                 "      - \"21101/udp\"\r\n    networks:\r\n      default:\r\n        ipv4_address: 172.10.3.8\r\n"
                 "    command: ./pathfindingserver\r\n"
                 "  sdk:\r\n    image: sdk\r\nnetworks:\r\n  default:\r\n    driver: bridge\r\n")
    off, n = set_pathfinding_text(tmpl_crlf, False)
    assert n == 1 and "  pathfindingserver:\r\n    profiles:\r\n      - donotstart\r\n    image: debian:11\r\n" in off, off
    assert off.count("\r\n") == tmpl_crlf.count("\r\n") + 2 and "\n" not in off.replace("\r\n", ""), "CRLF kept"
    assert off.count("- donotstart") == 2 and off.replace("  pathfindingserver:\r\n    profiles:\r\n      - donotstart\r\n",
                                                          "  pathfindingserver:\r\n") == tmpl_crlf, "only that block changed"
    assert set_pathfinding_text(off, False) == (off, 0), "idempotent"
    on, n = set_pathfinding_text(off, True)
    assert n == 1 and on == tmpl_crlf, "disable -> enable round-trips byte-exact"
    assert set_pathfinding_text(tmpl_crlf, True) == (tmpl_crlf, 0)
    assert compose_service_profiled(tmpl_crlf, "pathfindingserver") is False and compose_service_profiled(off, "pathfindingserver") is True
    assert compose_service_profiled(tmpl_crlf, "oaserver") is True and compose_service_profiled(tmpl_crlf, "mysql") is False
    assert compose_service_profiled(tmpl_crlf, "gameserver") is None
    assert compose_disabled_services(tmpl_crlf) == {"oaserver"} and compose_disabled_services(off) == {"oaserver", "pathfindingserver"}
    tmpl_lf = tmpl_crlf.replace("\r\n", "\n")
    off_lf, n = set_pathfinding_text(tmpl_lf, False)
    assert n == 1 and "\r" not in off_lf and "    profiles:\n      - donotstart\n" in off_lf
    assert set_pathfinding_text(off_lf, True) == (tmpl_lf, 1)
    assert set_pathfinding_text("services:\n  mysql:\n    image: x\n", False) == ("services:\n  mysql:\n    image: x\n", 0)
    assert compose_service_profiled("services:\n  mysql:\n    image: x\n", "pathfindingserver") is None
    inline = "services:\n  pathfindingserver:\n    profiles: [donotstart]\n    image: x\n"
    assert compose_service_profiled(inline, "pathfindingserver") is True and compose_disabled_services(inline) == {"pathfindingserver"}
    assert set_pathfinding_text(inline, True) == ("services:\n  pathfindingserver:\n    image: x\n", 1)
    assert set_pathfinding_text("services:\n  pathfindingserver:\n    profiles: [debug]\n", False)[0] == \
        "services:\n  pathfindingserver:\n    profiles: [debug, donotstart]\n"
    # a same-named key nested in another service (depends_on long form) never passes for the service
    nested = ("services:\n  gameserver:\n    depends_on:\n      pathfindingserver:\n        condition: service_started\n"
              "  pathfindingserver:\n    image: x\n")
    off_n, n = set_pathfinding_text(nested, False)
    assert n == 1 and off_n == nested.replace("  pathfindingserver:\n    image: x\n",
                                              "  pathfindingserver:\n    profiles:\n      - donotstart\n    image: x\n"), off_n
    faked_pf = ("STATE_PATH", "running_projects", "_compose_stream", "_compose", "verify_stack", "log_line",
                "current_job", "PATHFINDING_DEFAULT", "do_bootstrap", "do_provision", "ensure_data_layout")
    saved_pf = {k: g[k] for k in faked_pf}
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "1.6_live")
            os.makedirs(stack)
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            tmpl_path, rendered = _compose_paths("1.6")
            write_text(os.path.join(stack, ".env"), "OUTER_IP=192.0.2.10\n")
            write_text(tmpl_path, tmpl_crlf)
            argv = []
            g.update({"STATE_PATH": os.path.join(td, "state.json"), "running_projects": lambda: ([], None),
                      "_compose_stream": lambda _d, _j, *a, **k: argv.append(a) or 0,
                      "verify_stack": lambda *a, **k: argv.append(("verify",)),
                      "log_line": lambda *_a: None, "current_job": lambda **_k: None})
            assert pathfinding_enabled("1.6") is True, "the template alone, before the bootstrap"
            render_stack_configs("1.6", _Job())
            rendered_vendor = tmpl_crlf.replace("%OUTER_IP%", "192.0.2.10")
            assert read_text(rendered) == rendered_vendor and pathfinding_enabled("1.6") is True
            # (a) stack not running: both files edited, state persisted, no compose call
            res = do_pathfinding("1.6", _Job(), False)
            assert res == {"enabled": False, "changed": True, "applied": False} and argv == [], (res, argv)
            assert pathfinding_enabled("1.6") is False and version_state("1.6")["pathfinding"] is False
            assert compose_service_profiled(read_text(tmpl_path), "pathfindingserver") is True
            assert read_text(rendered) == set_pathfinding_text(rendered_vendor, False)[0], "rendered: same edit, CRLF kept"
            # the rendered file is the truth for status when the two disagree (a hand edit on the box)
            write_text(tmpl_path, tmpl_crlf)
            assert pathfinding_enabled("1.6") is False
            assert do_pathfinding("1.6", _Job(), False)["changed"] is True, "the template is brought back in line"
            assert do_pathfinding("1.6", _Job(), False)["changed"] is False
            # (b) running + bootstrapped: enable = up --no-deps + the stability check; disable = rm through the profile
            open(os.path.join(stack, ".bootstrap.lock"), "w").close()
            g["running_projects"] = lambda: ([VERSIONS["1.6"]["project"]], None)
            res = do_pathfinding("1.6", _Job(), True)
            assert res == {"enabled": True, "changed": True, "applied": True}, res
            assert argv == [("up", "-d", "--no-deps", "pathfindingserver"), ("verify",)], argv
            assert read_text(rendered) == rendered_vendor and read_text(tmpl_path) == tmpl_crlf, "round-trip on disk"
            del argv[:]
            res = do_pathfinding("1.6", _Job(), False)
            assert res["applied"] is True and argv == [("--profile", "donotstart", "rm", "-s", "-f", "pathfindingserver")], argv
            # (c) a failing compose: 500, the files stay edited and the decision is on record
            g["_compose_stream"] = lambda *a, **k: 1
            try:
                do_pathfinding("1.6", _Job(), True)
                raise AssertionError("a failed up must be a 500")
            except AgentError as e:
                assert e.status == 500 and "pathfindingserver" in e.message, e.message
            assert pathfinding_enabled("1.6") is True and version_state("1.6")["pathfinding"] is True
            # docker mute: the files change, the docker half waits for the next start
            g["_compose_stream"] = lambda _d, _j, *a, **k: argv.append(a) or 0
            g["running_projects"] = lambda: (None, "docker unavailable (selftest)")
            del argv[:]
            j = _Job()
            assert do_pathfinding("1.6", j, False) == {"enabled": False, "changed": True, "applied": False} and argv == []
            assert any("next start" in l for l in j.lines), j.lines
            g["running_projects"] = lambda: ([VERSIONS["1.6"]["project"]], None)
            # (d) dead_services subtracts the profiled services even when `config --services` lists them,
            # and a stale container of a switched-off service (the ps fallback) is not "dead" either
            ps_json = "\n".join(json.dumps({"Service": s, "State": "running"}) for s in ("mysql", "sdk"))
            g["_compose"] = lambda _d, *a, **k: subprocess.CompletedProcess(
                [], 0, stdout=(ps_json if a[0] == "ps" else "mysql\noaserver\npathfindingserver\nsdk\n"), stderr="")
            dead, states = dead_services("1.6")
            assert dead == [] and states == {"mysql": "running", "sdk": "running"}, (dead, states)
            stale = ps_json + "\n" + json.dumps({"Service": "pathfindingserver", "State": "exited"})
            g["_compose"] = lambda _d, *a, **k: subprocess.CompletedProcess(
                [], 0 if a[0] == "ps" else 1, stdout=(stale if a[0] == "ps" else ""), stderr="")
            assert dead_services("1.6")[0] == [], "a leftover container of a profiled service is never revived"
            # (e) Prepare server: the request's choice, else the stored one, else GIO_PATHFINDING only for a
            # stack never bootstrapped -- applied to the template BEFORE the bootstrap renders it
            g["PATHFINDING_DEFAULT"] = False
            assert _pathfinding_decision(None, None, False) is False and _pathfinding_decision(True, None, False) is True
            assert _pathfinding_decision(None, True, False) is True and _pathfinding_decision(None, None, True) is None
            assert _pathfinding_decision(False, True, True) is False
            seen = {}

            def fake_bootstrap(v, jb, force=False):
                seen["tmpl_profiled"] = compose_service_profiled(read_text(tmpl_path), "pathfindingserver")
                render_stack_configs(v, jb)
                open(os.path.join(stack, ".bootstrap.lock"), "w").close()
                return "fresh"

            def fresh_files():
                for p in (os.path.join(stack, ".bootstrap.lock"), rendered):
                    if os.path.exists(p):
                        os.remove(p)
                write_text(tmpl_path, tmpl_crlf)
                with open(g["STATE_PATH"], "wb") as f:
                    f.write(b"{}")

            g.update({"do_bootstrap": fake_bootstrap, "do_provision": lambda _v, _j, **kw: {"provisioned": True},
                      "ensure_data_layout": lambda *a, **k: 0})
            fresh_files()
            do_setup("1.6", _Job())
            assert seen["tmpl_profiled"] is True and pathfinding_enabled("1.6") is False \
                and version_state("1.6")["pathfinding"] is False, "the default (off) reached the template before the render"
            fresh_files()
            do_setup("1.6", _Job(), pathfinding=True)
            assert seen["tmpl_profiled"] is False and pathfinding_enabled("1.6") is True \
                and version_state("1.6")["pathfinding"] is True, "an explicit true wins over the default"
            os.remove(os.path.join(stack, ".bootstrap.lock"))
            os.remove(rendered)
            write_text(tmpl_path, tmpl_crlf)
            do_setup("1.6", _Job())
            assert seen["tmpl_profiled"] is False and version_state("1.6")["pathfinding"] is True, \
                "no choice: the stored decision, never the default, on a stack that decided before"
        finally:
            g.update(saved_pf)
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_
    ok("pathfinding profile edit OK (vendor syntax, CRLF/LF, idempotent, round-trip, oaserver untouched, "
       "dead_services filter, job argv, setup decision before the render)")

    # The MUIP sign key (agent 3.4): a stack still on the archive's published key gets GIO_MUIP_KEY or a
    # random one (template + rendered xml + creds.txt + state), a private key is never touched, only a
    # fingerprint is ever logged or returned, and a template without sign_key cannot be prepared.
    faked_m = ("STATE_PATH", "MUIP_KEY", "log_line", "running_projects", "current_job")
    saved_m = {k: g[k] for k in faked_m}
    saved_vdirs = {v: (VERSIONS[v]["dir"], VERSIONS[v]["project"]) for v in VERSIONS}
    with tempfile.TemporaryDirectory() as td:
        try:
            stack = os.path.join(td, "1.6_live")
            conf = os.path.join(stack, "server", "muipserver", "conf")
            os.makedirs(conf)
            VERSIONS["1.6"]["dir"], VERSIONS["1.6"]["project"] = stack, _project_name(stack)
            VERSIONS["2.8"]["dir"], VERSIONS["2.8"]["project"] = "", ""
            tmpl_x, rendered_x = os.path.join(conf, "muipserver.xml.tmpl"), os.path.join(conf, "muipserver.xml")
            vendor = '<Root>\r\n  <ApiConf sign_key="8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ" />\r\n</Root>\r\n'
            write_text(tmpl_x, vendor)
            write_text(rendered_x, vendor)
            write_text(os.path.join(stack, ".env"), "MYSQL_ROOT_PASSWORD=RootPw_1234\nFLASK_SECRET_KEY=fl\nOUTER_IP=192.0.2.10\n")
            write_text(os.path.join(stack, "docker-compose.yml.tmpl"), "cmd: redis-server --requirepass Internal_1234\n")
            write_text(os.path.join(stack, "creds.txt"), "MUIP_KEY: 8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ\n")
            g.update({"STATE_PATH": os.path.join(td, "state.json"), "MUIP_KEY": "", "log_line": lambda *_a: None,
                      "running_projects": lambda: ([], None), "current_job": lambda **_k: None})
            assert is_vendor_muip_key("") and all(is_vendor_muip_key(k) for k in VENDOR_MUIP_KEYS) \
                and not is_vendor_muip_key("Chosen_Key_12345")
            assert random_muip_key() != random_muip_key() and SECRET_VALUE_RE.match(random_muip_key())
            assert muip_fingerprint("") is None and muip_fingerprint("abc") == hashlib.sha256(b"abc").hexdigest()[:8]
            j = _Job()
            assert ensure_muip_key("1.6", j) == "random"
            new = _read_sign_key_file(tmpl_x)
            assert len(new) == 36 and SECRET_VALUE_RE.match(new) and new not in VENDOR_MUIP_KEYS, new
            assert _read_sign_key_file(rendered_x) == new, "the rendered xml follows the template"
            assert read_text(tmpl_x) == vendor.replace("8JTdsghuAythdHFtjkasiuHbxdjjayYfsvaJ", new), "only the key, CRLF kept"
            creds = read_text(os.path.join(stack, "creds.txt"))
            assert "MUIP_KEY: %s" % new in creds and "mysql/root: RootPw_1234" in creds and "h4ke pass: Internal_1234" in creds, creds
            sec = version_state("1.6")["secrets"]
            assert sec["muipSource"] == "random" and sec["muipChangedAt"], sec
            assert len(j.lines) == 1 and new not in j.lines[0] and muip_fingerprint(new) in j.lines[0], "fingerprint only"
            assert ensure_muip_key("1.6", _Job()) is None and _read_sign_key_file(tmpl_x) == new, "a private key is never touched"
            write_text(tmpl_x, vendor)
            g["MUIP_KEY"] = "Chosen_Key_12345"
            j = _Job()
            assert ensure_muip_key("1.6", j) == "config"
            assert _read_sign_key_file(tmpl_x) == "Chosen_Key_12345" and _read_sign_key_file(rendered_x) == "Chosen_Key_12345"
            assert version_state("1.6")["secrets"]["muipSource"] == "config" and "Chosen_Key_12345" not in j.lines[0]
            write_text(tmpl_x, '<ApiConf sign_key="AdminRotated_0001" />\n')
            assert ensure_muip_key("1.6", _Job()) is None, "a key the admin rotated to is not replaced by the configured one"
            info = secrets_info("1.6")
            assert info["muip"]["vendorDefault"] is False and info["muip"]["source"] == "config" \
                and info["muip"]["fingerprint"] == hashlib.sha256(b"AdminRotated_0001").hexdigest()[:8], info["muip"]
            assert "AdminRotated_0001" not in json.dumps(info) and "Chosen_Key" not in json.dumps(info)
            write_text(tmpl_x, vendor)
            assert secrets_info("1.6")["muip"]["vendorDefault"] is True
            open(os.path.join(stack, "docker-compose.yml"), "w").close()  # present
            st16 = g["status"]()["versions"]["1.6"]
            assert st16["muipKey"] == "vendor" and st16["muipChangedAt"] == version_state("1.6")["secrets"]["muipChangedAt"], st16
            write_text(tmpl_x, '<ApiConf sign_key="AdminRotated_0001" />\n')
            full = g["status"]()
            assert full["versions"]["1.6"]["muipKey"] == "custom" and full["versions"]["2.8"]["muipKey"] is None
            assert "AdminRotated_0001" not in json.dumps(full) and "8JTdsghu" not in json.dumps(full)
            assert "muipKey" not in json.dumps(build_public_status(full, default_policy()))
            assert isinstance(full["pathfindingDefault"], bool) and "pathfinding" in full["versions"]["1.6"]
            write_text(tmpl_x, "<ApiConf />\n")
            try:
                ensure_muip_key("1.6", _Job())
                raise AssertionError("a template without sign_key must be a 500")
            except AgentError as e:
                assert e.status == 500 and "muipserver.xml.tmpl" in e.message, e.message
            # the import-time rule for GIO_MUIP_KEY is SECRET_VALUE_RE itself
            assert not SECRET_VALUE_RE.match("short1") and not SECRET_VALUE_RE.match("has space 12345") \
                and SECRET_VALUE_RE.match("x" * 128) and not SECRET_VALUE_RE.match("x" * 129)
        finally:
            g.update(saved_m)
            for v, (d_, p_) in saved_vdirs.items():
                VERSIONS[v]["dir"], VERSIONS[v]["project"] = d_, p_
    ok("MUIP sign key OK (vendor key replaced with random/config, private key untouched, creds.txt + state, "
       "fingerprint only, 500 without sign_key, never public)")

    # Policy defaults (agent 3.4) through the real state path: a state without a policy gets signup +
    # player commands ON, an admin's stored OFF survives, set_policy persists the whole object.
    saved_pol = {k: g[k] for k in ("STATE_PATH", "log_line")}
    with tempfile.TemporaryDirectory() as td:
        try:
            g.update({"STATE_PATH": os.path.join(td, "state.json"), "log_line": lambda *_a: None})
            with open(g["STATE_PATH"], "wb") as f:
                f.write(json.dumps({"policy": {"signup": {"enabled": False}, "playerCommands": False}}).encode())
            pol = get_policy()
            assert pol["signup"]["enabled"] is False and pol["playerCommands"] is False, "an admin's stored OFF survives the new defaults"
            with open(g["STATE_PATH"], "wb") as f:
                f.write(b'{"lastStarted": "1.6"}')
            pol = get_policy()
            assert pol["signup"]["enabled"] and pol["playerCommands"], "a state without a policy key gets the new defaults"
            set_policy({"playerCommands": False})
            stored = load_state()["policy"]
            assert stored["signup"]["enabled"] is True and stored["playerCommands"] is False \
                and stored["hotpatchUpstream"] is True, stored
            assert get_policy()["playerCommands"] is False
            # A stored policy that no longer validates (the payloads were redeployed without the
            # template id it names, or the box has no payload folder at all) must NOT fall back to
            # the defaults: those now say signup + player commands ON, i.e. the admin's "off" would
            # silently reopen the server. The template list is dropped, every other stored choice
            # stays, and a policy that cannot be applied at all fails CLOSED.
            saved_mt = g["manifest_templates"]
            try:
                g["manifest_templates"] = lambda v, man=None: []          # no template exists anywhere
                with open(g["STATE_PATH"], "wb") as f:
                    f.write(json.dumps({"policy": {
                        "signup": {"enabled": False, "versions": {"1.6": {"templates": ["fresh", "post-gaa"],
                                                                          "maxPerDay": 7}}},
                        "playerCommands": False, "hotpatchUpstream": False}}).encode())
                pol = get_policy()
                assert pol["signup"]["enabled"] is False and pol["playerCommands"] is False, \
                    "an unvalidatable template list must not re-enable signup/commands: %s" % pol
                assert pol["signup"]["versions"]["1.6"]["maxPerDay"] == 7, \
                    "the rest of the stored policy is kept: %s" % pol
                assert pol["hotpatchUpstream"] is False, pol
                # ... and the same stored policy with signup ON keeps it ON (the retry is not a veto)
                with open(g["STATE_PATH"], "wb") as f:
                    f.write(json.dumps({"policy": {
                        "signup": {"enabled": True, "versions": {"1.6": {"templates": ["post-gaa"]}}},
                        "playerCommands": True}}).encode())
                assert get_policy()["signup"]["enabled"] is True
            finally:
                g["manifest_templates"] = saved_mt
        finally:
            g.update(saved_pol)
    ok("policy defaults OK (on by default, stored OFF survives, set_policy persists the full object, "
       "an unvalidatable stored policy never reopens signup/commands)")

    # TLS (agent 3.6): full verification minus VERIFY_X509_STRICT; a certificate failure is its own error
    # -- never 30 "connection lost" retries (27 minutes) nor a "checksum mismatch" -- and only a download
    # whose md5 + size are pinned may repeat over the unverified fallback, which the md5 still judges.
    g = globals()
    faked_t = ("log_line", "_open_pinned_fallback", "TLS_PINNED_FALLBACK", "_fetch_to_file", "HOTPATCH_DIR",
               "HOTPATCH_BUNDLE_DIR", "CA_FILE", "_TLS_CONTEXT", "_TLS_CA_LOADED", "_TLS_CA_ERROR")
    saved_t = {k: g[k] for k in faked_t}
    saved_open_t, saved_sleep_t = urllib.request.urlopen, time.sleep
    saved_opener_t = getattr(urllib.request, "_opener", None)  # install_http_opener() replaces it below
    saved_hosts_t = set(_TLS_FALLBACK_HOSTS)
    if ssl is None:
        ok("TLS checks skipped (this Python has no ssl module)")
    else:
        tls_logs = []
        with tempfile.TemporaryDirectory() as td:
            try:
                g["log_line"] = tls_logs.append
                strict = int(getattr(ssl, "VERIFY_X509_STRICT", 0))
                ctx = build_tls_context()
                assert not int(ctx.verify_flags) & strict, "STRICT must be cleared: %r" % ctx.verify_flags
                assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True, "full verification kept"
                for keep in ("VERIFY_X509_PARTIAL_CHAIN", "VERIFY_X509_TRUSTED_FIRST"):
                    bit = int(getattr(ssl, keep, 0))
                    if bit and int(ssl.create_default_context().verify_flags) & bit:
                        assert int(ctx.verify_flags) & bit, "%s is kept" % keep
                bad_pem = os.path.join(td, "bad.pem")
                with open(bad_pem, "w") as f:
                    f.write("-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n")
                ctx = build_tls_context(bad_pem)
                assert ctx.verify_mode == ssl.CERT_REQUIRED and not int(ctx.verify_flags) & strict
                assert any("GIO_CA_FILE" in l and "ignored" in l for l in tls_logs), tls_logs
                assert _ca_file_problem(bad_pem) and _ca_file_problem(os.path.join(td, "missing.pem"))
                # the opener records whether GIO_CA_FILE really loaded: GET /agent/config reports tls.caFile only
                # for roots the context carries, and tls.caFileError when a (hand-set) file did not load
                for ca_, want_loaded, want_err in ((bad_pem, "", True), (os.path.join(td, "gone.pem"), "", True),
                                                   ("", "", False)):
                    g["CA_FILE"] = ca_
                    install_http_opener()
                    assert _TLS_CA_LOADED == want_loaded and bool(_TLS_CA_ERROR) is want_err, (ca_, _TLS_CA_LOADED,
                                                                                               _TLS_CA_ERROR)
                    assert _TLS_CONTEXT is not None and _TLS_CONTEXT.verify_mode == ssl.CERT_REQUIRED
                good_pem = None
                if hasattr(ssl, "enum_certificates"):
                    for der, enc, _trust in ssl.enum_certificates("ROOT"):
                        if enc == "x509_asn":
                            good_pem = os.path.join(td, "root.pem")
                            write_text(good_pem, ssl.DER_cert_to_PEM_cert(der))
                            break
                else:
                    vp = ssl.get_default_verify_paths()
                    good_pem = next((p for p in (vp.cafile, vp.openssl_cafile) if p and os.path.isfile(p)), None)
                if good_pem and not _ca_file_problem(good_pem):
                    g["CA_FILE"] = good_pem
                    install_http_opener()
                    assert _TLS_CA_LOADED == good_pem and _TLS_CA_ERROR is None, (_TLS_CA_LOADED, _TLS_CA_ERROR)
                urllib.request.install_opener(saved_opener_t)
                g["CA_FILE"] = saved_t["CA_FILE"]
                # the certificate failure behind urllib's URLError, however deep; nothing else counts
                cert = ssl.SSLCertVerificationError(1, "certificate verify failed: Basic Constraints of CA cert "
                                                       "not marked critical")
                wrapped = urllib.error.URLError(cert)
                assert _tls_failure(wrapped) is cert and _tls_failure(cert) is cert
                try:
                    try:
                        raise wrapped
                    except urllib.error.URLError as e:
                        raise RuntimeError("outer") from e
                except RuntimeError as e:
                    assert _tls_failure(e) is cert, "found through __cause__"
                assert _tls_failure(urllib.error.URLError("unreachable")) is None
                assert _tls_failure(urllib.error.HTTPError("https://x.invalid/", 404, "NoSuchKey", {}, None)) is None
                assert _tls_failure(socket.timeout("stalled")) is None
                tv = TlsVerifyError("archive.org", "Basic Constraints of CA cert not marked critical")
                assert isinstance(tv, OSError) and not isinstance(tv, ValueError), "no checksum branch may catch it"
                assert str(tv) == "the TLS certificate of archive.org could not be verified (Basic Constraints of " \
                                  "CA cert not marked critical)", str(tv)
                assert _short_err(tv).startswith("TLS certificate not verified: Basic Constraints"), _short_err(tv)
                assert _short_err(wrapped).startswith("TLS certificate not verified:") and \
                    "Basic Constraints" in _short_err(wrapped), _short_err(wrapped)
                assert _short_err(urllib.error.HTTPError("u", 404, "NoSuchKey", {}, None)) == "HTTP 404", \
                    "the /hotpatch/ route greps it"
                assert _short_err(urllib.error.URLError("unreachable")) == "unreachable"

                class TResp:
                    def __init__(self, chunks, status=200):
                        self.chunks, self.status, self.headers = list(chunks), status, {}

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def read(self, n):
                        return self.chunks.pop(0) if self.chunks else b""

                body = b"relic tls fallback bytes"
                md5 = hashlib.md5(body).hexdigest()
                asked = []

                def cert_fail(req, timeout=0):
                    asked.append(req.full_url)
                    raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

                via = []

                def fake_fallback(req, timeout):
                    via.append((req.full_url, timeout))
                    return TResp([body])

                urllib.request.urlopen = cert_fail
                g["_open_pinned_fallback"] = fake_fallback
                g["TLS_PINNED_FALLBACK"] = True
                _TLS_FALLBACK_HOSTS.discard("dl.example.com")
                del tls_logs[:]
                dst = os.path.join(td, "p", "pinned.bin")
                assert _fetch_to_file("https://dl.example.com/pinned.bin", dst, expect_md5=md5,
                                      expect_size=len(body)) == len(body)
                with open(dst, "rb") as f:
                    assert f.read() == body
                assert via == [("https://dl.example.com/pinned.bin", HOTPATCH_TIMEOUT)] and asked, (via, asked)
                assert "dl.example.com" in _TLS_FALLBACK_HOSTS
                assert _fetch_to_file("https://dl.example.com/pinned.bin", dst, expect_md5=md5,
                                      expect_size=len(body)) == len(body)
                assert sum("UNVERIFIED" in l for l in tls_logs) == 1, "one WARNING per host per process: %s" % tls_logs
                # recorded on the attempt, so the WARNING promises only what the md5 + size check decides
                assert any("only a file whose md5 + size match is kept" in l and "proves" not in l
                           for l in tls_logs if "UNVERIFIED" in l), tls_logs
                # the md5 is what proves a file that came over the unverified connection
                g["_open_pinned_fallback"] = lambda req, timeout: TResp([bytes(reversed(body))])
                try:
                    _fetch_to_file("https://dl.example.com/pinned.bin", os.path.join(td, "t", "x.bin"),
                                   expect_md5=md5, expect_size=len(body))
                    raise AssertionError("tampered bytes must fail the md5")
                except FetchRefused:
                    raise AssertionError("not a declared-length refusal")
                except ValueError as e:
                    assert "md5" in str(e), e
                assert not os.path.exists(os.path.join(td, "t")), "nothing of a failed pinned download stays"
                g["_open_pinned_fallback"] = fake_fallback
                # never for an unpinned request (an on-demand miss: no md5, or no size) nor with the switch off
                for kw in ({"cap": 1 << 20}, {"expect_md5": md5}, {"expect_size": len(body)}):
                    del via[:]
                    try:
                        _fetch_to_file("https://dl.example.com/x.bin", os.path.join(td, "u", "x.bin"), **kw)
                        raise AssertionError("an unpinned request must not fall back: %r" % kw)
                    except TlsVerifyError as e:
                        assert e.host == "dl.example.com" and "certificate verify failed" in e.detail, (e.host, e.detail)
                    assert not via and not os.path.exists(os.path.join(td, "u")), kw
                g["TLS_PINNED_FALLBACK"] = False
                try:
                    _fetch_to_file("https://dl.example.com/pinned.bin", os.path.join(td, "o", "x.bin"),
                                   expect_md5=md5, expect_size=len(body))
                    raise AssertionError("with GIO_TLS_PINNED_FALLBACK=0 a pinned download must fail too")
                except TlsVerifyError:
                    pass
                assert not via
                g["TLS_PINNED_FALLBACK"] = True
                # a network failure and an HTTP error reach the caller untouched
                urllib.request.urlopen = lambda req, timeout=0: (_ for _ in ()).throw(urllib.error.URLError("unreachable"))
                try:
                    _fetch_to_file("https://dl.example.com/pinned.bin", dst, expect_md5=md5, expect_size=len(body))
                    raise AssertionError("must raise")
                except TlsVerifyError:
                    raise AssertionError("a dead network is not a certificate failure")
                except urllib.error.URLError as e:
                    assert e.reason == "unreachable" and not via

                # the stack download: one call, 502 at once, the way out named -- no backoff, no retry lines
                fetched_t = []

                def tls_fetch(url, dst_, **kw):
                    fetched_t.append(url)
                    raise TlsVerifyError("archive.org", "certificate verify failed")

                def no_sleep(_s):
                    raise AssertionError("a certificate failure must not wait out a backoff")

                g["_fetch_to_file"] = tls_fetch
                time.sleep = no_sleep
                cat_t = {"version": "1.6", "url": "https://archive.org/download/test/1.6_live_gio.7z", "size": 10,
                         "md5": "0" * 32}
                jt = _Job()
                _fetch_cancel.clear()
                try:
                    _fetch_stack_archive(cat_t, os.path.join(td, "s", "1.6_live.7z"), jt)
                    raise AssertionError("a certificate failure must end the download")
                except AgentError as e:
                    assert e.status == 502 and "could not be downloaded" in e.message and "archive.org" in e.message \
                        and "GIO_TLS_PINNED_FALLBACK" in e.message and "GIO_CA_FILE" in e.message, e.message
                    assert "Checksum" not in e.message and "keeps failing" not in e.message, e.message
                assert fetched_t == [cat_t["url"]], fetched_t
                assert not any("connection lost" in l for l in jt.lines), jt.lines
                time.sleep = saved_sleep_t
                # the hotpatch CDN loop: no second try on a certificate failure, the next upstream at once
                g["HOTPATCH_DIR"] = os.path.join(td, "mirror")
                del fetched_t[:]

                def cdn_fetch(url, dst_, **kw):
                    fetched_t.append(url)
                    if "a.example" in url:
                        raise TlsVerifyError("a.example", "certificate verify failed")
                    return 7

                g["_fetch_to_file"] = cdn_fetch
                entry = {"md5": "1" * 32, "size": 7}
                assert hotpatch_fetch(["https://a.example", "https://b.example"], "client_app/x.bin", entry) == 7
                assert [u.split("/")[2] for u in fetched_t] == ["a.example", "b.example"], fetched_t
                del fetched_t[:]
                g["_fetch_to_file"] = tls_fetch
                try:
                    hotpatch_fetch(["https://a.example", "https://b.example"], "client_app/y.bin", entry)
                    raise AssertionError("every upstream refused")
                except AgentError as e:
                    assert e.status == 502 and e.message.count("TLS certificate not verified") == 2, e.message
                assert len(fetched_t) == 2, "one try per upstream: %s" % fetched_t
                # a bundle whose certificate does not verify: one try, then the per-file CDN path
                g["HOTPATCH_BUNDLE_DIR"] = os.path.join(td, "bundles")
                del fetched_t[:]
                spec_t = {"name": "1.6_hotpatch.7z", "url": "https://archive.org/download/x/1.6_hotpatch.7z",
                          "size": 10, "md5": "0" * 32}
                try:
                    hotpatch_bundle_fetch(spec_t, _Job())
                    raise AssertionError("must fall back to the CDN")
                except BundleUnavailable as e:
                    assert "TLS certificate not verified" in str(e), e
                assert len(fetched_t) == 1, fetched_t
            finally:
                urllib.request.urlopen = saved_open_t
                urllib.request.install_opener(saved_opener_t)
                time.sleep = saved_sleep_t
                g.update(saved_t)
                _TLS_FALLBACK_HOSTS.clear()
                _TLS_FALLBACK_HOSTS.update(saved_hosts_t)
                _fetch_cancel.clear()
        ok("TLS OK (STRICT cleared + full verification kept, GIO_CA_FILE failure ignored and recorded as not loaded, "
           "the certificate failure found behind URLError, pinned-only unverified fallback judged by the md5, one "
           "WARNING per host that promises only md5-checked files, stack "
           "download 502 at once, CDN next upstream, bundle -> CDN, _short_err texts)")

    # Agent settings (agent 3.6): GET/POST /agent/config + POST /agent/restart on the pure helpers and a
    # scratch config file. Nothing here writes the box's config, exits or starts a process.
    box_cfg = CONFIG_PATH
    box_cfg_before = _box_state_bytes(box_cfg) if box_cfg else None
    assert len(AGENT_SETTING_KEYS) == len(set(AGENT_SETTING_KEYS)) == 22 and set(AGENT_SETTINGS) == set(AGENT_SETTING_KEYS)
    assert not set(AGENT_SETTINGS) & set(_SETTING_REFUSED)
    for key, spec in AGENT_SETTINGS.items():
        assert spec["group"] in ("general", "network", "storage", "downloads"), key
        assert spec["type"] in ("ip", "host", "url", "path", "text", "bool", "mib", "seconds", "enum"), key
        assert spec["apply"] in ("live", "restart") and (spec["type"] == "enum") == bool(spec["choices"]), key
        assert spec["var"] in globals(), key
    assert [k for k in AGENT_SETTING_KEYS if AGENT_SETTINGS[k]["apply"] == "restart"] == [
        "GIO_ADVERTISED_IP", "GIO_ADVERTISED_HOST", "GIO_HOTPATCH_DIR", "GIO_HOTPATCH_BUNDLE_DIR"]
    # The live apply parses a value exactly like the import-time code: these are the 3.5 expressions,
    # verbatim, over an environment holding only that key (None = absent).
    falsy_t = ("0", "false", "no", "off")
    truthy_t = ("1", "true", "yes", "on")

    def bundle_35(e):
        d = os.path.join(os.path.dirname(os.path.abspath(HOTPATCH_DIR)), "hotpatch-bundles")
        b = e.get("GIO_HOTPATCH_BUNDLE_DIR", "").strip() or d
        return d if _bundle_dir_inside_mirror(b, HOTPATCH_DIR) else b

    import_time = {
        "GIO_SERVER_NAME": lambda e: e.get("GIO_SERVER_NAME", "").strip() or socket.gethostname(),
        "GIO_PROVISION_MODE": lambda e: e.get("GIO_PROVISION_MODE", "once").strip().lower(),
        "GIO_TXT_FIXES_MODE": lambda e: e.get("GIO_TXT_FIXES_MODE", "now").strip().lower(),
        "GIO_PATHFINDING": lambda e: e.get("GIO_PATHFINDING", "1").strip().lower() not in falsy_t,
        "GIO_BIND_IP": lambda e: e.get("GIO_BIND_IP", "").strip(),
        "GIO_ADVERTISED_IP": lambda e: (e.get("GIO_ADVERTISED_IP") or "").strip(),
        "GIO_ADVERTISED_HOST": lambda e: _advertised_config(e)[0],
        "GIO_ADVERTISED_ALLOW_PRIVATE": lambda e: e.get("GIO_ADVERTISED_ALLOW_PRIVATE", "0").strip().lower() in truthy_t,
        "GIO_ADVERTISED_CHECK": lambda e: _parse_check_every(e.get("GIO_ADVERTISED_CHECK")),
        "GIO_MUIP_HOST": lambda e: e.get("GIO_MUIP_HOST", "").strip(),
        "GIO_HOTPATCH_URL": lambda e: e.get("GIO_HOTPATCH_URL", "").strip().rstrip("/"),
        "GIO_TRUST_PROXY": lambda e: e.get("GIO_TRUST_PROXY", "0").strip().lower() in truthy_t,
        "GIO_HOTPATCH_DIR": lambda e: e.get("GIO_HOTPATCH_DIR", "").strip() or os.path.join(
            os.path.dirname(os.path.abspath(STATE_PATH)), "hotpatch"),
        "GIO_HOTPATCH_BUNDLE_DIR": bundle_35,
        "GIO_7Z": lambda e: e.get("GIO_7Z", "").strip(),
        "GIO_FETCH_DISK_RESERVE": lambda e: _parse_mib(e.get("GIO_FETCH_DISK_RESERVE"), 4096) << 20,
        "GIO_HOTPATCH_DISK_RESERVE": lambda e: _parse_mib(e.get("GIO_HOTPATCH_DISK_RESERVE"), 2048) << 20,
        "GIO_HOTPATCH_ONDEMAND_MAX": lambda e: _parse_mib(e.get("GIO_HOTPATCH_ONDEMAND_MAX"), 2048) << 20,
        "GIO_HOTPATCH_UPSTREAM": lambda e: e.get("GIO_HOTPATCH_UPSTREAM", "1").strip().lower() not in falsy_t,
        "GIO_HOTPATCH_BUNDLES": lambda e: e.get("GIO_HOTPATCH_BUNDLES", "1").strip().lower() not in falsy_t,
        "GIO_TLS_PINNED_FALLBACK": lambda e: e.get("GIO_TLS_PINNED_FALLBACK", "1").strip().lower() not in falsy_t,
        "GIO_CA_FILE": lambda e: e.get("GIO_CA_FILE", "").strip(),
    }
    assert set(import_time) == set(AGENT_SETTING_KEYS)
    raws = (None, "", "   ", " 1 ", "0", "OFF", "Yes", "maybe", "15", "100000", "-4", "abc", " Switch ", "later",
            "http://h.example/x/", "/abs/p", "x.example.", "C:\\relic_servers\\hotpatch")
    for key in AGENT_SETTING_KEYS:
        spec = AGENT_SETTINGS[key]
        for raw in raws:
            env = {} if raw is None else {key: raw}
            assert spec["parse"](raw) == import_time[key](env), (key, raw, spec["parse"](raw), import_time[key](env))
        assert _setting_default(key) == spec["show"](import_time[key]({})), key
        assert isinstance(_setting_value(key), str), key
    for key, raw, want in (("GIO_FETCH_DISK_RESERVE", "8192", 8192 << 20), ("GIO_ADVERTISED_CHECK", "10", 30),
                           ("GIO_HOTPATCH_UPSTREAM", "0", False), ("GIO_TRUST_PROXY", "1", True),
                           ("GIO_PROVISION_MODE", "switch", "switch"), ("GIO_SERVER_NAME", " Relic ", "Relic")):
        assert AGENT_SETTINGS[key]["parse"](raw) == want, (key, raw)
    # (No check of the running globals against this process' environment: install_agent.sh runs the
    # selftest with the box's GIO_* exported, and a block above that restores a global to a literal
    # instead of its saved value would then abort an upgrade over a difference that is not a defect.)
    ok("agent settings registry OK (22 keys, the live parse equals the import-time expressions, defaults)")

    with tempfile.TemporaryDirectory() as td:
        # update_config_file: comments, blank lines, unknown keys and the order stay; duplicates collapse
        cfg = os.path.join(td, "config")
        write_text(cfg, "# Relic agent\nGIO_AGENT_TOKEN=tok\n\n  GIO_BIND_IP = 192.0.2.1\nOTHER=keep me\n"
                        "GIO_BIND_IP=192.0.2.2\n#GIO_TRUST_PROXY=1\nGIO_URL=http://h.example/?a=b\n")
        new = update_config_file(cfg, {"GIO_BIND_IP": "192.0.2.3", "GIO_TRUST_PROXY": "1"})
        assert new == read_text(cfg) == ("# Relic agent\nGIO_AGENT_TOKEN=tok\n\nGIO_BIND_IP=192.0.2.3\nOTHER=keep me\n"
                                         "#GIO_TRUST_PROXY=1\nGIO_URL=http://h.example/?a=b\nGIO_TRUST_PROXY=1\n"), new
        assert config_dict(read_config_pairs(cfg), True) == config_dict(read_config_pairs(cfg), False), \
            "after a write first-wins and last-wins read the same"
        assert update_config_file(cfg, {"GIO_BIND_IP": "", "GIO_NOT_THERE": ""}) == (
            "# Relic agent\nGIO_AGENT_TOKEN=tok\n\nOTHER=keep me\n#GIO_TRUST_PROXY=1\nGIO_URL=http://h.example/?a=b\n"
            "GIO_TRUST_PROXY=1\n")
        # CRLF kept, BOM dropped, a last line without its newline, a byte that is not UTF-8
        with open(cfg, "wb") as f:
            f.write(b"\xef\xbb\xbfNAME=caf\xe9\r\nGIO_X=1\r\n# c\r\nGIO_Z=9")
        update_config_file(cfg, {"GIO_X": "2", "GIO_Y": "4", "GIO_Z": "8"})
        with open(cfg, "rb") as f:
            assert f.read() == b"NAME=caf\xe9\r\nGIO_X=2\r\n# c\r\nGIO_Z=8\r\nGIO_Y=4\r\n"
        assert render_config_update("", {}) == "" and render_config_update("", {"GIO_A": "1"}) == "GIO_A=1\n"
        # a missing file is created 0600; an existing file keeps its mode; no temp file stays behind
        fresh = os.path.join(td, "fresh", "config")
        os.makedirs(os.path.dirname(fresh))
        assert update_config_file(fresh, {"GIO_SERVER_NAME": "Relic"}) == "GIO_SERVER_NAME=Relic\n"
        if os.name != "nt":
            assert os.stat(fresh).st_mode & 0o777 == 0o600, oct(os.stat(fresh).st_mode)
            os.chmod(cfg, 0o640)
            update_config_file(cfg, {"GIO_X": "3"})
            assert os.stat(cfg).st_mode & 0o777 == 0o640, oct(os.stat(cfg).st_mode)
        assert not [n for n in os.listdir(td) + os.listdir(os.path.dirname(fresh)) if n.endswith(".relic-tmp")]
        ok("config writer OK (comments/order/unknown keys kept, duplicates collapsed, remove, append, CRLF, "
           "no BOM, non-UTF-8 bytes, 0600 new file, mode kept)")

        # values: every type validated and normalised; "" removes a key; 400 names the key
        def refused_n(key, value):
            try:
                normalise_setting(key, value)
            except AgentError as e:
                assert e.status == 400 and e.message.startswith(key + ": "), e.message
                return e.message
            raise AssertionError("%s=%r must be refused" % (key, value))

        n_ = normalise_setting
        for key in AGENT_SETTING_KEYS:
            assert n_(key, "") == "" and n_(key, "  ") == "", key
        assert n_("GIO_TRUST_PROXY", "Yes") == "1" and n_("GIO_TRUST_PROXY", "off") == "0"
        assert n_("GIO_TRUST_PROXY", True) == "1" and n_("GIO_PATHFINDING", False) == "0"
        refused_n("GIO_TRUST_PROXY", "maybe")
        assert n_("GIO_PROVISION_MODE", " Switch ") == "switch" and n_("GIO_TXT_FIXES_MODE", "LATER") == "later"
        refused_n("GIO_PROVISION_MODE", "sometimes")
        assert n_("GIO_BIND_IP", " 192.0.2.10 ") == "192.0.2.10"
        for v in ("999.1.1.1", "2001:db8::1", "192.0.2"):
            refused_n("GIO_BIND_IP", v)
        assert n_("GIO_ADVERTISED_HOST", "game.example.com.") == "game.example.com"
        for v in ("192.0.2.10", "-bad-.example.com", "a b.example.com", "x" * 64 + ".example.com", "game..example.com"):
            refused_n("GIO_ADVERTISED_HOST", v)
        assert n_("GIO_MUIP_HOST", "http://192.0.2.10:21051/") == "http://192.0.2.10:21051"
        assert n_("GIO_HOTPATCH_URL", "https://game.example.com/hotpatch/") == "https://game.example.com/hotpatch"
        for v in ("ftp://game.example.com", "http://", "game.example.com", "http://h.example/?q=1",
                  "http://h.example:99999", "http://h.example/#x"):
            refused_n("GIO_MUIP_HOST", v)
        assert n_("GIO_FETCH_DISK_RESERVE", " 4096 ") == "4096" and n_("GIO_FETCH_DISK_RESERVE", 0) == "0"
        assert n_("GIO_HOTPATCH_ONDEMAND_MAX", "010") == "10"
        for v in ("10485761", "-1", "1.5", "4 GB"):
            refused_n("GIO_FETCH_DISK_RESERVE", v)
        assert n_("GIO_ADVERTISED_CHECK", "30") == "30" and n_("GIO_ADVERTISED_CHECK", 86400) == "86400"
        for v in ("29", "86401"):
            refused_n("GIO_ADVERTISED_CHECK", v)
        assert n_("GIO_SERVER_NAME", "  My box ") == "My box"
        for v in ("x" * 65, "a\nb", "a\tb", '"quoted"', "'quoted'", ["list"], None, 1.5):
            refused_n("GIO_SERVER_NAME", v)
        assert n_("GIO_HOTPATCH_DIR", td) == td and n_("GIO_HOTPATCH_BUNDLE_DIR", os.path.join(td, "new")) == \
            os.path.join(td, "new"), "a folder need not exist yet"
        refused_n("GIO_HOTPATCH_DIR", "relative" + os.sep + "dir")
        refused_n("GIO_HOTPATCH_DIR", cfg)  # a file
        assert n_("GIO_7Z", cfg) == cfg
        refused_n("GIO_7Z", os.path.join(td, "nope.exe"))
        refused_n("GIO_7Z", td)  # a folder
        if ssl is not None:
            junk = os.path.join(td, "junk.pem")
            with open(junk, "w") as f:
                f.write("not a certificate\n")
            refused_n("GIO_CA_FILE", junk)
        refused_n("GIO_CA_FILE", os.path.join(td, "missing.pem"))
        # one config line per value: every character str.splitlines() breaks at (U+2028 / U+2029 / NEL too -- they
        # would split the line for the agent's reader alone), C1 controls, and no lone surrogate (a JSON "\udc80"
        # would land in the file as a byte no UTF-8 reader accepts); path / URL values are capped
        breaks = [chr(i) for i in range(0x10000) if len(("a%sb" % chr(i)).splitlines()) > 1]
        assert set(breaks) >= {"\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}, breaks
        for c in breaks + ["\x00", "\x7f", "\x9b"]:
            assert plain_line_problem("a%sb" % c), repr(c)
            refused_n("GIO_SERVER_NAME", "Box%sGIO_PAYLOAD_DIR=D:/x" % c)
            refused_n("GIO_HOTPATCH_BUNDLE_DIR", os.path.join(td, "hp%sGIO_AGENT_LISTEN=0.0.0.0:1" % c))
        assert plain_line_problem("Relic box \u00e9 \u2713") is None
        assert n_("GIO_SERVER_NAME", "Caf\u00e9") == "Caf\u00e9"
        for key, v in (("GIO_SERVER_NAME", "Box\udc80"), ("GIO_HOTPATCH_DIR", os.path.join(td, "x\udcff")),
                       ("GIO_MUIP_HOST", "http://h.example/\udc80")):
            assert "unpaired surrogate" in refused_n(key, v), (key, v)
        assert ("at most %d" % SETTING_PATH_MAX) in refused_n("GIO_HOTPATCH_DIR", os.path.join(td, "x" * SETTING_PATH_MAX))
        refused_n("GIO_HOTPATCH_URL", "http://h.example/" + "x" * SETTING_PATH_MAX)
        # the readers split at "\n" only -- like systemd, bash and the launcher -- so a separator stays in its value
        assert parse_config_text("GIO_SERVER_NAME=a\u2028GIO_X=1\r\nGIO_Y=2\n") == [
            ("GIO_SERVER_NAME", "a\u2028GIO_X=1"), ("GIO_Y", "2")]
        # a stray byte (a hand edit in another code page) or a value the environment cannot hold never stops a
        # start: both readers run at import, where an exception is a crash-loop under systemd
        odd = os.path.join(td, "odd.cfg")
        with open(odd, "wb") as f:
            f.write(b"GIO_SERVER_NAME=Box\x80\nGIO_A=1\nGIO_LONG=xyz\nGIO_B=2\n")
        assert read_config_pairs(odd) == [("GIO_SERVER_NAME", "Box\ufffd"), ("GIO_A", "1"), ("GIO_LONG", "xyz"),
                                          ("GIO_B", "2")]
        assert read_config_values(odd)["GIO_SERVER_NAME"] == "Box\ufffd"

        class PickyEnv(dict):
            def __setitem__(self, k, v):
                if k == "GIO_LONG":
                    raise ValueError("the environment variable is longer than 32767 characters")
                dict.__setitem__(self, k, v)

        picky, picky_logs, saved_ll = PickyEnv(), [], g["log_line"]
        g["log_line"] = picky_logs.append
        try:
            assert apply_config_file(odd, picky) == ["GIO_SERVER_NAME", "GIO_A", "GIO_B"] and "GIO_LONG" not in picky
        finally:
            g["log_line"] = saved_ll
        assert any("GIO_LONG" in l and "ignored" in l for l in picky_logs), picky_logs
        # the hotpatch mirror is served without a token and the agent empties the bundle folder: a folder of their
        # own -- never a drive root, one of the agent's folders or anything overlapping a stack; inside the state
        # folder is fine (the default)
        own_t = _agent_owned_folders(os.path.join(td, "cfgdir", "config"), os.path.join(td, "state", "state.json"),
                                     os.path.join(td, "payloads"))
        stacks_t = [("1.6", os.path.join(td, "srv", "1.6_live")), ("2.8", "")]
        root_t = (os.path.splitdrive(td)[0] + os.sep) if os.name == "nt" else "/"
        assert "root of a drive" in _served_folder_problem(root_t, own_t, stacks_t)
        for bad_f, needle in ((os.path.join(td, "cfgdir"), "configuration folder"),
                              (os.path.join(td, "state"), "state folder"), (td, "state folder"),
                              (os.path.join(td, "payloads"), "payloads"),
                              (os.path.dirname(os.path.abspath(__file__)), "own folder"),
                              (os.path.join(td, "srv"), "version 1.6"), (os.path.join(td, "srv", "1.6_live"), "version 1.6"),
                              (os.path.join(td, "srv", "1.6_live", "hp"), "version 1.6")):
            problem_ = _served_folder_problem(bad_f, own_t, stacks_t)
            assert problem_ and needle in problem_, (bad_f, problem_)
        for good_f in (os.path.join(td, "state", "hotpatch"), os.path.join(td, "hp"), os.path.join(td, "srv", "hotpatch"),
                       os.path.join(td, "cfgdir", "hp"), os.path.join(td, "payloads", "hp")):
            assert _served_folder_problem(good_f, own_t, stacks_t) is None, good_f
        # ... and a hand-set refused folder falls back to the default at start (a WARNING, never a crash-loop)
        sp_t = os.path.join(td, "state", "state.json")
        dm_, db_ = os.path.join(td, "state", "hotpatch"), os.path.join(td, "state", "hotpatch-bundles")
        assert _hotpatch_dirs_from(None, None, sp_t, own_t, stacks_t) == (dm_, db_, [])
        m_, b_, r_ = _hotpatch_dirs_from(os.path.join(td, "srv", "1.6_live"), None, sp_t, own_t, stacks_t)
        assert (m_, b_, [x[0] for x in r_]) == (dm_, db_, ["GIO_HOTPATCH_DIR"]), (m_, b_, r_)
        m_, b_, r_ = _hotpatch_dirs_from(" ", os.path.join(td, "cfgdir"), sp_t, own_t, stacks_t)
        assert (m_, b_, [x[0] for x in r_]) == (dm_, db_, ["GIO_HOTPATCH_BUNDLE_DIR"]), (m_, b_, r_)
        m_, b_, r_ = _hotpatch_dirs_from(os.path.join(td, "hp"), os.path.join(td, "hp", "b"), sp_t, own_t, stacks_t)
        assert (m_, b_, [x[0] for x in r_]) == (os.path.join(td, "hp"), os.path.join(td, "hotpatch-bundles"),
                                                 ["GIO_HOTPATCH_BUNDLE_DIR"]), (m_, b_, r_)
        m_, b_, r_ = _hotpatch_dirs_from(os.path.join(td, "hp"), os.path.join(td, "bundles"), sp_t, own_t, stacks_t)
        assert (m_, b_, r_) == (os.path.join(td, "hp"), os.path.join(td, "bundles"), [])
        # systemd reads the file without --config: a backslash is its escape character
        saved_pl = (g["IS_WINDOWS"], g["CONFIG_FILE"])
        try:
            g["IS_WINDOWS"], g["CONFIG_FILE"] = False, None
            refused_n("GIO_SERVER_NAME", "back\\slash")
            g["CONFIG_FILE"] = cfg
            assert n_("GIO_SERVER_NAME", "back\\slash") == "back\\slash"
        finally:
            g["IS_WINDOWS"], g["CONFIG_FILE"] = saved_pl
        ok("setting values OK (bool/enum/ip/host/url/mib/seconds/path/file/pem/text validated and normalised, "
           "\"\" removes, the systemd backslash refused; every splitlines break / C1 control / lone surrogate "
           "refused, path + URL capped, the readers split at LF only and survive a stray byte or an unusable "
           "value; the mirror / bundle folder guard and its fallback at start)")

        # the environment wins over the file: first-wins (--config) vs last-wins (systemd), overrides
        assert config_dict([("A", "1"), ("B", "2"), ("A", "3")], True) == {"A": "1", "B": "2"}
        assert config_dict([("A", "1"), ("B", "2"), ("A", "3")], False) == {"A": "3", "B": "2"}
        assert env_overridden("GIO_X", {"GIO_X": "a"}, {"GIO_X": "a"}, False) is False, "the file's own value"
        assert env_overridden("GIO_X", {"GIO_X": "b"}, {"GIO_X": "a"}, False) is True, "an export"
        assert env_overridden("GIO_X", {"GIO_X": "b"}, {}, False) is True, "--config / a manual run: an export wins"
        assert env_overridden("GIO_X", {}, {"GIO_X": "a"}, False) is False
        # systemd: the EnvironmentFile beats Environment= and the manager's environment -- only a key the file sets
        # that arrived with another value (a later EnvironmentFile= of a drop-in) is overridden
        assert env_overridden("GIO_X", {"GIO_X": "a"}, {"GIO_X": "a"}, True) is False, "systemd's EnvironmentFile itself"
        assert env_overridden("GIO_X", {"GIO_X": "b"}, {}, True) is False, "an Environment= drop-in loses to the file"
        assert env_overridden("GIO_X", {"GIO_X": "b"}, {"GIO_X": "a"}, True) is True, "another EnvironmentFile= won"
        assert env_overridden("GIO_X", {}, {"GIO_X": "a"}, True) is False
        assert env_beneath_file("GIO_X", {"GIO_X": "b"}, {}, True) == "b", "Environment= applies, the file silent"
        assert env_beneath_file("GIO_X", {"GIO_X": "a"}, {"GIO_X": "a"}, True) is None
        assert env_beneath_file("GIO_X", {"GIO_X": "b"}, {}, False) is None
        assert env_beneath_file("GIO_X", {}, {}, True) is None
        assert runs_from_systemd_file() is (not CONFIG_FILE and CONFIG_PATH == SYSTEMD_CONFIG
                                            and restart_mode() == "systemd")
        # a self-restart: what the file owned at boot follows the file NOW (changed, or gone); an explicit override
        # and the rest of the environment stay
        assert respawn_env({"PATH": "/usr/bin", "GIO_A": "1", "GIO_B": "2", "GIO_C": "x", "HTTPS_PROXY": "p"},
                           {"GIO_A": "1", "GIO_B": "2", "GIO_C": "y", "HTTPS_PROXY": "p"},
                           {"GIO_A": "9", "GIO_C": "y", "HTTPS_PROXY": "p"}) == \
            {"PATH": "/usr/bin", "GIO_A": "9", "GIO_C": "x", "HTTPS_PROXY": "p"}
        assert respawn_env({}, {"GIO_A": "1"}, {"GIO_A": "2"}) == {}, "the file is read by the new process itself"
        # restart: systemd only for a child of PID 1 off Windows, else a respawn when the script is a file
        script = os.path.join(td, "gio_agent.py")
        write_text(script, "# selftest\n")
        assert restart_mode({"INVOCATION_ID": "x"}, False, [script], "/usr/bin/python3", ppid=1) == "systemd"
        assert restart_mode({"JOURNAL_STREAM": "8:1"}, False, [script], "/usr/bin/python3", ppid=1) == "systemd"
        assert restart_mode({"INVOCATION_ID": "x"}, False, [script], "/usr/bin/python3", ppid=4321) == "self"
        assert restart_mode({"INVOCATION_ID": "x"}, True, [script], "pythonw.exe", ppid=1) == "self"
        assert restart_mode({}, False, [os.path.join(td, "missing.py")], "/usr/bin/python3", ppid=1) is None
        assert restart_mode({}, False, [script], "", ppid=1) is None and restart_mode({}, False, [], "py") is None
        cmd, kw = respawn_command([script, "--config", "cfg", "--log", "log"], "pythonw.exe", {"A": "1"}, True, cwd=td)
        assert cmd == ["pythonw.exe", os.path.abspath(script), "--config", "cfg", "--log", "log"], cmd
        assert kw["env"] == {"A": "1"} and kw["cwd"] == td and kw["close_fds"] is True, kw
        assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
        assert kw["creationflags"] == (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)) and "start_new_session" not in kw
        boot = {"PATH": "/usr/bin"}
        cmd, kw = respawn_command([script], "/usr/bin/python3", boot, False)
        assert kw["start_new_session"] is True and "creationflags" not in kw and kw["env"] == boot and kw["env"] is not boot
        env_ = respawn_command([script], "py")[1]["env"]
        assert env_ == respawn_env() and env_ is not os.environ, "the BOOT environment, never os.environ"
        # the listening socket: "address in use" is retried (the previous process closing), anything else is not
        tries, naps = [], []

        def busy_then_ok(addr):
            tries.append(addr)
            if len(tries) == 1:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            if len(tries) == 2:
                raise OSError(10048, "Only one usage of each socket address is normally permitted")
            return "server"

        assert bind_http_server("127.0.0.1", 18080, factory=busy_then_ok, sleep=naps.append) == "server"
        assert tries == [("127.0.0.1", 18080)] * 3 and naps == [0.5, 0.5], (tries, naps)

        def denied(addr):
            raise OSError(errno.EACCES, "Permission denied")

        def always_busy(addr):
            raise OSError(errno.EADDRINUSE, "Address already in use")

        for fac, wait in ((denied, 10.0), (always_busy, 0)):
            try:
                bind_http_server("127.0.0.1", 18080, factory=fac, wait=wait, sleep=naps.append)
                raise AssertionError("must raise")
            except OSError as e:
                assert e.errno in (errno.EACCES, errno.EADDRINUSE)
        assert naps == [0.5, 0.5], "neither a refusal nor an expired wait sleeps again"
        # Windows: agent.pid beside --config, the launcher reads it
        assert write_pid_file(os.path.join(td, "config"), True, pid=4242) == os.path.join(td, "agent.pid")
        assert read_text(os.path.join(td, "agent.pid")) == "4242"
        assert write_pid_file(os.path.join(td, "config"), False, pid=1) is None and write_pid_file("", True, pid=1) is None
        # set_version_dir: one fresh dict, the project derived, "" = not configured
        saved_meta = {v: dict(VERSIONS[v]) for v in VERSIONS}
        try:
            assert dir_setting_key("2.8") == "GIO_DIR_28" and dir_setting_key("1.6") == "GIO_DIR_16"
            old = VERSIONS["2.8"]
            set_version_dir("2.8", os.path.join(td, "2.8_live"))
            assert VERSIONS["2.8"] is not old and VERSIONS["2.8"] == {"dir": os.path.join(td, "2.8_live"),
                                                                      "project": "28_live"}, VERSIONS["2.8"]
            set_version_dir("2.8", "")
            assert VERSIONS["2.8"] == {"dir": "", "project": ""} and not is_configured("2.8")
            try:
                set_version_dir("9.9", td)
                raise AssertionError("an unknown version must be refused")
            except AgentError as e:
                assert e.status == 400
        finally:
            for v, meta in saved_meta.items():
                VERSIONS[v] = meta
        ok("restart + env helpers OK (override detection incl. systemd's EnvironmentFile-over-Environment= "
           "precedence, first/last wins, restart mode, respawn argv/env/flags with the file authoritative for what "
           "it owned, bind retry, agent.pid, set_version_dir)")

        # The routes, on a scratch config: GET body, POST refusals (nothing written), a save applying the live
        # keys, /agent/restart's refusals and its flag -- the restart itself is never launched.
        live_vars = [AGENT_SETTINGS[k]["var"] for k in AGENT_SETTING_KEYS]
        faked_c = tuple(dict.fromkeys(live_vars + [
            "CONFIG_PATH", "CONFIG_FILE", "_BOOT_ENV", "_BOOT_FILE", "TOKEN", "log_line", "install_http_opener",
            "running_projects", "_launch_restart", "restart_mode", "_HTTPD", "_restarting", "_TLS_CONTEXT",
            "_TLS_CA_LOADED", "_TLS_CA_ERROR", "respawn_command", "bind_http_server"]))
        saved_c = {k: g[k] for k in faked_c}
        saved_meta = {v: dict(VERSIONS[v]) for v in VERSIONS}
        fake_busy = Job("fetch", "1.6", job_id="busy-selftest-cfg")
        try:
            cfg = os.path.join(td, "agent", "config")
            os.makedirs(os.path.dirname(cfg))
            launcher_render = ("# the launcher's render\r\nGIO_AGENT_TOKEN=tok\r\nGIO_SERVER_NAME=Old name\r\n"
                               "GIO_BIND_IP=192.0.2.1\r\n")
            write_text(cfg, launcher_render)
            opener_calls, launched = [], []
            g.update({"CONFIG_PATH": cfg, "CONFIG_FILE": cfg, "TOKEN": "selftest-token",
                      "_BOOT_ENV": {"GIO_TRUST_PROXY": "1", "PATH": "/usr/bin"},
                      "_BOOT_FILE": {"GIO_AGENT_TOKEN": "tok", "GIO_SERVER_NAME": "Old name", "GIO_BIND_IP": "192.0.2.1"},
                      "log_line": lambda *_a: None, "install_http_opener": lambda: opener_calls.append(1),
                      "running_projects": lambda: (["16_live"], None), "_launch_restart": launched.append,
                      "_TLS_CONTEXT": None, "_restarting": False, "_TLS_CA_LOADED": "", "_TLS_CA_ERROR": None})
            # the running process as if started from that file: every restart key at its default
            g.update({"_BOOT_ADVERTISED_IP": "", "ADVERTISED_HOST": "", "SERVER_NAME": "Old name", "BIND_IP": "192.0.2.1",
                      "TRUST_PROXY": True, "HOTPATCH_DIR": _hotpatch_dir_from(None, STATE_PATH)})
            g["HOTPATCH_BUNDLE_DIR"] = _bundle_dir_from(None, HOTPATCH_DIR)[0]
            VERSIONS["1.6"] = {"dir": os.path.join(td, "1.6_live"), "project": "16_live"}
            VERSIONS["2.8"] = {"dir": "", "project": ""}

            def call(method, path, body=None, token="selftest-token"):
                reply = []
                raw = b"" if body is None else json.dumps(body).encode()
                hh = Handler.__new__(Handler)
                hh.path, hh.rfile = path, io.BytesIO(raw)
                hh.headers = {"Authorization": "Bearer " + token, "Content-Length": str(len(raw))}
                hh._send = lambda status_, obj, retry_after=None: reply.append((status_, obj))
                getattr(hh, "do_" + method)()
                assert len(reply) == 1, reply
                return reply[0]

            assert call("GET", "/agent/config", token="wrong")[0] == 401
            assert call("POST", "/agent/config", {"set": {"GIO_BIND_IP": "192.0.2.9"}}, token="wrong")[0] == 401
            assert call("POST", "/agent/restart", {}, token="wrong")[0] == 401 and not launched
            st_, info = call("GET", "/agent/config")
            assert st_ == 200 and info["agent"] == AGENT_VERSION == "3.7", (st_, info)
            assert info["file"] == cfg and info["writable"] is True and info["writableReason"] is None, info
            assert info["platform"] in ("windows", "linux") and isinstance(info["started"], float)
            assert info["listen"] == format_listen(LISTEN_HOST, LISTEN_PORT) and info["restartPending"] == [], info
            assert info["tls"] == {"strict": False, "caFile": "", "caFileError": None,
                                   "pinnedFallback": TLS_PINNED_FALLBACK,
                                   "fallbackHosts": sorted(_TLS_FALLBACK_HOSTS)}, info["tls"]
            # tls.caFile names only roots the opener really carries; a GIO_CA_FILE that did not load says why
            g.update({"_TLS_CA_LOADED": "", "_TLS_CA_ERROR": "[X509] no certificate or crl found (selftest)"})
            tls_ = call("GET", "/agent/config")[1]["tls"]
            assert tls_["caFile"] == "" and tls_["caFileError"].startswith("[X509]"), tls_
            g.update({"_TLS_CA_LOADED": os.path.join(td, "roots.pem"), "_TLS_CA_ERROR": None})
            tls_ = call("GET", "/agent/config")[1]["tls"]
            assert tls_["caFile"] == os.path.join(td, "roots.pem") and tls_["caFileError"] is None, tls_
            g.update({"_TLS_CA_LOADED": "", "_TLS_CA_ERROR": None})
            assert [s["key"] for s in info["settings"]] == list(AGENT_SETTING_KEYS)
            by = {s["key"]: s for s in info["settings"]}
            for s in info["settings"]:
                assert set(s) - {"choices"} == {"key", "group", "type", "value", "stored", "default", "apply", "env"}, s
                assert isinstance(s["value"], str) and isinstance(s["default"], str), s
            assert by["GIO_SERVER_NAME"]["stored"] == "Old name" == by["GIO_SERVER_NAME"]["value"]
            assert by["GIO_TRUST_PROXY"]["env"] is True and by["GIO_TRUST_PROXY"]["stored"] is None
            assert by["GIO_TRUST_PROXY"]["value"] == "1" and by["GIO_TRUST_PROXY"]["default"] == "0"
            assert by["GIO_PROVISION_MODE"]["choices"] == ["once", "switch", "never"] and "choices" not in by["GIO_BIND_IP"]
            assert by["GIO_FETCH_DISK_RESERVE"]["default"] == "4096" and by["GIO_HOTPATCH_UPSTREAM"]["default"] == "1"
            assert by["GIO_HOTPATCH_DIR"]["apply"] == "restart" and by["GIO_HOTPATCH_DIR"]["type"] == "path"
            assert info["versions"] == {"1.6": {"dir": os.path.join(td, "1.6_live"), "configured": True, "present": False,
                                                "up": True, "project": "16_live"},
                                        "2.8": {"dir": "", "configured": False, "present": False, "up": False,
                                                "project": ""}}, info["versions"]

            def post(sets):
                return call("POST", "/agent/config", {"set": sets} if sets is not None else {})

            mirror = os.path.join(td, "mirror")
            for sets, status_, needle in (
                    (None, 400, "set must be"), ({}, 400, "set must be"), ("GIO_BIND_IP", 400, "set must be"),
                    ({"GIO_NOPE": "1"}, 400, "GIO_NOPE: unknown setting"),
                    ({"GIO_DIR_28": os.path.join(td, "2.8_live")}, 400, "GIO_DIR_28: a server's folder is changed with POST /server/relocate"),
                    ({"GIO_AGENT_TOKEN": "x"}, 400, "GIO_AGENT_TOKEN: "), ({"GIO_AGENT_LISTEN": "0.0.0.0:1"}, 400, "GIO_AGENT_LISTEN: "),
                    ({"GIO_MUIP_KEY": "x"}, 400, "GIO_MUIP_KEY: "), ({"GIO_STATE_PATH": td}, 400, "GIO_STATE_PATH: "),
                    ({"GIO_PAYLOAD_DIR": td}, 400, "GIO_PAYLOAD_DIR: "), ({"GIO_AGENT_REGION": "x"}, 400, "GIO_AGENT_REGION: "),
                    ({"GIO_HOTPATCH_SOURCE": "cdn"}, 400, "GIO_HOTPATCH_SOURCE: the hotpatch card"),
                    ({"GIO_SERVER_NAME": "ok", "GIO_BIND_IP": "nope"}, 400, "GIO_BIND_IP: "),
                    ({"GIO_ADVERTISED_IP": "198.51.100.7", "GIO_ADVERTISED_HOST": "game.example.com"}, 400,
                     "GIO_ADVERTISED_HOST: GIO_ADVERTISED_IP and GIO_ADVERTISED_HOST cannot both be set"),
                    ({"GIO_HOTPATCH_DIR": mirror, "GIO_HOTPATCH_BUNDLE_DIR": os.path.join(mirror, "b")}, 400,
                     "GIO_HOTPATCH_BUNDLE_DIR: the bundle folder"),
                    ({"GIO_SERVER_NAME": "ok", "GIO_TRUST_PROXY": "0"}, 409, "GIO_TRUST_PROXY is set by this agent's environment")):
                st_, o_ = post(sets)
                assert st_ == status_ and o_["error"].startswith(needle), (sets, st_, o_)
            # the mirror is served without a token: never a drive root, one of the agent's folders (the config with
            # the admin token!) or a folder overlapping a stack -- the bundle folder likewise
            state_d = os.path.dirname(os.path.abspath(STATE_PATH))
            drive_root = (os.path.splitdrive(td)[0] + os.sep) if os.name == "nt" else "/"
            for sets, needle in (({"GIO_HOTPATCH_DIR": os.path.join(td, "agent")}, "configuration folder"),
                                 ({"GIO_HOTPATCH_DIR": td}, "configuration folder"),
                                 ({"GIO_HOTPATCH_DIR": state_d}, "state folder"),
                                 ({"GIO_HOTPATCH_DIR": drive_root}, "root of a drive"),
                                 ({"GIO_HOTPATCH_DIR": PAYLOAD_DIR}, "payloads"),
                                 ({"GIO_HOTPATCH_DIR": os.path.dirname(os.path.abspath(__file__))}, "the agent's"),
                                 ({"GIO_HOTPATCH_DIR": os.path.join(td, "1.6_live")}, "version 1.6"),
                                 ({"GIO_HOTPATCH_DIR": os.path.join(td, "1.6_live", "hotpatch")}, "version 1.6"),
                                 ({"GIO_HOTPATCH_BUNDLE_DIR": os.path.join(td, "agent")}, "configuration folder"),
                                 ({"GIO_HOTPATCH_BUNDLE_DIR": os.path.join(td, "1.6_live", "b")}, "version 1.6")):
                st_, o_ = post(sets)
                key_ = next(iter(sets))
                assert st_ == 400 and o_["error"].startswith(key_ + ": ") and needle in o_["error"] \
                    and "a folder of its own" in o_["error"], (sets, st_, o_)
            assert read_text(cfg) == launcher_render and SERVER_NAME == "Old name", "a refused POST writes and applies nothing"
            g["CONFIG_PATH"] = None
            st_, o_ = post({"GIO_BIND_IP": "192.0.2.9"})
            assert st_ == 409 and o_["error"].startswith("This agent cannot save its configuration: it was started without"), o_
            assert call("GET", "/agent/config")[1]["writable"] is False
            g["CONFIG_PATH"] = cfg
            g["_restarting"] = True
            st_, o_ = post({"GIO_BIND_IP": "192.0.2.9"})
            assert st_ == 409 and "restarting" in o_["error"] and read_text(cfg) == launcher_render, o_
            g["_restarting"] = False
            # a save: the file rewritten in its own style, the live keys applied at once, the restart key flagged
            mirror2 = os.path.join(td, "mirror2")
            st_, o_ = post({"GIO_SERVER_NAME": "Relic test", "GIO_FETCH_DISK_RESERVE": "8192", "GIO_TXT_FIXES_MODE": "Later",
                            "GIO_BIND_IP": "", "GIO_HOTPATCH_DIR": mirror2, "GIO_TLS_PINNED_FALLBACK": False})
            assert st_ == 200, o_
            assert o_["applied"] == ["GIO_SERVER_NAME", "GIO_TXT_FIXES_MODE", "GIO_BIND_IP", "GIO_FETCH_DISK_RESERVE",
                                     "GIO_TLS_PINNED_FALLBACK"], o_["applied"]
            # the bundle folder's default lives beside the mirror: it moves with it at the restart
            assert o_["restartRequired"] == ["GIO_HOTPATCH_DIR"] and \
                o_["restartPending"] == ["GIO_HOTPATCH_DIR", "GIO_HOTPATCH_BUNDLE_DIR"], o_
            assert read_text(cfg) == ("# the launcher's render\r\nGIO_AGENT_TOKEN=tok\r\nGIO_SERVER_NAME=Relic test\r\n"
                                      "GIO_FETCH_DISK_RESERVE=8192\r\nGIO_TXT_FIXES_MODE=later\r\nGIO_HOTPATCH_DIR=%s\r\n"
                                      "GIO_TLS_PINNED_FALLBACK=0\r\n" % mirror2), read_text(cfg)
            assert (SERVER_NAME, FETCH_DISK_RESERVE, TXT_FIXES_MODE, BIND_IP, TLS_PINNED_FALLBACK) == (
                "Relic test", 8192 << 20, "later", "", False)
            assert HOTPATCH_DIR == _hotpatch_dir_from(None, STATE_PATH), "a restart key waits for the restart"
            by = {s["key"]: s for s in o_["settings"]}
            assert by["GIO_SERVER_NAME"]["value"] == "Relic test" == by["GIO_SERVER_NAME"]["stored"]
            assert by["GIO_BIND_IP"]["stored"] is None and by["GIO_HOTPATCH_DIR"]["stored"] == mirror2
            assert not opener_calls, "the opener changes only with GIO_CA_FILE"
            # setting the restart key back to what runs: nothing pending, nothing required
            st_, o_ = post({"GIO_HOTPATCH_DIR": ""})
            assert st_ == 200 and o_["restartRequired"] == [] and o_["restartPending"] == [] and o_["applied"] == [], o_
            # a folder INSIDE the state folder is fine (the default lives there)
            st_, o_ = post({"GIO_HOTPATCH_DIR": os.path.join(state_d, "mirror3")})
            assert st_ == 200 and o_["restartRequired"] == ["GIO_HOTPATCH_DIR"], o_
            # a hand edit that the next start would refuse: restartPending compares with what the start will
            # REALLY use (the default, with a WARNING), so nothing is pending
            update_config_file(cfg, {"GIO_HOTPATCH_DIR": os.path.join(td, "1.6_live")})
            assert call("GET", "/agent/config")[1]["restartPending"] == [], "a refused folder falls back to the default"
            st_, o_ = post({"GIO_HOTPATCH_DIR": ""})
            assert st_ == 200 and o_["restartPending"] == [], o_
            # GIO_CA_FILE re-installs the opener (a root of this box's own store as the PEM)
            pem = None
            if ssl is not None and hasattr(ssl, "enum_certificates"):
                for der, enc, _trust in ssl.enum_certificates("ROOT"):
                    if enc == "x509_asn":
                        pem = os.path.join(td, "roots.pem")
                        write_text(pem, ssl.DER_cert_to_PEM_cert(der))
                        break
            elif ssl is not None:
                paths = ssl.get_default_verify_paths()
                pem = next((p for p in (paths.cafile, paths.openssl_cafile) if p and os.path.isfile(p)
                            and not _ca_file_problem(p)), None)
            if pem and not _ca_file_problem(pem):
                st_, o_ = post({"GIO_CA_FILE": pem})
                assert st_ == 200 and o_["applied"] == ["GIO_CA_FILE"] and CA_FILE == pem and opener_calls == [1], o_
                st_, o_ = post({"GIO_CA_FILE": ""})
                assert st_ == 200 and CA_FILE == "" and opener_calls == [1, 1], o_
            # POST /agent/restart: 409 unless this agent can come back, is serving and runs no job at all
            g["restart_mode"] = lambda *_a, **_k: None
            st_, o_ = call("POST", "/agent/restart", {})
            assert st_ == 409 and "cannot restart itself" in o_["error"], o_
            g["restart_mode"] = lambda *_a, **_k: "self"
            g["_HTTPD"] = None
            assert call("POST", "/agent/restart", {})[0] == 409
            g["_HTTPD"] = object()
            fake_busy.yielding = True  # a yielding download counts: the process would go away under it
            with _jobs_lock:
                JOBS[fake_busy.id] = fake_busy
                JOBS_ORDER.append(fake_busy.id)
            st_, o_ = call("POST", "/agent/restart", {})
            assert st_ == 409 and o_["error"].startswith("Another operation is already running") and not launched, o_
            with _jobs_lock:
                JOBS.pop(fake_busy.id, None)
                JOBS_ORDER.remove(fake_busy.id)
            st_, o_ = call("POST", "/agent/restart", {})
            assert (st_, o_) == (202, {"ok": True, "restarting": True, "how": "self"}) and launched == ["self"], (st_, o_)
            assert g["_restarting"] is True
            try:
                start_job("start", "1.6", lambda j_: None)
                raise AssertionError("no job may start while the agent restarts")
            except AgentError as e:
                assert e.status == 409 and "restarting" in e.message, e.message
            try:
                start_public_job("signup", "1.6", lambda j_: None)
                raise AssertionError("no signup may start while the agent restarts")
            except AgentError as e:
                assert e.status == 429 and e.code == "server_busy", (e.status, e.code)
            assert call("POST", "/agent/restart", {})[0] == 409 and launched == ["self"], "one restart at a time"
            assert post({"GIO_BIND_IP": "192.0.2.9"})[0] == 409
            # the restart worker itself, when no new process can be started -- here the working directory vanished,
            # which respawn_command raises (not an OSError from Popen): this process serves again, locks released
            class FakeSrv:
                def __init__(self):
                    self.calls = []

                def shutdown(self):
                    self.calls.append("shutdown")

                def server_close(self):
                    self.calls.append("close")

                def serve_forever(self):
                    self.calls.append("serve")

            def cwd_gone(*_a, **_k):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory (the working directory)")

            old_srv, new_srv, rebinds = FakeSrv(), FakeSrv(), []
            g.update({"_HTTPD": old_srv, "_restarting": True, "respawn_command": cwd_gone,
                      "bind_http_server": lambda h_, p_: rebinds.append((h_, p_)) or new_srv})
            saved_sleep_c = time.sleep
            time.sleep = lambda _s: None
            try:
                _restart_worker("self")
            finally:
                time.sleep = saved_sleep_c
            assert old_srv.calls == ["shutdown", "close"] and new_srv.calls == ["serve"], (old_srv.calls, new_srv.calls)
            assert rebinds == [(LISTEN_HOST, LISTEN_PORT)] and g["_HTTPD"] is new_srv and g["_restarting"] is False
            assert _op_lock.acquire(blocking=False), "the op lock is released"
            _op_lock.release()
            lock_free = []

            def probe_lock():
                if _config_lock.acquire(timeout=2):
                    lock_free.append(1)
                    _config_lock.release()

            th_ = threading.Thread(target=probe_lock)
            th_.start()
            th_.join(5)
            assert lock_free == [1], "the config lock is released"
        finally:
            with _jobs_lock:
                JOBS.pop(fake_busy.id, None)
                if fake_busy.id in JOBS_ORDER:
                    JOBS_ORDER.remove(fake_busy.id)
            g.update(saved_c)
            for v, meta in saved_meta.items():
                VERSIONS[v] = meta
            _invalidate_public_status()
        ok("agent config routes OK (GET body, 401, refusals write nothing: unknown / not editable / relocate / "
           "invalid / both advertised / bundle in mirror / env override / not writable / restarting; a save "
           "rewrites the file in its own style and applies the live keys; the mirror / bundle folder refusals "
           "(drive root, agent folders, stacks) and the next-start fallback; tls caFile / caFileError; restart "
           "409s, 202 + the job freeze; a respawn that fails keeps this process serving)")

    # Stack relocation (agent 3.6): POST /server/relocate on fake stacks in temp dirs. docker (ls, compose),
    # the start routine and -- for the forced cross-volume cases -- the volume test are faked; the copy is
    # the real one (cp -a on POSIX, the walk on Windows). Nothing here touches a real stack or the box config.
    # Every field a version_state_set() call of the agent writes is either dropped or kept by a repoint.
    import ast
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            agent_tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        agent_tree = None
    if agent_tree is not None:
        written = set()
        for top in agent_tree.body:
            if isinstance(top, ast.FunctionDef) and top.name == "selftest":
                continue
            for node in ast.walk(top):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "version_state_set":
                    written |= {k.arg for k in node.keywords if k.arg not in (None, "strict", "best_effort")}
        # the two **dict calls: apply_advertised_ip's rec, do_auth's st
        written |= {"advertisedIp", "advertisedIpSql", "advertisedIpSqlOwed", "passwordVerify", "defaultPassword"}
        assert len(written) > 15, written
        assert written <= set(_RELOCATE_DROP) | set(_RELOCATE_KEEP), \
            "version_state_set() fields a repoint does not classify: %s" % sorted(
                written - set(_RELOCATE_DROP) - set(_RELOCATE_KEEP))
    assert not set(_RELOCATE_DROP) & set(_RELOCATE_KEEP) and "relocate" not in SERVICE_REPORT_JOBS
    # an emptied stack folder is written "KEY=" (on Linux an absent GIO_DIR_xx means /home/<v>_live)
    assert render_config_update("A=1\nGIO_DIR_16=/x\nGIO_DIR_16=/y\n", {"GIO_DIR_16": ""},
                                explicit_empty=("GIO_DIR_16",)) == "A=1\nGIO_DIR_16=\n"
    assert render_config_update("A=1\r\n", {"GIO_DIR_16": ""}, explicit_empty=("GIO_DIR_16",)) == "A=1\r\nGIO_DIR_16=\r\n"
    assert render_config_update("A=1\nGIO_DIR_16=/x\n", {"GIO_DIR_16": ""}) == "A=1\n", "without it \"\" still removes"
    assert parse_config_text("GIO_DIR_16=\n") == [("GIO_DIR_16", "")]
    ok("relocation state classification OK (every version_state_set field dropped or kept by a repoint; an "
       "emptied GIO_DIR_xx written as KEY=)")

    reloc_faked = ("CONFIG_PATH", "CONFIG_FILE", "IS_WINDOWS", "_BOOT_ENV", "_BOOT_FILE", "STATE_PATH", "TOKEN",
                   "log_line", "running_projects", "_compose_stream", "do_start", "_same_volume", "_copy_tree",
                   "_free_bytes_at", "save_config_values", "HOTPATCH_DIR", "HOTPATCH_BUNDLE_DIR", "PAYLOAD_DIR",
                   "FETCH_DISK_RESERVE", "read_manifest", "protect_path", "compose_projects_all", "_is_mount",
                   "_win_drive_type", "_sync_filesystems", "PROVISION_MODE")
    saved_r = {k: g[k] for k in reloc_faked}
    saved_meta = {v: dict(VERSIONS[v]) for v in VERSIONS}
    saved_sleep_r = time.sleep
    fake_reloc = Job("relocate", "1.6", job_id="busy-selftest-reloc")
    route_jobs = []
    with tempfile.TemporaryDirectory() as td:
        try:
            agent_d = os.path.join(td, "agent")
            os.makedirs(agent_d)
            cfg = os.path.join(agent_d, "config")
            s16, s28 = os.path.join(td, "srv", "1.6_live"), os.path.join(td, "srv", "2.8_live")
            cfg_text = ("# the launcher's render\nGIO_AGENT_TOKEN=tok\nGIO_DIR_16=%s\nGIO_DIR_28=%s\n"
                        "HTTPS_PROXY=http://192.0.2.1:3128\n" % (s16, s28))
            write_text(cfg, cfg_text)
            running, calls, starts, protected = set(), [], [], []
            ls_fail, cancel_on_down, down_rc, all_projects, syncs = [], [], [], [], []

            def fake_ls():
                return (None, "docker unavailable (selftest)") if ls_fail else (sorted(running), None)

            def fake_compose(directory, job_, *args, **kw):
                calls.append((directory, args, version_state("1.6").get("runningOk")))
                if "down" in args:
                    if down_rc:
                        return down_rc[0]  # a failed down: the project may well still be (partly) up
                    running.discard(args[args.index("-p") + 1] if "-p" in args else _project_name(directory))
                    if cancel_on_down:
                        _relocate_cancel.set()
                return 0

            def fake_start(version, job_, provision="auto"):
                starts.append((version, provision, VERSIONS[version]["dir"]))
                running.add(VERSIONS[version]["project"])
                version_state_set(version, runningOk=True)
                return {"started": True}

            g.update({"CONFIG_PATH": cfg, "CONFIG_FILE": cfg, "_BOOT_ENV": {}, "_BOOT_FILE": {},
                      "STATE_PATH": os.path.join(agent_d, "state.json"), "TOKEN": "selftest-token",
                      "log_line": lambda *_a: None, "running_projects": fake_ls, "_compose_stream": fake_compose,
                      "do_start": fake_start, "HOTPATCH_DIR": os.path.join(agent_d, "hotpatch"),
                      "HOTPATCH_BUNDLE_DIR": os.path.join(agent_d, "hotpatch-bundles"),
                      "PAYLOAD_DIR": os.path.join(agent_d, "payloads"), "FETCH_DISK_RESERVE": 1 << 20,
                      "read_manifest": lambda _v: None, "protect_path": lambda p_, _m: protected.append(p_),
                      "compose_projects_all": lambda: ((list(all_projects), None) if not ls_fail
                                                       else (None, "docker unavailable (selftest)")),
                      "_is_mount": lambda _p: False, "_win_drive_type": lambda _l: 3,
                      "_sync_filesystems": lambda: syncs.append(1), "PROVISION_MODE": "once"})
            time.sleep = lambda _s: None  # the rename retry's waits

            def make_stack(d, marker=None, verify=None):
                files = {"docker-compose.yml.tmpl": "services: {}\n", "docker-compose.yml": "services: {}\n",
                         ".bootstrap.lock": "\n", ".env": "MYSQL_ROOT_PASSWORD=pw\nOUTER_IP=192.0.2.10\n",
                         "creds.txt": "MYSQL_ROOT_PASSWORD: pw\n", os.path.join("server", "gameserver", "gameserver"): "ELF",
                         os.path.join("mysql", "ibdata1"): "x" * 4096, os.path.join("redis", "dump.rdb"): "REDIS0009"}
                if marker:
                    files[PROVISIONED_MARKER] = marker
                if verify is not None:
                    files[os.path.join("sdk", "data", "config.json.tmpl")] = \
                        '{"auth": {"enable_password_verify": %s}}\n' % ("true" if verify else "false")
                for rel, text in files.items():
                    p_ = os.path.join(d, rel)
                    os.makedirs(os.path.dirname(p_), exist_ok=True)
                    write_text(p_, text)
                os.makedirs(os.path.join(d, "mysql", "relic_tpl_gaa"))  # an empty database folder travels too
                return _tree_counts(d)

            totals16 = make_stack(s16, marker="2026-01-02 03:04:05\nprogress=default\ndefaultAccount=1\n", verify=False)
            make_stack(s28)
            VERSIONS["1.6"] = {"dir": s16, "project": "16_live"}
            VERSIONS["2.8"] = {"dir": s28, "project": "28_live"}
            full_vs = {"runningOk": True, "provisionedAt": "2026-01-02 03:04:05", "progress": "default",
                       "defaultAccount": True, "txtFixes": "applied", "advertisedIp": "198.51.100.7",
                       "advertisedIpSql": "198.51.100.7", "advertisedIpSqlOwed": [], "towerRestartPending": False,
                       "templates": {"gaa": {"ready": True}}, "secrets": {"muipChangedAt": "t0", "muipSource": "random"},
                       "secretsPending": None, "passwordVerify": False, "passwordPending": False,
                       "defaultPassword": "selftest-pw", "fetchedAt": "2026-01-01 00:00:00", "fetchedFrom": "archive.org",
                       "pathfinding": False,
                       "hotpatch": {"enabled": True, "pending": False, "appliedAt": "t", "url": "http://192.0.2.10:18080/hotpatch",
                                    "res": 1, "data": 2, "silence": 3, "source": "bundle", "voice": ["Japanese"]}}
            save_state({"versions": {"1.6": dict(full_vs), "2.8": {"provisionedAt": "2026-05-05 05:05:05"}},
                        "lastStarted": "1.6"}, strict=True)

            def refused(v_, d_, mode_, status_, needle, in_job=False):
                rep_, err_ = relocate_check(v_, d_, mode_, in_job=in_job)
                assert err_ is not None and err_.status == status_ and needle in err_.message, \
                    (d_, mode_, err_ and (err_.status, err_.message))
                assert rep_["ok"] is False and rep_["problem"] == err_.message, rep_
                return rep_

            for v_, m_ in (("9.9", "move"), ("1.6", "copy"), ("1.6", "")):
                try:
                    relocate_check(v_, td, m_)
                    raise AssertionError("must raise for %s/%s" % (v_, m_))
                except AgentError as e:
                    assert e.status == 400, (v_, m_, e.message)
            refused("1.6", "relative" + os.sep + "1.6_live", "move", 400, "The folder must be")
            if os.name == "nt":
                refused("1.6", "\\\\server\\share\\1.6_live", "repoint", 400, "network path")
                refused("1.6", "C:relative", "repoint", 400, "local drive")
                refused("1.6", s16.upper(), "move", 400, "already is the folder")
            refused("1.6", "", "move", 400, "A move needs")
            refused("1.6", 7, "move", 400, "dir must be a string")
            refused("1.6", os.path.join(td, 'q"uote'), "repoint", 400, "without quotes")
            refused("1.6", os.path.join(td, "tab\tname"), "repoint", 400, "without quotes")
            g["CONFIG_FILE"], g["IS_WINDOWS"] = None, False  # the systemd EnvironmentFile
            refused("1.6", os.path.join(td, "back\\slash"), "repoint", 400, "backslash")
            g["CONFIG_FILE"], g["IS_WINDOWS"] = cfg, saved_r["IS_WINDOWS"]
            refused("1.6", (os.path.splitdrive(td)[0] + os.sep) if os.name == "nt" else "/", "repoint", 400,
                    "root of a drive")
            refused("1.6", os.path.join(td, "x", "___"), "repoint", 400, "no usable project name")
            refused("1.6", os.path.join(td, "x", "default"), "repoint", 400, "no usable project name")
            refused("1.6", s16 + os.sep, "repoint", 400, "already is the folder")
            refused("1.6", os.path.join(td, "other", "2.8_live"), "repoint", 400, "same name as version 2.8")
            refused("1.6", os.path.join(s28, "inner"), "repoint", 400, "inside one another")
            refused("1.6", os.path.join(td, "srv"), "repoint", 400, "inside one another")
            refused("1.6", os.path.join(agent_d, "stacks", "1.6_live"), "repoint", 400, "the agent's state folder")
            refused("1.6", os.path.join(s16, "inner"), "move", 400, "inside the current one")
            junk = os.path.join(td, "junk", "1.6_live")
            os.makedirs(junk)
            write_text(os.path.join(junk, "notes.txt"), "x")
            refused("1.6", junk, "repoint", 409, "The folder is not empty and holds no server")
            refused("1.6", junk, "move", 409, "is not empty")
            afile = os.path.join(td, "afile")
            write_text(afile, "x")
            refused("1.6", afile, "repoint", 409, "is a file")
            refused("1.6", os.path.join(afile, "1.6_live"), "repoint", 409, "is not a folder on this machine")
            stack_b = os.path.join(td, "b", "1.6_b")
            make_stack(stack_b, marker="2026-07-07 07:07:07\nprogress=keep\ndefaultAccount=0\n", verify=True)
            refused("1.6", stack_b, "move", 409, "repoint switches to it")
            g["_BOOT_ENV"] = {"GIO_DIR_16": "/elsewhere"}
            refused("1.6", stack_b, "repoint", 409, "GIO_DIR_16 is set by this agent's environment")
            g["_BOOT_ENV"] = {}
            g["CONFIG_PATH"] = None
            refused("1.6", stack_b, "repoint", 409, "cannot save its configuration")
            g["CONFIG_PATH"] = cfg
            ls_fail.append(1)
            refused("1.6", stack_b, "repoint", 500, "Cannot read the docker state")
            ls_fail.clear()
            with _jobs_lock:
                JOBS[fake_reloc.id] = fake_reloc
                JOBS_ORDER.append(fake_reloc.id)
            refused("1.6", stack_b, "repoint", 409, "Another operation is already running")
            assert relocate_check("1.6", stack_b, "repoint", in_job=True)[1] is None, "the job itself is not 'busy'"
            with _jobs_lock:
                JOBS.pop(fake_reloc.id, None)
                JOBS_ORDER.remove(fake_reloc.id)
            # a folder that holds no server: nothing to move; re-pointing it needs no docker answer
            empty28 = os.path.join(td, "e", "2.8_empty")
            os.makedirs(empty28)
            VERSIONS["2.8"] = {"dir": empty28, "project": "28_empty"}
            refused("2.8", os.path.join(td, "e2", "2.8_live"), "move", 409, "holds no server")
            ls_fail.append(1)
            assert relocate_check("2.8", s28, "repoint")[1] is None, "an empty folder needs no docker answer"
            ls_fail.clear()
            VERSIONS["2.8"] = {"dir": "", "project": ""}
            refused("2.8", "", "repoint", 400, "not configured on this agent already")
            refused("2.8", os.path.join(td, "e2", "2.8_live"), "move", 409, "has no folder on this agent yet")
            VERSIONS["2.8"] = {"dir": s28, "project": "28_live"}
            # the check reply of a move that goes: the same volume is a rename -- nothing measured
            running.add("16_live")
            new16 = os.path.join(td, "new", "1.6_moved")
            os.makedirs(new16)  # an empty folder the admin made
            rep = relocate_check("1.6", new16, "move")[0]
            assert rep == {"version": "1.6", "mode": "move", "from": s16, "to": new16, "target": "empty", "running": True,
                           "sameVolume": True, "stackBytes": None, "freeBytes": rep["freeBytes"], "needBytes": None,
                           "ok": True, "problem": None}, rep
            assert isinstance(rep["freeBytes"], int) and rep["freeBytes"] > 0, rep
            rep = relocate_check("1.6", stack_b, "repoint")[0]
            assert (rep["ok"], rep["target"], rep["sameVolume"], rep["stackBytes"], rep["needBytes"]) == \
                (True, "stack", None, None, None), rep
            assert relocate_check("1.6", os.path.join(td, "later", "1.6_live"), "repoint")[0]["target"] == "absent"
            # across volumes (forced): the stack is measured and must fit with the reserve
            g["_same_volume"] = lambda a_, b_: False
            rep = relocate_check("1.6", new16, "move")[0]
            assert rep["ok"] and rep["sameVolume"] is False and rep["stackBytes"] == totals16[2] and \
                rep["needBytes"] == totals16[2] + (1 << 20), rep
            g["_free_bytes_at"] = lambda _p: 1000
            rep = refused("1.6", new16, "move", 409, "Not enough free disk space on the target drive")
            assert rep["freeBytes"] == 1000 and rep["stackBytes"] == totals16[2], rep
            g["_free_bytes_at"], g["_same_volume"] = saved_r["_free_bytes_at"], saved_r["_same_volume"]
            # the folder value is one config line, like every setting value (a line separator, a lone surrogate)
            for c in (" ", " ", "\x85", "\x9b", "\udc80"):
                refused("1.6", os.path.join(td, "odd%sGIO_STATE_PATH=x" % c, "1.6_live"), "repoint", 400,
                        "one plain line")
            refused("1.6", os.path.join(td, "x" * SETTING_PATH_MAX, "1.6_live"), "repoint", 400, "at most")
            # Windows: a drive letter is asked what it is -- a network share, a CD, a RAM disk or no drive at all is
            # refused for the check and the job alike (only the spelling was tested before)
            if os.name == "nt":
                letter_d = os.path.splitdrive(td)[0][:1].upper()
                for dtype, needle in ((4, "network drive"), (5, "CD/DVD"), (6, "RAM disk"), (1, "not a drive")):
                    g["_win_drive_type"] = lambda _l, _t=dtype: _t
                    refused("1.6", os.path.join(td, "net", "1.6_live"), "repoint", 400, needle)
                    assert ("%s:" % letter_d) in relocate_check("1.6", os.path.join(td, "net", "1.6_live"),
                                                               "move")[0]["problem"]
                for dtype in (0, 2, 3):  # unknown (never fatal), removable, fixed
                    g["_win_drive_type"] = lambda _l, _t=dtype: _t
                    assert relocate_check("1.6", stack_b, "repoint")[1] is None, dtype
                g["_win_drive_type"] = saved_r["_win_drive_type"]
                # Ask the REAL one only what the CODE promises -- an int, and 0 instead of an
                # exception for an argument it cannot use. What the box's own TEMP drive happens to
                # be is not under test: a %TEMP% on a mapped network letter answers 4 and would
                # abort the selftest, the same class of bug as the Linux branch below.
                assert _win_drive_type(letter_d) in (0, 1, 2, 3, 4, 5, 6), _win_drive_type(letter_d)
                assert _win_drive_type(None) == 0, "an argument it cannot use is 0, never an exception"
                g["_win_drive_type"] = lambda _l: 3
            else:
                # The block above faked the drive type for every platform (g.update at the top) --
                # put the real one back before asking what it answers off Windows, or this asserts
                # against the fake and the whole selftest dies on Linux (found 2026-09-23 by an
                # install_agent.sh upgrade, which runs --selftest before it touches the box).
                g["_win_drive_type"] = saved_r["_win_drive_type"]
                assert _win_drive_type("C") == 0, "never asked off Windows"
                g["_win_drive_type"] = lambda _l: 3
            # a move takes the WHOLE current tree: refused while it holds the other server or an agent folder -- a
            # cross-drive move would delete them after the copy; a repoint touches no file and stays allowed
            inner28 = os.path.join(s16, "nested", "2.8_inner")
            VERSIONS["2.8"] = {"dir": inner28, "project": "28_inner"}
            refused("1.6", os.path.join(td, "out16", "1.6_live"), "move", 409, "holds version 2.8's folder")
            assert relocate_check("1.6", stack_b, "repoint")[1] is None, "a repoint moves nothing"
            VERSIONS["2.8"] = {"dir": s28, "project": "28_live"}
            g["HOTPATCH_BUNDLE_DIR"] = os.path.join(s16, "bundles")
            refused("1.6", os.path.join(td, "out16", "1.6_live"), "move", 409, "contains the hotpatch bundle folder")
            g["HOTPATCH_BUNDLE_DIR"] = os.path.join(agent_d, "hotpatch-bundles")
            # ... and the NEXT start's mirror counts as much as the running one (a GIO_HOTPATCH_DIR saved, restart owed)
            write_text(cfg, cfg_text + "GIO_HOTPATCH_DIR=%s\n" % os.path.join(td, "mirror-next"))
            refused("1.6", os.path.join(td, "mirror-next", "1.6_live"), "repoint", 400,
                    "the hotpatch mirror (after the agent's restart)")
            write_text(cfg, cfg_text)
            # a mount point cannot be replaced by the move (the copy would land on the parent's filesystem first)
            g["_is_mount"] = lambda p_: _same_path(p_, new16)
            rep = refused("1.6", new16, "move", 409, "is a mount point")
            assert os.path.join(new16, "1.6_live") in rep["problem"], rep
            assert relocate_check("1.6", new16, "repoint")[1] is None, "a repoint at a mounted, empty folder is fine"
            g["_is_mount"] = lambda _p: False
            # another compose project (running or stopped) already carries the new folder's project name: docker
            # would take the two for one; the version's own name and a project living in that very folder are fine
            all_projects[:] = [("mailstack", os.path.join(td, "elsewhere", "docker-compose.yml"))]
            rep = refused("1.6", os.path.join(td, "x", "mailstack"), "repoint", 409, "another compose project")
            assert "mailstack" in rep["problem"] and "elsewhere" in rep["problem"], rep
            refused("1.6", os.path.join(td, "x", "mailstack"), "move", 409, "another compose project")
            all_projects[:] = [("16_b", os.path.join(stack_b, "docker-compose.yml")),
                               ("16_live", os.path.join(td, "old", "docker-compose.yml"))]
            assert relocate_check("1.6", stack_b, "repoint")[1] is None, "the project that lives in the new folder"
            assert relocate_check("1.6", os.path.join(td, "later2", "1.6_live"), "repoint")[1] is None, "its own name"
            ls_fail.append(1)
            assert relocate_check("2.8", os.path.join(td, "x", "mailstack"), "repoint")[0]["problem"].startswith(
                "Cannot read the docker state"), "docker mute: the stack that may run is refused as before"
            ls_fail.clear()
            del all_projects[:]
            ok("relocation checks OK (relative / UNC / quotes / backslash / root / no project name / same folder / "
               "project collision / nesting with the other version or the agent's folders / file / non-empty target "
               "/ env override / not writable / docker mute / busy; the check reply, same volume vs measured copy; "
               "line separators + surrogates + length; Windows drive types; a move out of a folder holding the other "
               "server or an agent folder; the next start's mirror; a mount point; a foreign compose project name)")

            # (1) move on the same volume, the stack running: down under the OLD project (full grace, runningOk off
            #     first), one rename, the fetch leftovers follow, GIO_DIR_16 rewritten in place, started again
            write_text(s16 + ".7z.part", "partial download")
            stale = os.path.join(os.path.dirname(s16), ".relic-extract-1.6_live")
            os.makedirs(stale)
            del calls[:], starts[:]
            jm = _Job()
            res = do_relocate("1.6", jm, new16 + os.sep, "move")
            assert res == {"version": "1.6", "from": s16, "to": new16, "mode": "move", "moved": True, "crossVolume": False,
                           "bytes": None, "restarted": True, "stateReset": False, "restartError": None}, res
            assert calls == [(s16, ("down", "--remove-orphans"), False)], calls
            assert starts == [("1.6", "never", new16)] and running == {"16_moved"}, (starts, running)
            assert VERSIONS["1.6"] == {"dir": new16, "project": "16_moved"}, VERSIONS["1.6"]
            assert not os.path.exists(s16) and _tree_counts(new16) == totals16
            assert read_text(new16 + ".7z.part") == "partial download" and not os.path.exists(s16 + ".7z.part")
            assert not os.path.exists(stale)
            assert read_text(cfg) == cfg_text.replace("GIO_DIR_16=%s\n" % s16, "GIO_DIR_16=%s\n" % new16), read_text(cfg)
            vs = version_state("1.6")
            assert {k: vs.get(k) for k in full_vs if k != "runningOk"} == \
                {k: x for k, x in full_vs.items() if k != "runningOk"} and vs["runningOk"] is True, vs
            assert protected == ([os.path.join(new16, "creds.txt")] if IS_WINDOWS else []), protected
            assert any("one rename" in l for l in jm.lines) and any("Configuration saved" in l for l in jm.lines), jm.lines
            assert not syncs, "a rename copies nothing -- nothing to flush"
            # (2) move across volumes (forced), the stack stopped but installed: its containers still go down, the
            #     tree is copied, verified, renamed into place, and the old copy deleted after the config write
            g["_same_volume"] = lambda a_, b_: False
            running.clear()
            del calls[:], starts[:], protected[:]
            if os.name != "nt":
                os.chmod(os.path.join(new16, "creds.txt"), 0o600)
            cross16 = os.path.join(td, "cross", "1.6_live")
            before = _tree_counts(new16)
            jc = _Job()
            res = do_relocate("1.6", jc, cross16, "move")
            assert (res["moved"], res["crossVolume"], res["bytes"], res["restarted"], res["to"]) == \
                (True, True, before[2], False, cross16), res
            assert calls == [(new16, ("down", "--remove-orphans"), False)] and starts == [], (calls, starts)
            assert not os.path.exists(new16) and _tree_counts(cross16) == before
            assert os.path.isdir(os.path.join(cross16, "mysql", "relic_tpl_gaa"))
            assert read_text(os.path.join(cross16, "redis", "dump.rdb")) == "REDIS0009"
            if os.name != "nt":
                assert os.stat(os.path.join(cross16, "creds.txt")).st_mode & 0o777 == 0o600, "cp -a keeps the mode"
            assert sorted(os.listdir(os.path.dirname(cross16))) == ["1.6_live", "1.6_live.7z.part"], \
                os.listdir(os.path.dirname(cross16))  # no .relic-move-* left, the .part followed
            assert VERSIONS["1.6"]["dir"] == cross16 and "GIO_DIR_16=%s\n" % cross16 in read_text(cfg)
            assert any("Copy verified" in l for l in jc.lines) and any("Deleting the old copy" in l for l in jc.lines)
            # the copy is flushed to disk BEFORE it is verified, named in the config and the old tree deleted
            assert syncs == [1], syncs
            flush_at = next(i for i, l in enumerate(jc.lines) if "Flushing the copy" in l)
            assert flush_at < next(i for i, l in enumerate(jc.lines) if "Configuration saved" in l) < \
                next(i for i, l in enumerate(jc.lines) if "Deleting the old copy" in l), jc.lines
            # (3) the copy helpers themselves: counts equal, links kept (POSIX), a stop request honoured
            probe = os.path.join(td, "probe")
            src_p = os.path.join(probe, "src")
            make_stack(src_p)
            if os.name != "nt":
                os.symlink("ibdata1", os.path.join(src_p, "mysql", "link"))
                os.chmod(os.path.join(src_p, "creds.txt"), 0o600)
            for fn in [_copy_tree_py] + ([_copy_tree_cp] if os.name != "nt" else []):
                dst_p = os.path.join(probe, fn.__name__)
                fn(src_p, dst_p, _Job(), lambda: False, 0)
                assert _tree_counts(dst_p) == _tree_counts(src_p), fn.__name__
                if os.name != "nt":
                    assert os.readlink(os.path.join(dst_p, "mysql", "link")) == "ibdata1", fn.__name__
            if os.name != "nt":
                assert os.stat(os.path.join(probe, "_copy_tree_cp", "creds.txt")).st_mode & 0o777 == 0o600
            try:
                _copy_tree_py(src_p, os.path.join(probe, "stopped"), _Job(), lambda: True, 0)
                raise AssertionError("a stop request must stop the copy")
            except RelocateStopped:
                pass
            # the Windows walk: every file's data fsync'ed before its times + read-only bit are applied (a read-only
            # source copies fine and stays read-only)
            ro_src = os.path.join(probe, "ro_src")
            make_stack(ro_src)
            ro_file = os.path.join(ro_src, "creds.txt")
            os.chmod(ro_file, 0o444)
            fsyncs, saved_fsync = [], os.fsync
            os.fsync = lambda fd_: (fsyncs.append(fd_), saved_fsync(fd_))[1]
            try:
                _copy_tree_py(ro_src, os.path.join(probe, "ro_dst"), _Job(), lambda: False, 0)
            finally:
                os.fsync = saved_fsync
            ro_files = _tree_counts(ro_src)[0]
            assert len(fsyncs) == ro_files and _tree_counts(os.path.join(probe, "ro_dst")) == _tree_counts(ro_src), \
                (len(fsyncs), ro_files)
            ro_copy = os.path.join(probe, "ro_dst", "creds.txt")
            assert read_text(ro_copy) == read_text(ro_file) and not os.stat(ro_copy).st_mode & 0o222, "read-only kept"
            assert int(os.stat(ro_copy).st_mtime) == int(os.stat(ro_file).st_mtime), "times kept"
            for p_ in (ro_file, ro_copy):
                os.chmod(p_, 0o644)  # the temp dir must be deletable
            # (4) a stop request during the copy: the temp copy goes, the admin's empty folder stays, the config
            #     and the old tree are untouched, and the stack that was running is started again where it was
            running.add("16_live")
            del calls[:], starts[:]

            def stopping_copy(src, dst, job_, should_stop, total):
                os.makedirs(os.path.join(dst, "mysql"))
                write_text(os.path.join(dst, "mysql", "half"), "x")
                _relocate_cancel.set()
                assert should_stop()
                raise RelocateStopped("stopped on request")

            g["_copy_tree"] = stopping_copy
            cancel16 = os.path.join(td, "cancel", "1.6_live")
            os.makedirs(cancel16)
            cfg_before = read_text(cfg)
            try:
                do_relocate("1.6", _Job(), cancel16, "move")
                raise AssertionError("a stopped copy must fail the job")
            except AgentError as e:
                assert e.status == 409 and e.message.startswith("Stopped on request"), e.message
            assert os.listdir(os.path.dirname(cancel16)) == ["1.6_live"] and not os.listdir(cancel16), "no .relic-move-*"
            assert _tree_counts(cross16) == before and VERSIONS["1.6"]["dir"] == cross16 and read_text(cfg) == cfg_before
            assert calls == [(cross16, ("down", "--remove-orphans"), False)] and starts == [("1.6", "never", cross16)]
            # (5) a stop request while the stack goes down: nothing moved, started again
            cancel_on_down.append(1)
            del calls[:], starts[:]
            try:
                do_relocate("1.6", _Job(), cancel16, "move")
                raise AssertionError("a stop during the down must fail the job")
            except AgentError as e:
                assert e.status == 409 and "nothing was moved or changed" in e.message, e.message
            cancel_on_down.clear()
            assert starts == [("1.6", "never", cross16)] and _tree_counts(cross16) == before and read_text(cfg) == cfg_before
            # (5b) the down itself fails (compose exits 1 -- a network with a foreign endpoint, say): runningOk is off
            #      already and no watchdog would revive the server, so a server that ran is started again, and the
            #      error says so; one that was not running stays down with nothing moved
            down_rc.append(1)
            del calls[:], starts[:]
            try:
                do_relocate("1.6", _Job(), cancel16, "move")
                raise AssertionError("a failed down must fail the job")
            except AgentError as e:
                assert e.status == 500 and "down failed (code 1) -- nothing was moved." in e.message \
                    and "stopped for the move; it was started again" in e.message, e.message
            assert starts == [("1.6", "never", cross16)] and _tree_counts(cross16) == before and read_text(cfg) == cfg_before
            assert VERSIONS["1.6"]["dir"] == cross16 and version_state("1.6")["runningOk"] is True
            running.clear()
            del calls[:], starts[:]
            try:
                do_relocate("1.6", _Job(), cancel16, "move")
                raise AssertionError("a failed down must fail the job")
            except AgentError as e:
                assert e.status == 500 and e.message == "docker compose down failed (code 1) -- nothing was moved.", \
                    e.message
            assert starts == [] and calls == [(cross16, ("down", "--remove-orphans"), False)], (starts, calls)
            down_rc.clear()
            running.add("16_live")
            # (6) a copy that does not match: 500, nothing left behind
            def lossy_copy(src, dst, job_, should_stop, total):
                saved_r["_copy_tree"](src, dst, job_, should_stop, total)
                os.remove(os.path.join(dst, "redis", "dump.rdb"))

            g["_copy_tree"] = lossy_copy
            del calls[:], starts[:]
            try:
                do_relocate("1.6", _Job(), cancel16, "move")
                raise AssertionError("an unverified copy must fail the job")
            except AgentError as e:
                assert e.status == 500 and "does not match the original" in e.message, e.message
            assert os.listdir(os.path.dirname(cancel16)) == ["1.6_live"] and not os.listdir(cancel16)
            assert _tree_counts(cross16) == before and read_text(cfg) == cfg_before and starts == [("1.6", "never", cross16)]
            g["_copy_tree"] = saved_r["_copy_tree"]
            # (7) the config write fails: a rename is undone, a copy deleted -- the old tree and config untouched
            def failing_save(updates, explicit_empty=()):
                raise AgentError(500, "Saving %s failed: disk full (selftest)" % cfg)

            g["save_config_values"] = failing_save
            running.clear()
            del calls[:], starts[:]
            rb = os.path.join(td, "rb", "1.6_rb")
            for same_, needle in ((True, "moved back"), (False, "copy was deleted")):
                g["_same_volume"] = (lambda a_, b_: True) if same_ else (lambda a_, b_: False)
                try:
                    do_relocate("1.6", _Job(), rb, "move")
                    raise AssertionError("a failed config write must fail the move")
                except AgentError as e:
                    assert e.status == 500 and needle in e.message and "disk full" in e.message, e.message
                assert not os.path.exists(rb) and _tree_counts(cross16) == before and VERSIONS["1.6"]["dir"] == cross16
            assert read_text(cfg) == cfg_before and starts == [] and os.path.isfile(cross16 + ".7z.part")
            g["save_config_values"], g["_same_volume"] = saved_r["save_config_values"], saved_r["_same_volume"]
            ok("relocation moves OK (same volume: down under the old project, one rename, siblings follow, config "
               "rewritten in place, started again; forced cross volume: copy verified, old deleted after the config "
               "write, flushed to disk before either; copy helpers (every file fsync'ed, read-only kept); a stop "
               "during the copy or the down, a failed down, an unverified copy and a failed config write leave the "
               "old tree + config untouched and restart the stack that ran)")

            # (8) repoint at ANOTHER server while running: no file touched, the record starts over (the admin's
            #     choices kept, passwordVerify read from its sdk config, provisioning from its marker)
            version_state_set("1.6", **full_vs)
            running.add("16_live")
            del calls[:], starts[:]
            res = do_relocate("1.6", _Job(), stack_b, "repoint")
            assert res == {"version": "1.6", "from": cross16, "to": stack_b, "mode": "repoint", "moved": False,
                           "crossVolume": False, "bytes": None, "restarted": True, "stateReset": True,
                           "restartError": None}, res
            assert calls == [(cross16, ("down", "--remove-orphans"), False)] and starts == [("1.6", "never", stack_b)]
            assert _tree_counts(cross16) == before and os.path.isfile(cross16 + ".7z.part"), "a repoint touches no file"
            assert version_state("1.6") == {"runningOk": True, "pathfinding": False, "passwordVerify": True,
                                            "hotpatch": {"enabled": True, "voice": ["Japanese"], "source": "bundle",
                                                         "appliedAt": "t", "pending": True}}, version_state("1.6")
            rec = provision_record("1.6")
            assert (rec["provisionedAt"], rec["progress"], rec["defaultAccount"]) == ("2026-07-07 07:07:07", "keep", False), rec
            assert VERSIONS["1.6"] == {"dir": stack_b, "project": "16_b"} and "GIO_DIR_16=%s\n" % stack_b in read_text(cfg)
            # (9) its tree moved by hand while it ran: re-pointing at a folder with no compose file is refused
            #     before anything changes; re-pointing at the moved tree brings the project down BY NAME with the
            #     moved compose file and keeps the record (the marker says what the state says)
            hand = os.path.join(td, "hand", "1.6_b2")
            os.makedirs(os.path.dirname(hand))
            os.rename(stack_b, hand)
            version_state_set("1.6", provisionedAt="2026-07-07 07:07:07", progress="keep", defaultAccount=False,
                              txtFixes="applied")
            del calls[:], starts[:]
            later = os.path.join(td, "later", "1.6_live")
            try:
                do_relocate("1.6", _Job(), later, "repoint")
                raise AssertionError("a running project without its compose file must be refused")
            except AgentError as e:
                assert e.status == 409 and "no longer holds its docker-compose.yml" in e.message, e.message
            assert calls == [] and VERSIONS["1.6"]["dir"] == stack_b and version_state("1.6")["runningOk"] is True
            res = do_relocate("1.6", _Job(), hand, "repoint")
            assert (res["stateReset"], res["restarted"]) == (False, True), res
            assert calls == [(hand, ("-p", "16_b", "down", "--remove-orphans"), False)], calls
            assert starts == [("1.6", "never", hand)] and running == {"16_b2"}, (starts, running)
            vs = version_state("1.6")
            assert vs["txtFixes"] == "applied" and vs["provisionedAt"] == "2026-07-07 07:07:07", vs
            # (10) repoint "": the version leaves the agent -- "GIO_DIR_16=" written, files and record untouched
            del calls[:], starts[:]
            res = do_relocate("1.6", _Job(), "", "repoint")
            assert (res["to"], res["restarted"], res["stateReset"]) == ("", False, False), res
            assert calls == [(hand, ("down", "--remove-orphans"), False)] and starts == [], (calls, starts)
            assert VERSIONS["1.6"] == {"dir": "", "project": ""} and not is_configured("1.6")
            assert "\nGIO_DIR_16=\n" in read_text(cfg) and read_config_values()["GIO_DIR_16"] == ""
            assert os.path.isfile(os.path.join(hand, "docker-compose.yml.tmpl")) and version_state("1.6")["txtFixes"] == "applied"
            # (11) an unconfigured version pointed at an absent folder: nothing stopped, no folder created (the
            #      download creates it), the record starts over
            del calls[:]
            res = do_relocate("1.6", _Job(), later, "repoint")
            assert (res["stateReset"], res["from"], res["to"]) == (True, "", later) and calls == [], (res, calls)
            assert not os.path.exists(os.path.dirname(later)) and VERSIONS["1.6"]["dir"] == later
            assert version_state("1.6") == {"runningOk": False, "pathfinding": False,
                                            "hotpatch": {"enabled": True, "voice": ["Japanese"], "source": "bundle",
                                                         "appliedAt": "t", "pending": True}}, version_state("1.6")

            # (12) the routes: 401, the dry run (a refusal is its `problem`), the real request's 400/409, cancel, 202
            def call(method, path, body=None, token="selftest-token"):
                reply = []
                raw = b"" if body is None else json.dumps(body).encode()
                hh = Handler.__new__(Handler)
                hh.path, hh.rfile = path, io.BytesIO(raw)
                hh.headers = {"Authorization": "Bearer " + token, "Content-Length": str(len(raw))}
                hh._send = lambda status_, obj, retry_after=None: reply.append((status_, obj))
                getattr(hh, "do_" + method)()
                assert len(reply) == 1, reply
                return reply[0]

            body = {"version": "1.6", "dir": hand, "mode": "repoint"}
            assert call("POST", "/server/relocate", body, token="wrong")[0] == 401
            st_, o_ = call("POST", "/server/relocate", dict(body, check=True))
            assert st_ == 200 and (o_["ok"], o_["target"], o_["from"], o_["to"]) == (True, "stack", later, hand), o_
            st_, o_ = call("POST", "/server/relocate", {"version": "1.6", "dir": "rel", "mode": "move", "check": True})
            assert st_ == 200 and o_["ok"] is False and o_["problem"].startswith("The folder must be"), o_
            st_, o_ = call("POST", "/server/relocate", {"version": "1.6", "dir": "rel", "mode": "move"})
            assert st_ == 400 and o_["error"].startswith("The folder must be"), o_
            for body_, needle in (({"version": "1.6", "mode": "repoint"}, "missing/invalid field: 'dir'"),
                                  ({"version": "1.6", "dir": hand, "mode": "copy"}, "mode must be"),
                                  ({"version": "1.6", "dir": hand}, "mode must be"),
                                  ({"version": "9.9", "dir": hand, "mode": "repoint"}, "Unknown version"),
                                  ({"version": ["1.6"], "dir": hand, "mode": "repoint"}, "version must be a string")):
                st_, o_ = call("POST", "/server/relocate", body_)
                assert st_ == 400 and needle in o_["error"], (body_, st_, o_)
            assert call("POST", "/server/relocate", {"version": "1.6", "cancel": True}) == (200, {"ok": True, "stopping": False})
            with _jobs_lock:
                JOBS[fake_reloc.id] = fake_reloc
                JOBS_ORDER.append(fake_reloc.id)
            st_, o_ = call("POST", "/server/relocate", body)
            assert st_ == 409 and o_["error"].startswith("Another operation is already running"), o_
            assert call("POST", "/server/relocate", {"version": "2.8", "cancel": True}) == (200, {"ok": True, "stopping": False})
            assert not _relocate_cancel.is_set()
            assert call("POST", "/server/relocate", {"version": "1.6", "cancel": True}) == (200, {"ok": True, "stopping": True})
            assert _relocate_cancel.is_set()
            _relocate_cancel.clear()
            with _jobs_lock:
                JOBS.pop(fake_reloc.id, None)
                JOBS_ORDER.remove(fake_reloc.id)
            del calls[:], starts[:]
            st_, o_ = call("POST", "/server/relocate", body)
            assert st_ == 202 and (o_["ok"], o_["async"], o_["kind"], o_["version"]) == (True, True, "relocate", "1.6"), o_
            route_jobs.append(o_["job"])
            jr_ = JOBS[o_["job"]]
            for _ in range(400):
                if jr_.state != "running":
                    break
                saved_sleep_r(0.025)
            assert jr_.state == "done" and jr_.result["to"] == hand and jr_.result["stateReset"] is True, (jr_.state, jr_.error)
            assert VERSIONS["1.6"] == {"dir": hand, "project": "16_b2"} and "GIO_DIR_16=%s\n" % hand in read_text(cfg)
            assert calls == [] and starts == [], "not running, not installed at the old folder: nothing stopped or started"
            # (13) repoint at an INSTALLED stack without any provisioning record (a vendor bootstrap.sh stack, a copy
            #      from before agent 3.2): nothing could re-seed the record, so automatic provisioning is switched off
            #      for it -- the next Start must not import the save over a database that may hold players; only an
            #      explicit provision clears it, and a fresh bootstrap (the database wiped) too
            nomark = os.path.join(td, "vendor", "1.6_vendor")
            make_stack(nomark)
            g["read_manifest"] = lambda _v: {"files": []}  # this version ships a payload: auto-provision is possible
            del calls[:], starts[:]
            j13 = _Job()
            res = do_relocate("1.6", j13, nomark, "repoint")
            assert res["stateReset"] is True and version_state("1.6").get("autoProvision") is False, \
                version_state("1.6")
            assert any("Automatic provisioning is OFF" in l for l in j13.lines), j13.lines
            assert read_provisioned_marker("1.6") is None and not version_state("1.6").get("provisionedAt")
            j13 = _Job()
            assert needs_provision("1.6", j13) is False, j13.lines
            assert any("automatic provisioning is off" in l for l in j13.lines), j13.lines
            version_state_set("1.6", autoProvision=None)  # what do_provision's final record write does
            assert needs_provision("1.6") is True, "without the flag the same stack would be imported at the next Start"
            version_state_set("1.6", autoProvision=False)
            forget_provisioned("1.6", _Job())
            assert version_state("1.6").get("autoProvision") is None, "a fresh bootstrap wipes what the flag protected"
            # a repoint at a folder that is not installed (absent / empty) or carries a marker sets no flag
            res = do_relocate("1.6", _Job(), os.path.join(td, "later3", "1.6_live"), "repoint")
            assert res["stateReset"] is True and "autoProvision" not in version_state("1.6"), version_state("1.6")
            g["read_manifest"] = lambda _v: None
        finally:
            for jid in route_jobs:  # never restore the real docker helpers under a job still running on fakes
                for _ in range(400):
                    if jid not in JOBS or JOBS[jid].state != "running":
                        break
                    saved_sleep_r(0.025)
            time.sleep = saved_sleep_r
            with _jobs_lock:
                for jid in [fake_reloc.id] + route_jobs:
                    JOBS.pop(jid, None)
                    if jid in JOBS_ORDER:
                        JOBS_ORDER.remove(jid)
            g.update(saved_r)
            for v, meta in saved_meta.items():
                VERSIONS[v] = meta
            _relocate_cancel.clear()
            _invalidate_public_status()
    ok("relocation repoint + routes OK (another server: no file touched, record reset to the admin's choices + "
       "passwordVerify from its sdk + provisioning from its marker; a tree moved by hand: refused without its compose "
       "file, else down by name and the record kept; \"\" = GIO_DIR_16= and nothing else; an absent folder is not "
       "created; 401, dry run problems, 400s, busy 409, cancel only for that version's job, 202 + the job; an "
       "installed stack without a record: automatic provisioning off until an explicit provision / fresh bootstrap)")
    if box_cfg:
        assert _box_state_bytes(box_cfg) == box_cfg_before, "the selftest modified the box config %s" % box_cfg
    assert CONFIG_PATH == box_cfg and not _restarting

    # The isolation above is load-bearing, so it is a CHECK, not a convention: a block that
    # restores STATE_PATH to the wrong value, or a future check that reads a record, must fail
    # here and not on a stranger's box during an upgrade. The box's own file is re-read as well:
    # byte-identical (or still absent) is the only acceptable outcome of a selftest.
    assert STATE_PATH.startswith(_state_scratch), \
        "the selftest left STATE_PATH at %s -- it must never read or write the box state" % STATE_PATH
    assert _box_state_bytes(_box_state) == _box_state_before, \
        "the selftest modified the box state file %s" % _box_state
    ok("the box state file is never an input and never written (GIO_STATE_PATH neutralised)")
    _rmtree_quiet(_state_scratch)  # a failed run leaves it for inspection; the OS clears /tmp
    print("selftest OK -- %d checks passed  sign=%s...  url=%s..." % (checks, sign[:12], url[:78]))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--fetch" in sys.argv:
        # A CLI client of the RUNNING agent (install_agent.sh / deploy_from_windows.ps1 after an
        # install): never runs the job in this process.
        sys.exit(fetch_cli(_cli_list("--fetch")))
    host, port = LISTEN_HOST, LISTEN_PORT  # parsed once at import (parse_listen), see above
    _tool = find_extractor()
    log_line(f"gio-agent {AGENT_VERSION} listening on {host}:{port} (platform="
             f"{'windows' if IS_WINDOWS else 'linux'}, region={REGION}, muip={MUIP_HOST or 'derived'}, "
             f"advertised={(ADVERTISED_HOST + ' (followed)') if ADVERTISED_HOST else (ADVERTISED_IP or 'n/a')}, "
             f"provision={PROVISION_MODE}, "
             f"config={CONFIG_FILE or ((CONFIG_PATH + ' (EnvironmentFile)') if CONFIG_PATH else 'env')}, "
             f"state={STATE_PATH}, name={SERVER_NAME}, "
             f"hotpatch={HOTPATCH_DIR} url={HOTPATCH_URL or 'derived'} upstream={'on' if HOTPATCH_UPSTREAM else 'off'} source={hotpatch_source()}" + ("" if hotpatch_source() == HOTPATCH_SOURCE else f" (config says {HOTPATCH_SOURCE})") + f" "
             f"muipKey={'config' if MUIP_KEY else 'random'} pathfindingDefault={'on' if PATHFINDING_DEFAULT else 'off'} "
             f"bundles={'on' if HOTPATCH_BUNDLES else 'off'} extractor={os.path.basename(_tool[0]) if _tool else 'none'})")
    for v, meta in VERSIONS.items():
        log_line("  %s -> %s  present=%s bootstrapped=%s payload=%s hotpatch=%s"
                 % (v, meta["dir"] or "(not configured)", is_present(v),
                    is_present(v) and is_bootstrapped(v), read_manifest(v) is not None,
                    read_hotpatch_manifest(v) is not None))
    if not TOKEN:
        log_line("gio-agent: WARNING: GIO_AGENT_TOKEN is empty -- every admin endpoint answers 401.")
    # Before anything can read or write the state: an unreadable file is set aside and its .bak
    # restored (or left in place, degraded), then stacks provisioned before 3.2 get their marker.
    log_line("gio-agent: state file: %s" % recover_state_at_startup())
    migrate_provisioned_markers()
    try:
        if not isinstance(load_state().get("policy"), dict):
            # Loud once per start: a box whose admin never pressed Apply policy follows the 3.4
            # defaults (signup + player commands on) -- the Player accounts card turns them off.
            log_line("gio-agent: policy: defaults (signup on, player commands on) -- no policy stored yet.")
    except StateUnreadable:
        pass
    if _ADVERTISED_BOTH_SET:
        log_line("gio-agent: WARNING: both GIO_ADVERTISED_HOST and GIO_ADVERTISED_IP are set -- following "
                 "the host %s, the IP %s is ignored." % (ADVERTISED_HOST, os.environ.get("GIO_ADVERTISED_IP", "").strip()))
    if ADVERTISED_HOST:
        # Before anything serves: the address the first Start / ensure_bind_ip / hotpatch URL use.
        log_line("gio-agent: advertised host: %s" % resolve_advertised_at_boot())
    # No request of this process may follow the WINDOWS system proxy: on a PC that also plays,
    # that proxy is the launcher's Fiddler, whose rules send *.yuanshen.com (the hotpatch
    # upstreams) to the private sdk -- and urllib snapshots the proxy table at its first request.
    # Explicit *_proxy environment variables still apply, on every platform. The same opener carries
    # the TLS context (build_tls_context: full verification without VERIFY_X509_STRICT, + GIO_CA_FILE).
    install_http_opener()
    if CA_FILE or not TLS_PINNED_FALLBACK:
        log_line("gio-agent: TLS: extra roots=%s, pinned-download fallback=%s"
                 % (("%s (NOT loaded: %s)" % (CA_FILE, _TLS_CA_ERROR)) if _TLS_CA_ERROR else (CA_FILE or "none"),
                    "on" if TLS_PINNED_FALLBACK else "off"))
    write_pid_file()  # Windows + --config: the launcher's LocalAgent reads it
    threading.Thread(target=tower_watchdog, daemon=True).start()
    threading.Thread(target=service_watchdog, daemon=True).start()
    if ADVERTISED_HOST:
        threading.Thread(target=advertise_watchdog, daemon=True).start()
    _HTTPD = bind_http_server(host, port)  # retries "address in use" ~10 s: a restarted agent's predecessor
    _HTTPD.serve_forever()
    # Only POST /agent/restart stops serve_forever: its worker ends this process (or, when no new one
    # could be started, serves again itself). The main thread must never run into interpreter shutdown
    # under it.
    while True:
        time.sleep(3600)
