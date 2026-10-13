#!/usr/bin/env python3
"""Upil calibrate (dry-run, observe-only).

Scans all /dev/input/event* devices, watches keyboards that have the trigger
key (no grab) and the touchpad ABS position (no grab), and prints normalized
coordinates (fractions 0.0-1.0) plus live trigger status.

Safe: never changes system settings, never grabs any device.
Quit with Ctrl-C.

Examples:
    python3 calibrate.py
    python3 calibrate.py --trigger KEY_RIGHTALT --hold-ms 0
"""
import argparse
import sys
import time

from evdev import InputDevice, ecodes, list_devices


class C:
    """Terminal colors. Only white/green/yellow (+ reset, not a color)."""
    W = G = Y = R = ""

    @classmethod
    def setup(cls, enabled):
        if enabled:
            cls.W = "\033[37m"    # white: normal text
            cls.G = "\033[32m"    # green: OK / active
            cls.Y = "\033[33m"    # yellow: titles / zones / warnings
            cls.R = "\033[0m"     # reset


def find_keyboards(trigger_code):
    out = []
    for p in list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        caps = d.capabilities()
        if trigger_code in caps.get(ecodes.EV_KEY, []):
            out.append(d)
    return out


def find_touchpads():
    out = []
    for p in list_devices():
        try:
            d = InputDevice(p)
        except OSError:
            continue
        caps = d.capabilities()
        abs_caps = caps.get(ecodes.EV_ABS, [])
        codes = {c if isinstance(c, int) else c[0] for c in abs_caps}
        has_xy = ecodes.ABS_X in codes and ecodes.ABS_Y in codes
        keys = caps.get(ecodes.EV_KEY, [])
        has_finger = (ecodes.BTN_TOOL_FINGER in keys) or (ecodes.BTN_TOUCH in keys)
        if has_xy and has_finger:
            out.append(d)
    return out


def abs_info(dev, code):
    try:
        return dev.absinfo(code)
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="KEY_RIGHTALT",
                    help="standard evdev key name, for example KEY_RIGHTALT")
    ap.add_argument("--hold-ms", type=int, default=0,
                    help="how long to hold before slide mode activates, in milliseconds. 0 = instant.")
    ap.add_argument("--grab", action="store_true",
                    help="test EVIOCGRAB: lock the touchpad while the trigger is held "
                         "(frozen cursor). The grab releases automatically when the "
                         "trigger is released or the process exits. Default is observe-only.")
    ap.add_argument("--no-color", action="store_true",
                    help="disable colors (auto-disabled when output is not a terminal).")
    args = ap.parse_args()
    C.setup(not args.no_color and sys.stdout.isatty())

    try:
        trigger_code = getattr(ecodes, args.trigger)
    except AttributeError:
        print(f"Unknown trigger: {args.trigger}", file=sys.stderr)
        sys.exit(1)

    kbds = find_keyboards(trigger_code)
    pads = find_touchpads()
    if not kbds:
        print(f"{C.Y}No keyboard with {args.trigger}{C.R}", file=sys.stderr)
    if not pads:
        print(f"{C.Y}No touchpad found "
              f"(needs ABS_X+Y + BTN_TOUCH/BTN_TOOL_FINGER){C.R}", file=sys.stderr)
    print(f"{C.Y}== Upil calibrate =={C.R}")
    print(f"Trigger : {C.W}{args.trigger}{C.R} (code {trigger_code}), "
          f"hold {args.hold_ms}ms")
    print(f"Keyboards ({C.G}{len(kbds)}{C.R} watched, no grab):")
    for d in kbds:
        print(f"  {C.W}{d.path}{C.R}  {d.name}")
    print(f"Touchpads ({C.G}{len(pads)}{C.R} watched, no grab):")
    for p in pads:
        xi, yi = abs_info(p, ecodes.ABS_X), abs_info(p, ecodes.ABS_Y)
        caps = p.capabilities().get(ecodes.EV_ABS, [])
        codes = {c if isinstance(c, int) else c[0] for c in caps}
        mt = ("full (slots + per-finger position)"
              if ecodes.ABS_MT_POSITION_X in codes else
              "semi (slots without per-finger position — 1 blended position)" if ecodes.ABS_MT_SLOT in codes else
              "none (legacy ABS)")
        wmm = (xi.max - xi.min) / xi.resolution if xi.resolution else 0
        hmm = (yi.max - yi.min) / yi.resolution if yi.resolution else 0
        size = f"~{wmm:.0f}x{hmm:.0f}mm" if xi.resolution and yi.resolution else "unknown (res=0)"
        print(f"  {C.W}{p.path}{C.R}  {p.name}")
        print(f"    ABS_X  min={xi.min} max={xi.max} res={xi.resolution} now={xi.value}")
        print(f"    ABS_Y  min={yi.min} max={yi.max} res={yi.resolution} now={yi.value}")
        print(f"    size {C.G}{size}{C.R} | multitouch: {mt}")
        print(f"    {C.Y}zones 15%:{C.R} top Y<{yi.min + 0.15 * (yi.max - yi.min):.0f} | "
              f"right X>{xi.min + 0.85 * (xi.max - xi.min):.0f} | "
              f"bottom Y>{yi.min + 0.85 * (yi.max - yi.min):.0f} | "
              f"left X<{xi.min + 0.15 * (xi.max - xi.min):.0f}")
    print("Hold the trigger, then slide a finger. Ctrl-C to quit.")
    sys.stdout.flush()

    held = {d.path: False for d in kbds}
    hold_since = {}
    trigger_active = False

    kbds_by_fd = {d.fd: d for d in kbds}
    pads_by_fd = {d.fd: d for d in pads}
    from select import select

    def drain_pads():
        # Drop stale events buffered in the kernel while the trigger is
        # released, so the first position after activation does not jump.
        for p in pads:
            try:
                while True:
                    batch = list(p.read())
                    if not batch:
                        break
            except BlockingIOError:
                pass
            except OSError:
                pass

    prev_active = False
    # --- gesture state ---
    touching = False      # finger down (BTN_TOUCH)
    last_x = last_y = None
    anchor_zone = None    # zone latched when the finger touches down
    acc_x = acc_y = 0.0   # fraction accumulator
    DEADZONE = 0.003      # ~0.3% of width/height, ignore finger jitter
    QUANTUM = 0.005       # emit every >= 0.5%
    TICK = 0.05           # 20Hz throttle

    def zone_of(fx, fy):
        # Priority: top > right > bottom > left. Outside all four = neutral.
        # Fixed 15% demo zones; the daemon reads [zones]/[mapping] from config.
        if fy is not None and fy < 0.15:
            return "brightness"
        if fx is not None and fx > 0.85:
            return "volume"
        if fy is not None and fy > 0.85:
            return "seek"
        if fx is not None and fx < 0.15:
            return "left"
        return None

    def frac_pos(dev):
        xi, yi = abs_info(dev, ecodes.ABS_X), abs_info(dev, ecodes.ABS_Y)
        fx = (xi.value - xi.min) / (xi.max - xi.min)
        fy = (yi.value - yi.min) / (yi.max - yi.min)
        return fx, fy, xi, yi

    pads_paths = {p.path for p in pads}
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
                            if args.hold_ms <= 0:
                                trigger_active = any(held.values())
                        elif d.path in pads_paths and ev.type == ecodes.EV_KEY and ev.code in (
                                ecodes.BTN_TOUCH, ecodes.BTN_TOOL_FINGER):
                            if ev.value == 1 and not touching:
                                touching = True
                                fx, fy, _, _ = frac_pos(d)
                                last_x, last_y = fx, fy
                                anchor_zone = zone_of(fx, fy)
                                acc_x = acc_y = 0.0
                                print(f"{C.Y}touch at X={fx * 100:.0f}% Y={fy * 100:.0f}% "
                                      f"→ zone {anchor_zone or 'neutral (no action)'}{C.R}", flush=True)
                            elif ev.value == 0 and touching:
                                touching = False
                                last_x = last_y = None
                                anchor_zone = None
                                # Clutch: the accumulator survives while the trigger is held;
                                # the position baseline resets so it does not jump on re-touch.
                                print(f"lift finger (clutch, accumulator X={acc_x * 100:+.1f}% "
                                      f"Y={acc_y * 100:+.1f}%)", flush=True)
                        elif (trigger_active and touching and d.path in pads_paths
                                and ev.type == ecodes.EV_ABS
                                and (ev.code in (ecodes.ABS_X, ecodes.ABS_Y))):
                            xi = abs_info(d, ecodes.ABS_X)
                            yi = abs_info(d, ecodes.ABS_Y)
                            if ev.code == ecodes.ABS_X:
                                fx = (ev.value - xi.min) / (xi.max - xi.min)
                                if last_x is not None and anchor_zone is not None:
                                    dx = fx - last_x
                                    if abs(dx) >= DEADZONE:
                                        acc_x += dx
                                last_x = fx
                            else:
                                fy = (ev.value - yi.min) / (yi.max - yi.min)
                                if last_y is not None and anchor_zone is not None:
                                    dy = fy - last_y
                                    if abs(dy) >= DEADZONE:
                                        acc_y += dy
                                last_y = fy
                except OSError:
                    pass
            # 20Hz throttle: emit the accumulator once it reaches QUANTUM.
            if trigger_active and touching and anchor_zone:
                emit = None
                if anchor_zone == "brightness" and abs(acc_x) >= QUANTUM:
                    emit = (f"brightness {acc_x * 100:+.1f}%", acc_x)
                    acc_x = 0.0
                elif anchor_zone == "volume" and abs(acc_y) >= QUANTUM:
                    dv = -acc_y * 100  # up (Y decreases) = louder
                    emit = (f"volume {dv:+.1f}%", None)
                    acc_y = 0.0
                elif anchor_zone == "seek" and abs(acc_x) >= QUANTUM:
                    emit = (f"seek {acc_x * 100:+.1f}% track", acc_x)
                    acc_x = 0.0
                if emit:
                    print(f"[20Hz] {emit[0]}", flush=True)
            if args.hold_ms > 0:
                if any(held.values()):
                    earliest = min(hold_since.get(p, now) for p, h in held.items() if h)
                    trigger_active = (now - earliest) * 1000 >= args.hold_ms
                else:
                    trigger_active = False
                    hold_since = {}
            if trigger_active and not prev_active:
                # Rising edge: buang posisi basi, lalu mulai dari posisi segar.
                drain_pads()
                if args.grab:
                    grabbed = []
                    for p in pads:
                        try:
                            p.grab()
                            grabbed.append(p)
                        except OSError as e:
                            print(f"{C.Y}grab {p.path} FAILED: {e}{C.R}", flush=True)
                            break
                    if len(grabbed) != len(pads):
                        for p in grabbed:
                            try:
                                p.ungrab()
                            except OSError:
                                pass
                        print("grab incomplete — slide mode cancelled", flush=True)
                        trigger_active = False
                    else:
                        print(f"{C.G}trigger ACTIVE — touchpad grabbed (frozen cursor){C.R}", flush=True)
                else:
                    print(f"{C.G}trigger ACTIVE — reading touchpad{C.R}", flush=True)
            elif prev_active and not trigger_active:
                print("trigger released — stopped reading touchpad", flush=True)
                if args.grab:
                    for p in pads:
                        try:
                            p.ungrab()
                        except OSError:
                            pass
                    print(f"{C.Y}grab released — cursor back to normal{C.R}", flush=True)
                # Reset touch state: if the trigger is pressed again while the
                # finger is still down, the old baseline must not be reused.
                touching = False
                last_x = last_y = None
                anchor_zone = None
                acc_x = acc_y = 0.0
            prev_active = trigger_active
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
