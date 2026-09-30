# debug-default-profile

[中文](README.md) · **English**

Let Chrome / Edge open a remote debugging port (CDP) while running on the **default
user data directory**.

## What this is

Chromium-based browsers refuse to bind `--remote-debugging-port` /
`--remote-debugging-pipe` when the default user data directory is in use. To get a port
at all, you normally have to pass an explicit non-default `--user-data-dir`.

This project NOPs the branch instructions of that check inside the browser binary
(`chrome.dll` / `msedge.dll`), so the default profile can host a debugging port too.
No throwaway profile for every automation / debugging / test run.

## How it works (30-second version)

- Upstream the gate is `IsRemoteDebuggingAllowed()`: a `std::optional<bool>` saying
  whether the default user data directory is in use. It is fail-closed — if the value
  cannot be computed, it counts as "yes, default" and the request is rejected.
- Compiled into `chrome.dll` / `msedge.dll`, that check collapses into a few
  instruction pairs:

```
mov   eax, 2                 ; error code: kDisabledByDefaultUserDataDir
cmp   [has_value], 1         ; std::optional::has_value()
jne   reject                 ; no value -> treated as default dir -> reject
cmp   [value], 0             ; is the data dir the default one?
jne   reject
...allow
```

- The tool overwrites those `jne`s with `NOP`s so control flow falls through to the
  allow path.

The full reverse-engineering write-up is in
[docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md) (Chinese).
If you build Chromium yourself, there is also a source-level patch:
[docs/disable_cdp_user_data_dir_check.patch](docs/disable_cdp_user_data_dir_check.patch).

## Supported versions

| Browser | Version | Target file |
| --- | --- | --- |
| Chrome | 154.0.8037.93 (current) | chrome.dll |
| Chrome | 154.0.8037.58 | chrome.dll |
| Edge | 154.0.4258.48 (current) | msedge.dll |
| Edge | 154.0.4258.37 | msedge.dll |

When the version does not match (after an auto-update, say) the tool refuses to write
instead of corrupting the file. See below for adding a version — after a minor
auto-update, `--locate` re-finds the patch sites for you.

## Usage

Close the browser first, then run as Administrator:

```
python patch_debug_port.py --browser chrome
python patch_debug_port.py --browser edge
```

`Users` has read-only access under `Program Files`, so **elevation is mandatory**.
When you are not an administrator — or the browser is still running — the tool stops
before writing and tells you why; it never leaves a half-written backup or a
half-applied patch. To find out whether you have permission first, run `--dry-run`:
its last line reports whether the target is writable (`写入权限：可写 / 不可写`).

Verify only, no writes:

```
python patch_debug_port.py --browser chrome --dry-run
```

Roll back:

```
python patch_debug_port.py --browser chrome --restore
```

Other flags:

- `--path` — target the DLL directly
- `--version` — pin the version
- `--backup-dir` — where the backup goes (default: next to the DLL, suffix `.orig.bak`)
- `--no-backup` — do not create a backup
- `--list` — list supported versions and what is detected on this machine
- `--locate` — read-only scan that prints patch sites in a form you can paste straight
  into `PATCH_DB`

Verify: launch the browser with `--remote-debugging-port=9222` and open
http://127.0.0.1:9222/json/version.

## Adding a new version

**Minor auto-update (the common case):** the gate code barely changes, it just moves.

```
python patch_debug_port.py --browser chrome --locate
python patch_debug_port.py --browser edge --locate
```

It prints patch sites such as `(0x24A6383, "0F 85 FB 00 00 00")`. Paste them into
`PATCH_DB` in `patch_debug_port.py` (remember to fill in `roots`), then check them:

```
python patch_debug_port.py --browser chrome --dry-run
```

`--locate` matches the compiled instruction skeleton
(`mov eax,2` → `cmp byte [rsp+d],1` → `jne` → `cmp byte [rsp+d],0` → `jne`), with stack
offsets and jump displacements wildcarded, so it still recognises the code after it has
moved. If it reports that it found no gate instructions, a major version changed that
code — use the manual route below.

**Major version / code changed:**

1. Open the new build and confirm the error is still there.
2. Search the DLL for the error string, find its references (RIP-relative xrefs), and
   disassemble the surrounding context.
3. Locate those `cmp` + `jne` pairs and note the file offsets and original bytes.
4. Add them to `PATCH_DB` in `patch_debug_port.py` and validate with `--dry-run`.

A more detailed locating method is in
[docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md) (Chinese).

## Disclaimer

- For your own browser on your own machine only.
- Local debugging / automation / testing only. Not for someone else's device, and not in
  an environment you are not authorized to touch.
- The patch edits the original file; a browser auto-update wipes it and it has to be
  re-applied.
- Understand the risks before use; you bear the consequences.

## License

MIT
