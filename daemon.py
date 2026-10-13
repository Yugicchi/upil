#!/usr/bin/env python3
"""Upil daemon.

Like calibrate.py (trigger-gated read, latched zone, 20Hz accumulator),
but the output drives real backends, picked per subsystem in config:
  volume     -> PipeWire/wpctl,
  brightness -> brightnessctl/sysfs,
  seek       -> MPRIS D-Bus directly.

Default is DRY-RUN (prints commands only). Use --live to act for real.
The touchpad grab defaults ON in --live mode (disable with --no-grab).

Examples:
    python3 daemon.py                      # dry-run, observe
    python3 daemon.py --live               # act + grab
    python3 daemon.py --live --no-grab     # act without grab
"""
import argparse
import re
import subprocess
import sys
import threading
import time

from evdev import ecodes
from select import select

from calibrate import abs_info, find_keyboards, find_touchpads


def detect_backlight():
    """First backlight from sysfs. Portable: no tools, no DE daemon needed.

    Returns the sysfs name (for example 'intel_backlight'), or None.
    """
    import os
    try:
        entries = sorted(os.listdir("/sys/class/backlight"))
    except OSError:
        return None
    return entries[0] if entries else None


def require_backlight(dry_run):
    """Fail fast when no controllable backlight exists. The brightness driver
    (brightnessctl) uses its default device; multi-device selection is future
    work. Returns the detected name for the log."""
    name = detect_backlight()
    if name is None and not dry_run:
        print("[backend] no backlight in /sys/class/backlight. "
              "External monitors without DDC are not supported.", file=sys.stderr)
        sys.exit(1)
    return name or "none(dry-run)"


def have_tool(name):
    import shutil
    return shutil.which(name) is not None


class VolumeBackend:
    name = "?"
    available = True

    def get(self):
        raise NotImplementedError

    def set(self, pct):
        raise NotImplementedError


class PipeWireVolume(VolumeBackend):
    """Volume via PipeWire (wpctl). Works on any desktop: i3, XFCE, Plasma."""
    name = "pipewire"
    SINK = "@DEFAULT_AUDIO_SINK@"

    def __init__(self, dry_run):
        self.dry_run = dry_run

    def get(self):
        if self.dry_run:
            return 65.0
        p = subprocess.run(["wpctl", "get-volume", self.SINK],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"([0-9.]+)", p.stdout)
        return float(m.group(1)) * 100 if m else 65.0

    def set(self, pct):
        if self.dry_run:
            print(f"[dry-run] wpctl set-volume {self.SINK} {pct / 100:.3f}", flush=True)
            return
        subprocess.run(["wpctl", "set-volume", self.SINK, f"{pct / 100:.3f}"],
                       capture_output=True, timeout=5)


class BrightnessBackend:
    name = "?"
    available = True

    def get(self):
        raise NotImplementedError

    def set(self, pct):
        raise NotImplementedError


class BrightnessctlBrightness(BrightnessBackend):
    """Brightness via brightnessctl (sysfs). Works on any desktop."""
    name = "brightnessctl"

    def __init__(self, dry_run):
        self.dry_run = dry_run

    def get(self):
        if self.dry_run:
            return 8.0
        try:
            cur = int(subprocess.run(["brightnessctl", "g"], capture_output=True,
                                     text=True, timeout=5).stdout.strip())
            mx = int(subprocess.run(["brightnessctl", "m"], capture_output=True,
                                    text=True, timeout=5).stdout.strip())
            return cur / mx * 100 if mx else 8.0
        except Exception:
            return 8.0

    def set(self, pct):
        if self.dry_run:
            print(f"[dry-run] brightnessctl s {pct:.1f}%", flush=True)
            return
        subprocess.run(["brightnessctl", "-q", "s", f"{pct:.1f}%"],
                       capture_output=True, timeout=5)


class DisabledBackend:
    """A subsystem whose tool is missing. Auto mode degrades to this instead
    of killing the daemon; explicit requests still abort (typos must be loud).
    Works for volume and brightness (both have get/set + available)."""
    available = False

    def __init__(self, missing):
        self.name = f"DISABLED({missing})"
        self.missing = missing

    def get(self):
        return 0.0

    def set(self, pct):
        pass


class AsyncBackend:
    """Wraps a backend so a slow tool never blocks the input loop.

    The loop samples at throttle.hz and post()s every value; the worker
    thread sends at tool speed, latest value wins, intermediate values drop.
    (Measured: wpctl ~80ms/call, brightnessctl ~30ms — both slower than a
    50ms tick, so blocking would rubber-band the gesture.)
    """
    available = True

    def __init__(self, back):
        self.back = back
        self.name = back.name
        self.available = back.available
        self._lock = threading.Lock()
        self._latest = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __getattr__(self, item):
        # Forward anything else (for example .missing) to the wrapped backend.
        # Note: attributes set in __init__ are found first, so no recursion.
        return getattr(self.__dict__["back"], item)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True

    def get(self):
        return self.back.get()

    def post(self, pct):
        with self._lock:
            self._latest = pct

    def _run(self):
        while not self._stop:
            with self._lock:
                val, self._latest = self._latest, None
            if val is None:
                time.sleep(0.01)
                continue
            try:
                self.back.set(val)
            except Exception as e:
                print(f"[backend] error: {e}", flush=True)


def pick_volume(driver, dry_run):
    """driver = auto|pipewire. auto = pipewire when wpctl exists, else DISABLED."""
    if driver not in ("auto", "pipewire"):
        print(f"[backend] unknown volume driver: {driver} (auto|pipewire)",
              file=sys.stderr)
        sys.exit(1)
    if not have_tool("wpctl") and not dry_run:
        if driver == "pipewire":
            print("[backend] wpctl is required, aborting.", file=sys.stderr)
            sys.exit(1)
        print("[backend] volume DISABLED (wpctl not found), daemon keeps running",
              flush=True)
        return DisabledBackend("wpctl"), "DISABLED(wpctl)"
    if not dry_run:
        print("[backend] volume -> pipewire", flush=True)
    return PipeWireVolume(dry_run), "pipewire"


def pick_brightness(driver, dry_run):
    """driver = auto|brightnessctl. auto = brightnessctl when present, else DISABLED."""
    if driver not in ("auto", "brightnessctl"):
        print(f"[backend] unknown brightness driver: {driver} (auto|brightnessctl)",
              file=sys.stderr)
        sys.exit(1)
    if not have_tool("brightnessctl") and not dry_run:
        if driver == "brightnessctl":
            print("[backend] brightnessctl is required, aborting.", file=sys.stderr)
            sys.exit(1)
        print("[backend] brightness DISABLED (brightnessctl not found), daemon keeps running",
              flush=True)
        return DisabledBackend("brightnessctl"), "DISABLED(brightnessctl)"
    if not dry_run:
        print("[backend] brightness -> brightnessctl", flush=True)
    return BrightnessctlBrightness(dry_run), "brightnessctl"


def mpris_seek(seconds, dry_run, player=None, player_name="", verbose=0):
    """Relative seek: try `Seek` first, fall back to absolute `SetPosition`.

    Returns True if either worked (or dry-run).
    """
    us = int(seconds * 1_000_000)
    if dry_run:
        print(f"[dry-run] MPRIS Seek {seconds:+.1f}s ({us:+d}us)", flush=True)
        return True
    if player is None:
        return False
    try:
        import dbus
        iface = dbus.Interface(player, "org.mpris.MediaPlayer2.Player")
        try:
            iface.Seek(dbus.Int64(us))
            if verbose >= 1:
                print(f"[mpris] Seek {seconds:+.1f}s -> {player_name}", flush=True)
            return True
        except dbus.DBusException as e:
            print(f"[mpris] Seek unsupported ({e}), trying SetPosition", flush=True)
        props = dbus.Interface(player, "org.freedesktop.DBus.Properties")
        meta = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
        trackid = meta.get("mpris:trackid")
        pos = int(props.Get("org.mpris.MediaPlayer2.Player", "Position"))
        newpos = max(0, pos + us)
        iface.SetPosition(trackid, dbus.Int64(newpos))
        if verbose >= 1:
            print(f"[mpris] SetPosition {newpos // 1_000_000}s -> {player_name}", flush=True)
        return True
    except dbus.DBusException as e:
        print(f"[mpris] Seek failed: {e}", flush=True)
        return False
    except Exception as e:
        print(f"[mpris] error: {e}", flush=True)
        return False


def resolve_player(dry_run):
    """Find the first MPRIS player. Returns (bus_name, object) or (None, None)."""
    if dry_run:
        return "dry-run-player", object()
    try:
        import dbus
        bus = dbus.SessionBus()
        names = [n for n in bus.list_names() if n.startswith("org.mpris.MediaPlayer2.")]
        if not names:
            return None, None
        return names[0], bus.get_object(names[0], "/org/mpris/MediaPlayer2")
    except Exception as e:
        print(f"[mpris] error: {e}", flush=True)
        return None, None


def clamp(v):
    return max(0.0, min(100.0, v))


DEFAULT_CONFIG = {
    "trigger_key": "KEY_RIGHTALT",
    "trigger_hold_ms": 0,
    "zones": {"top": 0.15, "right": 0.15, "bottom": 0.15, "left": 0.15},
    "mapping": {"top": "brightness", "right": "volume", "bottom": "seek", "left": "none"},
    "scale": {"brightness": 1.0, "volume": 1.0, "seek_seconds": 300.0},
    "backend": {"volume": "auto", "brightness": "auto"},
    "throttle": {"hz": 20, "quantum": 0.005, "deadzone": 0.003, "max_jump": 0.25},
}
VALID_ACTIONS = {"brightness", "volume", "seek", "none"}


def load_config(path=None):
    """Load TOML merged over defaults, so missing keys stay safe."""
    import copy
    import os
    import tomllib
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    candidates = []
    if path:
        candidates.append(os.path.expanduser(path))
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    candidates.append(os.path.join(xdg, "upil", "config.toml"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml"))
    used = None
    for c in candidates:
        if os.path.isfile(c):
            used = c
            try:
                with open(c, "rb") as f:
                    user = tomllib.load(f)
            except Exception as e:
                print(f"[config] {c} broken ({e}), using defaults", flush=True)
                break
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            break
    return cfg, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default=None,
                    help="evdev key name (default from config).")
    ap.add_argument("--hold-ms", type=int, default=None,
                    help="how long to hold before slide mode starts, in milliseconds (default from config).")
    ap.add_argument("--live", action="store_true",
                    help="drive real backends (default is dry-run print only).")
    ap.add_argument("--no-grab", action="store_true",
                    help="do not grab the touchpad (default: grab in --live).")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="more log detail, repeatable: -v (values+backends), -vv (touch+accumulator). "
                         "Every line has a [..] tag so you can grep it.")
    ap.add_argument("--config", default=None,
                    help="path to a config TOML (default: ~/.config/upil/config.toml, "
                         "next to the script as fallback).")
    ap.add_argument("--print-config", action="store_true",
                    help="print the effective config, then exit.")
    args = ap.parse_args()
    dry_run = not args.live
    use_grab = args.live and not args.no_grab
    V = args.verbose

    def log(tag, msg, level=0):
        if V >= level:
            print(f"[{tag}] {msg}", flush=True)

    cfg, cfg_path = load_config(args.config)
    # CLI wins over config when given explicitly.
    trigger_name = args.trigger or cfg["trigger_key"]
    hold_ms = args.hold_ms if args.hold_ms is not None else cfg["trigger_hold_ms"]
    if args.print_config:
        import json
        print(f"# from: {cfg_path or 'built-in defaults'}")
        print(json.dumps(cfg, indent=2))
        return
    log("config", f"from {cfg_path or 'built-in defaults'}", 1)

    try:
        trigger_code = getattr(ecodes, trigger_name)
    except AttributeError:
        print(f"Unknown trigger: {trigger_name}", file=sys.stderr)
        sys.exit(1)

    kbds = find_keyboards(trigger_code)
    pads = find_touchpads()
    if not kbds or not pads:
        print("Incomplete devices, aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"Mode: {'LIVE' if args.live else 'DRY-RUN'}, grab={'yes' if use_grab else 'no'}",
          flush=True)
    vol_back, vol_driver = pick_volume(
        str(cfg["backend"].get("volume", "auto")), dry_run)
    bright_back, bright_driver = pick_brightness(
        str(cfg["backend"].get("brightness", "auto")), dry_run)
    backlight = require_backlight(dry_run)
    print(f"[backend] volume={vol_driver} brightness={bright_driver} backlight={backlight}", flush=True)
    # Slow tools (wpctl ~80ms) must not block the 50ms input loop:
    # sample fast, actuate at tool speed, latest value wins.
    vol_back, bright_back = AsyncBackend(vol_back), AsyncBackend(bright_back)
    vol_back.start()
    bright_back.start()

    held = {d.path: False for d in kbds}
    hold_since = {}
    trigger_active = False
    prev_active = False
    touching = False
    paused = False        # gesture pauses on multi-finger (firmware signal PROVEN)
    multi_held = set()    # multitap codes currently pressed, per (path, code)
    last_x = last_y = None
    anchor_action, anchor_axis = None, None
    acc_x = acc_y = 0.0
    jump_warned = False   # log jumps once per touch (anti-spam)
    # Standard multitap codes (not numbers). Proven to arrive on co-press.
    MULTI_NAMES = ("BTN_TOOL_DOUBLETAP", "BTN_TOOL_TRIPLETAP",
                   "BTN_TOOL_QUADTAP", "BTN_TOOL_QUINTTAP")
    MULTI_CODES = {getattr(ecodes, n) for n in MULTI_NAMES if hasattr(ecodes, n)}
    # The daemon holds backend values during a gesture (read once at start).
    base_vol = base_bright = None
    base_ready = False     # backend values read once per trigger hold
    sent_vol = sent_bright = None  # last value actually posted (skip duplicates)
    mpris_name, mpris_obj, mpris_warned = None, None, False
    vol_warned = bright_warned = False
    DEADZONE = float(cfg["throttle"]["deadzone"])
    QUANTUM = float(cfg["throttle"]["quantum"])
    TICK = 1.0 / float(cfg["throttle"]["hz"])
    # A jump over JUMP in one event (~5ms) is impossible for a finger:
    # ghost touch / tap bounce / wet touchpad. Drop it + reset baseline.
    JUMP = float(cfg["throttle"].get("max_jump", 0.25))
    Z_TOP = float(cfg["zones"]["top"])
    Z_RIGHT = float(cfg["zones"]["right"])
    Z_BOTTOM = float(cfg["zones"]["bottom"])
    Z_LEFT = float(cfg["zones"]["left"])
    MAP = {k: str(cfg["mapping"].get(k, "none")) for k in ("top", "right", "bottom", "left")}
    bad = {k: v for k, v in MAP.items() if v not in VALID_ACTIONS}
    if bad:
        print(f"[config] invalid mapping: {bad} (choices: {sorted(VALID_ACTIONS)})",
              file=sys.stderr)
        sys.exit(1)
    if all(v == "none" for v in MAP.values()):
        print("[config] every mapping is 'none' — nothing to control, aborting.",
              file=sys.stderr)
        sys.exit(1)
    S_BRIGHT = float(cfg["scale"]["brightness"])
    S_VOL = float(cfg["scale"]["volume"])
    S_SEEK = float(cfg["scale"]["seek_seconds"])

    def zone_of(fx, fy):
        # Returns (action, axis). Priority: top > right > bottom > left.
        # Horizontal strips (top/bottom) use dx, vertical strips (left/right) use dy.
        # Outside strips / mapping none = (None, None), i.e. neutral.
        if fy is not None and fy < Z_TOP:
            a = MAP["top"]
            return (a, "x") if a != "none" else (None, None)
        if fx is not None and fx > 1.0 - Z_RIGHT:
            a = MAP["right"]
            return (a, "y") if a != "none" else (None, None)
        if fy is not None and fy > 1.0 - Z_BOTTOM:
            a = MAP["bottom"]
            return (a, "x") if a != "none" else (None, None)
        if fx is not None and fx < Z_LEFT:
            a = MAP["left"]
            return (a, "y") if a != "none" else (None, None)
        return None, None

    def frac_pos(dev):
        xi, yi = abs_info(dev, ecodes.ABS_X), abs_info(dev, ecodes.ABS_Y)
        return ((xi.value - xi.min) / (xi.max - xi.min),
                (yi.value - yi.min) / (yi.max - yi.min))

    def drain_pads():
        for p in pads:
            try:
                while True:
                    if not list(p.read()):
                        break
            except (BlockingIOError, OSError):
                pass

    kbds_by_fd = {d.fd: d for d in kbds}
    pads_by_fd = {d.fd: d for d in pads}
    pads_paths = {p.path for p in pads}
    last_rescan = time.monotonic()
    try:
        while True:
            watched = dict(kbds_by_fd)
            if trigger_active:
                watched.update(pads_by_fd)
            r, _, _ = select(watched, [], [], TICK)
            now = time.monotonic()
            for fd in r:
                d = watched[fd]
                try:
                    for ev in d.read():
                        if ev.type == ecodes.EV_KEY and ev.code == trigger_code and d.path in held:
                            held[d.path] = bool(ev.value)
                            if ev.value == 1:
                                hold_since[d.path] = now
                            if hold_ms <= 0:
                                trigger_active = any(held.values())
                        elif d.path in pads_paths and ev.type == ecodes.EV_KEY and ev.code in (
                                ecodes.BTN_TOUCH, ecodes.BTN_TOOL_FINGER):
                            if ev.value == 1 and not touching:
                                touching = True
                                paused = False
                                multi_held.clear()
                                jump_warned = False
                                fx, fy = frac_pos(d)
                                last_x, last_y = fx, fy
                                anchor_action, anchor_axis = zone_of(fx, fy)
                                acc_x = acc_y = 0.0
                                log("touch", f"touch at X={fx * 100:.0f}% Y={fy * 100:.0f}%", 2)
                                vol_warned = bright_warned = False
                                if anchor_action in ("volume", "brightness"):
                                    # Read once per hold; clutch touches continue the
                                    # daemon-held value (it is the only writer).
                                    # This removes one ~70ms blocking read per touch.
                                    if not base_ready:
                                        base_vol = vol_back.get() if vol_back.available else 0.0
                                        base_bright = bright_back.get() if bright_back.available else 0.0
                                        base_ready = True
                                    log("backend", f"base vol={base_vol:.1f} bright={base_bright:.1f}", 1)
                                elif anchor_action == "seek":
                                    mpris_name, mpris_obj = resolve_player(dry_run)
                                    mpris_warned = False
                                    if mpris_obj is None:
                                        log("mpris", "no player on the bus", 0)
                                    else:
                                        log("mpris", f"player {mpris_name}", 1)
                                log("zone", f"{anchor_action} ({anchor_axis})" if anchor_action else "neutral")
                            elif ev.value == 0 and touching:
                                touching = False
                                paused = False
                                last_x = last_y = None
                                anchor_action, anchor_axis = None, None
                                log("touch", f"finger lifted (clutch X={acc_x * 100:+.1f}% Y={acc_y * 100:+.1f}%)", 2)
                        elif d.path in pads_paths and ev.type == ecodes.EV_KEY and ev.code in MULTI_CODES:
                            # Second/third finger: PAUSE (not cancel). The frozen OSD
                            # is the visual marker. Auto-resumes on one finger
                            # with a fresh baseline (safe against 333/334/335 flap).
                            # Level 0: as important as the trigger, must show in journals.
                            key = (d.path, ev.code)
                            if ev.value:
                                multi_held.add(key)
                            else:
                                multi_held.discard(key)
                            want_pause = touching and len(multi_held) > 0
                            if want_pause and not paused:
                                paused = True
                                log("touch", "pause (multi-finger) — frozen OSD is the marker", 0)
                            elif not want_pause and paused:
                                paused = False
                                try:
                                    last_x, last_y = frac_pos(d)
                                except OSError:
                                    last_x = last_y = None
                                log("touch", "resume (one finger again, fresh baseline)", 0)
                        elif (trigger_active and touching and d.path in pads_paths
                                and ev.type == ecodes.EV_ABS
                                and ev.code in (ecodes.ABS_X, ecodes.ABS_Y)):
                            xi = abs_info(d, ecodes.ABS_X)
                            yi = abs_info(d, ecodes.ABS_Y)
                            if ev.code == ecodes.ABS_X:
                                fx = (ev.value - xi.min) / (xi.max - xi.min)
                                if last_x is not None and anchor_action is not None and not paused:
                                    dx = fx - last_x
                                    if abs(dx) >= JUMP:
                                        if not jump_warned:
                                            log("touch", f"ignored X jump ({dx * 100:+.0f}%), fresh baseline", 1)
                                            jump_warned = True
                                    elif abs(dx) >= DEADZONE:
                                        acc_x += dx
                                last_x = fx
                            else:
                                fy = (ev.value - yi.min) / (yi.max - yi.min)
                                if last_y is not None and anchor_action is not None and not paused:
                                    dy = fy - last_y
                                    if abs(dy) >= JUMP:
                                        if not jump_warned:
                                            log("touch", f"ignored Y jump ({dy * 100:+.0f}%), fresh baseline", 1)
                                            jump_warned = True
                                    elif abs(dy) >= DEADZONE:
                                        acc_y += dy
                                last_y = fy
                except OSError:
                    # Lost device (unplugged/suspended): close the fd, drop it from
                    # watching; the rescan below picks it up again if it returns.
                    try:
                        d.close()
                    except OSError:
                        pass
                    for gone in [p for p, dev in list(kbds_by_fd.items()) + list(pads_by_fd.items())
                                 if dev is d]:
                        kbds_by_fd.pop(gone, None)
                        pads_by_fd.pop(gone, None)
                    log("device", f"{d.path} gone, rescan scheduled")
            if hold_ms > 0:
                if any(held.values()):
                    earliest = min(hold_since.get(p, now) for p, h in held.items() if h)
                    trigger_active = (now - earliest) * 1000 >= hold_ms
                else:
                    trigger_active = False
                    hold_since = {}
            # Emit at throttle.hz. Direction rule: horizontal strips +dx,
            # vertical strips -dy (up/inward = up). Applies to every action.
            if trigger_active and touching and not paused and anchor_action:
                delta = acc_x if anchor_axis == "x" else -acc_y
                if abs(delta) >= QUANTUM:
                    if anchor_action == "brightness":
                        if not bright_back.available:
                            if not bright_warned:
                                log("backend", f"brightness disabled ({bright_back.missing} not found), gesture ignored", 0)
                                bright_warned = True
                        else:
                            base_bright = clamp(base_bright + delta * 100 * S_BRIGHT)
                            if round(base_bright, 1) != sent_bright:
                                bright_back.post(base_bright)
                                sent_bright = round(base_bright, 1)
                                log("emit", f"brightness {base_bright:.1f}%", 1)
                    elif anchor_action == "volume":
                        if not vol_back.available:
                            if not vol_warned:
                                log("backend", f"volume disabled ({vol_back.missing} not found), gesture ignored", 0)
                                vol_warned = True
                        else:
                            base_vol = clamp(base_vol + delta * 100 * S_VOL)
                            if round(base_vol, 1) != sent_vol:
                                vol_back.post(base_vol)
                                sent_vol = round(base_vol, 1)
                                log("emit", f"volume {base_vol:.1f}%", 1)
                    elif anchor_action == "seek":
                        sec = delta * S_SEEK
                        ok = mpris_seek(sec, dry_run, mpris_obj, mpris_name or "", V)
                        if not ok and not mpris_warned:
                            log("mpris", "seek skipped (no player)", 0)
                            mpris_warned = True
                    acc_x = acc_y = 0.0
            # Rescan devices every 2 seconds (unplug/replug keyboards/mice,
            # suspend/resume, changed event numbers). No service restart.
            # Match key = path (/dev/input/eventX is stable for the same device),
            # because every scan opens new fds that must be dropped if duplicated.
            if now - last_rescan >= 2.0:
                last_rescan = now
                known_paths = {dev.path for dev in list(kbds_by_fd.values()) + list(pads_by_fd.values())}
                for dev in find_keyboards(trigger_code):
                    if dev.path in known_paths:
                        dev.close()
                        continue
                    known_paths.add(dev.path)
                    kbds_by_fd[dev.fd] = dev
                    held.setdefault(dev.path, False)
                    log("device", f"new: {dev.path} {dev.name}")
                for dev in find_touchpads():
                    if dev.path in known_paths:
                        dev.close()
                        continue
                    known_paths.add(dev.path)
                    pads_by_fd[dev.fd] = dev
                    pads_paths.add(dev.path)
                    pads.append(dev)
                    log("device", f"new: {dev.path} {dev.name}")
                    if trigger_active and use_grab:
                        try:
                            dev.grab()
                        except OSError as e:
                            log("grab", f"{dev.path} FAILED: {e}")
            if trigger_active and not prev_active:
                drain_pads()
                if use_grab:
                    grabbed = []
                    for p in pads:
                        try:
                            p.grab()
                            grabbed.append(p)
                        except OSError as e:
                            log("grab", f"{p.path} FAILED: {e}")
                            break
                    if len(grabbed) != len(pads):
                        for p in grabbed:
                            try:
                                p.ungrab()
                            except OSError:
                                pass
                        log("grab", "incomplete — slide mode cancelled")
                        trigger_active = False
                    else:
                        log("trigger", "ACTIVE + grab")
                else:
                    log("trigger", "ACTIVE")
            elif prev_active and not trigger_active:
                log("trigger", "released")
                if use_grab:
                    for p in pads:
                        try:
                            p.ungrab()
                        except OSError:
                            pass
                touching = False
                paused = False
                multi_held.clear()
                last_x = last_y = None
                anchor_action, anchor_axis = None, None
                base_ready = False
                sent_vol = sent_bright = None
                acc_x = acc_y = 0.0
            prev_active = trigger_active
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        vol_back.stop()
        bright_back.stop()
        for p in pads:
            try:
                p.ungrab()
            except OSError:
                pass


if __name__ == "__main__":
    main()
