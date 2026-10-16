# Discord HDR streaming brightness fix

If you stream ("Go Live" / screen share) from Windows with HDR turned on, Discord sends your viewers a blown-out, oversaturated picture, and the higher your Windows **SDR content brightness** slider is, the worse it gets.

This patches Discord's HDR-to-SDR conversion shaders so viewers see what you see, without turning HDR off or lowering the slider.

## Why it happens

Discord captures an HDR desktop as 16-bit float scRGB, where `1.0` means 80 nits. Windows draws normal (SDR) content at your SDR content brightness. At 100% that is 480 nits, so a white window arrives as `6.0`.

Discord's capture module (`discord_voice.node`) converts that to SDR with one of two pixel shaders:

- **The plain one** multiplies by a brightness value Discord calculates and clips.
- **The "tonemapped" one** multiplies by the same value, clips brightness at white, then **lifts the shadows** and **boosts saturation by up to 40%**.

Discord's own log shows the inputs to that calculation:

```
DXGI Display HDR check - is_hdr: true, sdr_white: 480 nits, max_lum: Ok(426.8598) nits
```

When the SDR white level is high, and especially when it is above the display's reported peak, the result is the blown-out stream.

## What the patch does

It replaces both shaders with [`hdr_to_sdr.hlsl`](hdr_to_sdr.hlsl):

1. If the source is HDR10 (PQ), it decodes it and converts BT.2020 to BT.709.
2. It scales so that your SDR white level becomes white on stream. Anything dimmer scales linearly.
3. It clips over-bright highlights while keeping their hue.
4. It encodes to sRGB if Discord asks for it.

That's the whole conversion. There's no shadow lift and no saturation boost, and Discord's brightness value is ignored.

## Requirements

- Windows 10 or 11 with the Discord desktop app (stable channel)
- Python 3.8+ (no packages needed; it uses the Direct3D shader compiler that ships with Windows)

## Usage

Quit Discord fully (tray icon → **Quit Discord**), then:

```bash
python patch_discord_hdr.py
```

Or let the script close and reopen Discord for you:

```bash
python patch_discord_hdr.py --restart-discord
```

Then start your stream again.

| Option | What it does |
| --- | --- |
| `--white-nits N` | The HDR brightness (nits) that becomes white on stream. Defaults to your Windows SDR content brightness. Lower makes the stream brighter. |
| `--dry-run` | Build and validate the patch without writing anything |
| `--restore` | Put the original `discord_voice.node` back from the backup |
| `--dump DIR` | Disassemble every shader in `discord_voice.node` into `DIR`, which helps when Discord changes them |
| `--restart-discord` | Close Discord first and start it again afterwards (also works with `--restore`) |

**After a Discord update**, the module gets replaced and the stream blows out again, so run the script again. It always patches the newest installed Discord version, which is the one Discord loads next.

**To undo:** run `python patch_discord_hdr.py --restore`. The script backs up the original module to `backups/<discord version>/` before the first patch.

## How it works

- It scans `discord_voice.node` for embedded DXBC shader containers. The targets are the pixel shaders whose constant buffer has a `needs_st2084` field, which are Discord's two HDR conversion shaders.
- It compiles `hdr_to_sdr.hlsl` to `ps_4_0` with `d3dcompiler_47.dll`, keeping the same bindings, constant buffer layout and input signature as Discord's shaders, and strips the reflection data.
- It pads the compiled shader with a `PRIV` (private data) chunk so it is exactly as long as each original, which lets it be swapped in place. The Direct3D compiler recomputes the container checksum.
- Before writing anything, it creates each padded shader on a real D3D11 device with the exact length Discord will pass. If the driver rejects it, nothing is written.
- Patched shaders carry a marker, so re-running the script detects them and re-patches, for example with a different `--white-nits`.

## Caveats

- This modifies the Discord client's files, which Discord's Terms of Service don't allow. It only changes how your own stream's pixels are converted, but use it at your own risk.
- It isn't affiliated with or endorsed by Discord.
- Tested on Windows 11 with an RTX 4090 and a Dell AW3423DW, on Discord desktop `app-1.0.9261`. If Discord rewrites its capture shaders, the script stops with "no HDR shaders found" and doesn't patch anything.

## License

[MIT](LICENSE)
