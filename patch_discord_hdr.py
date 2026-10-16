"""Replace Discord's HDR screen-share shaders with a correct HDR -> SDR conversion.

Discord's capture module (discord_voice.node) converts HDR captures to SDR with two
pixel shaders that multiply by a CPU-computed `scale`; the "tonemapped" one also
lifts shadows and boosts saturation by up to 40%. With a high Windows SDR content
brightness the stream comes out blown out. This script swaps both shaders for
hdr_to_sdr.hlsl, which maps Windows' SDR white level to SDR white and does nothing else.

Discord must be fully quit (or pass --restart-discord). Re-run after Discord updates.
"""

import argparse
import ctypes
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

MARKER = b"discord-hdr-fix v1"
HERE = Path(__file__).resolve().parent
SHADER_SOURCE = HERE / "hdr_to_sdr.hlsl"
DISCORD_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Discord"

# ---------------------------------------------------------------- d3dcompiler


class _Macro(ctypes.Structure):
    _fields_ = [("Name", ctypes.c_char_p), ("Definition", ctypes.c_char_p)]


_P, _SZ, _U, _PP = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
d3dc = ctypes.WinDLL("d3dcompiler_47.dll")
d3dc.D3DCompile.argtypes = [ctypes.c_char_p, _SZ, ctypes.c_char_p, ctypes.POINTER(_Macro), _P,
                            ctypes.c_char_p, ctypes.c_char_p, _U, _U, _PP, _PP]
d3dc.D3DStripShader.argtypes = [ctypes.c_char_p, _SZ, _U, _PP]
d3dc.D3DSetBlobPart.argtypes = [ctypes.c_char_p, _SZ, _U, _U, ctypes.c_char_p, _SZ, _PP]
d3dc.D3DDisassemble.argtypes = [ctypes.c_char_p, _SZ, _U, ctypes.c_char_p, _PP]
for _f in (d3dc.D3DCompile, d3dc.D3DStripShader, d3dc.D3DSetBlobPart, d3dc.D3DDisassemble):
    _f.restype = ctypes.c_long

D3DCOMPILE_OPTIMIZATION_LEVEL3 = 1 << 15
D3DCOMPILER_STRIP_ALL = 1 | 2 | 4          # reflection, debug info, test blobs
D3D_BLOB_PRIVATE_DATA = 10


def _vtable(obj, n):
    return ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0],
                       ctypes.POINTER(ctypes.c_void_p * n))[0]


def _release(obj):
    ctypes.WINFUNCTYPE(ctypes.c_ulong, _P)(_vtable(obj, 3)[2])(obj)


def _take_blob(blob):
    """Copy an ID3DBlob's contents to bytes and release it."""
    vtbl = _vtable(blob, 5)
    ptr = ctypes.WINFUNCTYPE(_P, _P)(vtbl[3])(blob)
    size = ctypes.WINFUNCTYPE(_SZ, _P)(vtbl[4])(blob)
    data = ctypes.string_at(ptr, size)
    _release(blob)
    return data


def _check(hr, what):
    if hr != 0:
        sys.exit(f"{what} failed (0x{hr & 0xffffffff:08x})")


def compile_shader(white_nits):
    src = SHADER_SOURCE.read_bytes()
    defines = (_Macro * 2)(_Macro(b"WHITE_NITS", f"{white_nits:.4f}".encode()), _Macro(None, None))
    code, errs = ctypes.c_void_p(), ctypes.c_void_p()
    hr = d3dc.D3DCompile(src, len(src), SHADER_SOURCE.name.encode(), defines, None, b"main",
                         b"ps_4_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
                         ctypes.byref(code), ctypes.byref(errs))
    msg = _take_blob(errs).decode(errors="replace") if errs.value else ""
    _check(hr, f"shader compile: {msg}")
    blob = _take_blob(code)
    stripped = ctypes.c_void_p()
    _check(d3dc.D3DStripShader(blob, len(blob), D3DCOMPILER_STRIP_ALL, ctypes.byref(stripped)),
           "D3DStripShader")
    return _take_blob(stripped)


def pad_to(blob, size):
    """Add a PRIV chunk so the container is exactly `size` bytes (checksum recomputed)."""
    n = size - len(blob) - 12                 # chunk offset entry + chunk header
    if n < len(MARKER):
        sys.exit(f"replacement shader ({len(blob)} bytes) does not fit in {size} bytes")
    part = MARKER + b"\0" * (n - len(MARKER))
    out = ctypes.c_void_p()
    _check(d3dc.D3DSetBlobPart(blob, len(blob), D3D_BLOB_PRIVATE_DATA, 0, part, len(part),
                               ctypes.byref(out)), "D3DSetBlobPart")
    padded = _take_blob(out)
    if len(padded) != size:
        sys.exit(f"padding produced {len(padded)} bytes, expected {size}")
    return padded


def disassemble(blob):
    out = ctypes.c_void_p()
    if d3dc.D3DDisassemble(blob, len(blob), 0, None, ctypes.byref(out)) != 0:
        return None
    return _take_blob(out).decode(errors="replace")


def validate_on_gpu(blob):
    """Create the pixel shader on a real D3D11 device, with the length Discord will pass."""
    d3d11 = ctypes.WinDLL("d3d11.dll")
    d3d11.D3D11CreateDevice.argtypes = [_P, _U, _P, _U, _P, _U, _U, _PP, ctypes.POINTER(_U), _PP]
    d3d11.D3D11CreateDevice.restype = ctypes.c_long
    dev, ctx, level = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_uint()
    hr = d3d11.D3D11CreateDevice(None, 1, None, 0, None, 0, 7, ctypes.byref(dev),
                                 ctypes.byref(level), ctypes.byref(ctx))
    if hr != 0:
        print(f"  (skipping GPU validation: D3D11CreateDevice 0x{hr & 0xffffffff:08x})")
        return True
    create_ps = ctypes.WINFUNCTYPE(ctypes.c_long, _P, ctypes.c_char_p, _SZ, _P, _PP)(
        _vtable(dev, 16)[15])
    ps = ctypes.c_void_p()
    hr = create_ps(dev, blob, len(blob), None, ctypes.byref(ps))
    for obj in (ps, ctx, dev):
        if obj.value:
            _release(obj)
    return hr == 0


# ---------------------------------------------------------------- DXBC scanning

def iter_dxbc(data):
    """Yield (offset, bytes) for every DXBC shader container embedded in `data`."""
    pos = 0
    while (pos := data.find(b"DXBC", pos)) != -1:
        if pos + 32 <= len(data):
            one, size, count = struct.unpack_from("<III", data, pos + 20)
            if one == 1 and 32 < size < 1_000_000 and pos + size <= len(data) and count < 32:
                yield pos, bytes(data[pos:pos + size])
                pos += size
                continue
        pos += 4


def chunks(blob):
    count = struct.unpack_from("<I", blob, 28)[0]
    for i in range(count):
        off = struct.unpack_from("<I", blob, 32 + 4 * i)[0]
        size = struct.unpack_from("<I", blob, off + 4)[0]
        yield blob[off:off + 4], blob[off + 8:off + 8 + size]


def find_targets(data):
    """Discord's HDR pixel shaders (those with `needs_st2084`), or ones already patched."""
    targets = []
    for pos, blob in iter_dxbc(data):
        parts = dict(chunks(blob))
        if MARKER in parts.get(b"PRIV", b""):
            targets.append((pos, len(blob), True))
        elif b"needs_st2084" in parts.get(b"RDEF", b"") and (b"SHDR" in parts or b"SHEX" in parts):
            targets.append((pos, len(blob), False))
    return targets


# ---------------------------------------------------------------- Windows

def sdr_white_levels():
    """Windows' SDR content brightness for each active display, in nits."""
    user32 = ctypes.WinDLL("user32")
    user32.GetDisplayConfigBufferSizes.argtypes = [_U, ctypes.POINTER(_U), ctypes.POINTER(_U)]
    user32.QueryDisplayConfig.argtypes = [_U, ctypes.POINTER(_U), _P, ctypes.POINTER(_U), _P, _P]
    user32.DisplayConfigGetDeviceInfo.argtypes = [_P]
    for f in (user32.GetDisplayConfigBufferSizes, user32.QueryDisplayConfig,
              user32.DisplayConfigGetDeviceInfo):
        f.restype = ctypes.c_long

    QDC_ONLY_ACTIVE_PATHS = 2
    GET_SDR_WHITE_LEVEL = 11
    PATH_SIZE, MODE_SIZE, TARGET_INFO_OFFSET = 72, 64, 20
    n_paths, n_modes = wintypes.UINT(), wintypes.UINT()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths),
                                          ctypes.byref(n_modes)) != 0:
        return []
    paths = (ctypes.c_byte * (PATH_SIZE * n_paths.value))()
    modes = (ctypes.c_byte * (MODE_SIZE * n_modes.value))()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes, None) != 0:
        return []
    levels = []
    for i in range(n_paths.value):
        luid_lo, luid_hi, target_id = struct.unpack_from(
            "<IiI", paths, PATH_SIZE * i + TARGET_INFO_OFFSET)
        req = ctypes.create_string_buffer(
            struct.pack("<IIIiII", GET_SDR_WHITE_LEVEL, 24, luid_lo, luid_hi, target_id, 0), 24)
        if user32.DisplayConfigGetDeviceInfo(req) == 0:
            levels.append(struct.unpack_from("<I", req, 20)[0] * 80 / 1000)
    return levels


def discord_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Discord.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "Discord.exe" in out


def stop_discord():
    subprocess.run(["taskkill", "/IM", "Discord.exe", "/F"], capture_output=True)
    for _ in range(50):
        if not discord_running():
            return
        time.sleep(0.1)


def start_discord():
    subprocess.Popen([str(DISCORD_DIR / "Update.exe"), "--processStart", "Discord.exe"])
    print("Discord restarting")


def find_module():
    """The newest installed discord_voice.node (the one Discord loads on next start)."""
    def version(path):
        m = re.search(r"app-(\d+)\.(\d+)\.(\d+)", path)
        return tuple(map(int, m.groups())) if m else (0, 0, 0)

    found = glob.glob(str(DISCORD_DIR / "app-*" / "modules" / "discord_voice-*"
                          / "discord_voice" / "discord_voice.node"))
    if not found:
        sys.exit(f"discord_voice.node not found under {DISCORD_DIR}")
    newest = max(found, key=version)
    return Path(newest), "app-%d.%d.%d" % version(newest)


# ---------------------------------------------------------------- commands

def dump(module, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (pos, blob) in enumerate(iter_dxbc(module.read_bytes())):
        path = out_dir / f"shader_{i:02d}_0x{pos:08x}.asm"
        path.write_text(disassemble(blob) or "(could not disassemble)\n")
        print(f"  {path.name} ({len(blob)} bytes)")


def restore(module, backup):
    if not backup.exists():
        sys.exit(f"no backup at {backup}")
    shutil.copy2(backup, module)
    print(f"restored original module from {backup}")


def patch(module, backup, white_nits, dry_run):
    if white_nits is None:
        levels = sdr_white_levels()
        white_nits = levels[0] if levels else 480.0
        print(f"Windows SDR white level: {levels or 'unknown'} -> using {white_nits:.0f} nits")

    data = bytearray(module.read_bytes())
    targets = find_targets(data)
    if not targets:
        sys.exit("no HDR shaders found; Discord's capture code may have changed (try --dump)")

    shader = compile_shader(white_nits)
    print(f"replacement shader: {len(shader)} bytes (SDR white = {white_nits:.0f} nits)")
    for pos, size, already_patched in targets:
        padded = pad_to(shader, size)
        if not validate_on_gpu(padded):
            sys.exit("D3D11 rejected the patched shader; nothing written")
        verb = "re-patching" if already_patched else "patching"
        print(f"  {verb} shader at 0x{pos:08x} ({size} bytes): GPU validation OK")
        data[pos:pos + size] = padded

    if dry_run:
        print("dry run: nothing written\n")
        print(disassemble(pad_to(shader, targets[0][1])))
        return

    if not backup.exists() and not any(t[2] for t in targets):
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module, backup)
        print(f"backed up original to {backup}")
    tmp = module.with_name(module.name + ".hdrfix-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, module)
    print("patched. Start Discord and Go Live again.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="build and validate the patch without writing anything")
    mode.add_argument("--restore", action="store_true",
                      help="put the original discord_voice.node back from the backup")
    mode.add_argument("--dump", metavar="DIR", type=Path,
                      help="disassemble every shader in discord_voice.node into DIR")
    ap.add_argument("--white-nits", type=float, metavar="NITS",
                    help="HDR brightness that becomes white on stream; lower = brighter "
                         "(default: your Windows SDR content brightness)")
    ap.add_argument("--restart-discord", action="store_true",
                    help="close Discord first and start it again afterwards")
    args = ap.parse_args()

    module, app_version = find_module()
    backup = HERE / "backups" / app_version / "discord_voice.node"
    print(f"module: {module}")

    if args.dump:
        return dump(module, args.dump)
    if args.dry_run:
        return patch(module, backup, args.white_nits, dry_run=True)

    if args.restart_discord:
        stop_discord()
    elif discord_running():
        sys.exit("Discord is running. Quit it fully (tray icon -> Quit Discord) "
                 "or pass --restart-discord.")
    try:
        if args.restore:
            restore(module, backup)
        else:
            patch(module, backup, args.white_nits, dry_run=False)
    finally:
        if args.restart_discord:
            start_discord()


if __name__ == "__main__":
    main()
