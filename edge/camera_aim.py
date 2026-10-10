"""Aim a camera at an indicator LED, and pick the threshold by watching numbers.

AIMING IS THE WHOLE JOB. The counting is trivial once the region sits on one
LED and the threshold sits between its dark and its lit brightness. Both of
those are impossible to guess and obvious to see, so this shows them:

    --list      the cameras the operating system offers
    --scan      ONE frame, split into a grid, so you can find the LED's cell
    --watch     the region's brightness live, with a bar and a LIT/dark verdict

A typical commissioning run:

    python edge/camera_aim.py --list
    python edge/camera_aim.py --device "OnePlus NordCE 5G (Windows Virtual Camera)" --scan
    python edge/camera_aim.py --device "..." --region 300,220,14,14 --watch

Point the camera, run --scan, read off which cell is brightest when the LED is
lit, then --watch that cell through a few cycles. You want the number to swing
clearly between two levels. Put the threshold between them.

WHY NOT AUTO-THRESHOLD. A factory's light changes through the day and an
adaptive threshold would quietly re-learn a stuck-on LED as the new dark, which
is a counter that reports a stopped machine as running forever. A human picks
the number once, from the two levels this prints.
"""
import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

from ampedge.adapters import camera as cam  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def list_devices():
    try:
        import imageio_ffmpeg
    except ImportError:
        print("imageio-ffmpeg is not installed.  pip install imageio-ffmpeg")
        return 1
    import subprocess
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([exe, "-hide_banner", "-list_devices", "true",
                        "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, timeout=60)
    found = []
    for line in r.stderr.splitlines():
        if '"' in line and "(video)" in line:
            found.append(line.split('"')[1])
    if not found:
        print("No cameras found. On Linux use -f v4l2 -i /dev/video0; on macOS "
              "avfoundation. This build lists Windows (dshow) devices.")
        return 1
    print(f"{len(found)} camera(s):\n")
    for name in found:
        print(f"  {name}")
    print("\nPass one with --device \"<name>\" (quote it: the names have spaces).")
    return 0


def grab(device, fps, width, height, frames=20):
    f = cam._Frames(device, fps, width, height)
    f.open()
    try:
        last = None
        for _ in range(frames):          # let auto-exposure settle
            buf = f.read()
            if buf is not None:
                last = buf
        return last
    finally:
        f.close()


def cmd_scan(args):
    frame = grab(args.device, args.fps, args.width, args.height)
    if frame is None:
        print("no frame came back; is the camera in use by another program?")
        return 1
    rows, cols = args.rows, args.cols
    ch, cw = args.height // rows, args.width // cols
    print(f"{args.width}x{args.height} split into {rows}x{cols} cells "
          f"of {cw}x{ch} px. Mean brightness, 0-255:\n")
    cells = []
    for r in range(rows):
        line = []
        for c in range(cols):
            box = (c * cw, r * ch, cw, ch)
            m = cam.region_mean(frame, args.width, args.height, box)
            cells.append((m, r, c, box))
            line.append(f"{m:5.0f}")
        print("  " + " ".join(line))
    cells.sort(reverse=True)
    print("\nBrightest cells (point the camera so the LIT LED is in one of them):")
    for m, r, c, box in cells[:5]:
        print(f"  row {r} col {c}  mean {m:5.1f}   --region {box[0]},{box[1]},"
              f"{box[2]},{box[3]}")
    print("\nA cell is coarse. Narrow it by halving w and h and nudging x,y, "
          "then --watch it: you want ONE LED, not its neighbour too.")
    return 0


def cmd_watch(args):
    box = tuple(int(v) for v in args.region.split(","))
    if len(box) != 4:
        print("--region takes x,y,w,h in pixels, e.g. --region 300,220,14,14")
        return 1
    f = cam._Frames(args.device, args.fps, args.width, args.height)
    f.open()
    print(f"\nwatching region {box} at {args.fps:g} fps. Ctrl+C to stop.")
    print("Let the machine run a few cycles. You want the number to swing "
          "between two clear levels.\n")
    lo, hi, n, lit_n = 255.0, 0.0, 0, 0
    try:
        while True:
            buf = f.read()
            if buf is None:
                print("  no frame")
                time.sleep(0.5)
                continue
            m = cam.region_mean(buf, args.width, args.height, box)
            if m is None:
                print(f"  region {box} is outside the {args.width}x"
                      f"{args.height} frame")
                return 1
            lo, hi, n = min(lo, m), max(hi, m), n + 1
            lit = m >= args.threshold
            lit_n += 1 if lit else 0
            bar = "#" * int(m / 255 * 40)
            print(f"  {m:6.1f} {'LIT ' if lit else '    '} |{bar:<40}|")
            time.sleep(1.0 / args.fps)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
    print(f"\n  {n} frames: darkest {lo:.1f}, brightest {hi:.1f}, "
          f"lit {lit_n/max(1,n)*100:.0f}% of the time at threshold "
          f"{args.threshold:g}")
    if hi - lo < 25:
        print("\n  THAT IS NOT A BLINKING LED. The region barely changed, so")
        print("  it is either not on the LED, or the LED did not fire while")
        print("  you watched. Re-run --scan with the machine mid-cycle.")
    else:
        print(f"\n  Put the threshold between them, e.g. {(lo + hi) / 2:.0f}.")
        print(f"  Then in the config:  region: [{box[0]}, {box[1]}, {box[2]}, "
              f"{box[3]}]   threshold: {(lo + hi) / 2:.0f}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Aim a camera at an LED.")
    ap.add_argument("--list", action="store_true", help="list cameras and exit")
    ap.add_argument("--device", default="")
    ap.add_argument("--fps", type=float, default=cam.DEFAULT_FPS)
    ap.add_argument("--width", type=int, default=cam.WIDTH)
    ap.add_argument("--height", type=int, default=cam.HEIGHT)
    ap.add_argument("--scan", action="store_true",
                    help="one frame as a brightness grid, to find the LED")
    ap.add_argument("--rows", type=int, default=12)
    ap.add_argument("--cols", type=int, default=16)
    ap.add_argument("--region", default="", help="x,y,w,h in pixels")
    ap.add_argument("--watch", action="store_true",
                    help="live brightness of --region")
    ap.add_argument("--threshold", type=float, default=cam.DEFAULT_THRESHOLD)
    args = ap.parse_args(argv)

    if args.list:
        return list_devices()
    if not args.device:
        print("--device is required. Run --list to see the names.")
        return 1
    if args.scan:
        return cmd_scan(args)
    if args.watch:
        if not args.region:
            print("--watch needs --region x,y,w,h. Run --scan first.")
            return 1
        return cmd_watch(args)
    print("Nothing to do. Use --list, --scan or --watch.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
