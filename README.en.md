# debug-default-profile

[中文](README.md) · **English**

One script, two binary patches for **official** (non-self-built) Chrome / Edge:

1. `patch_browser.py debug-port` — let Chrome / Edge open a remote debugging port (CDP) while
   running on the **default user data directory**.
2. `patch_browser.py no-debugger` — make the browser ignore `debugger` statements in JS
   (defeats the "infinite debugger" anti-debugging trick).

The two subcommands are independent: use either or both. Different patch sites, different
backup suffixes (`.orig.bak` / `.nodebug.bak`), they never overwrite each other.

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

## Configuration file `patch_db.json`

Everything that changes with a browser update lives in `patch_db.json` next to the script;
edit that file instead of `patch_browser.py`:

```json
"brands": {
  "chrome": { "dll": "chrome.dll", "roots": ["C:\\Program Files\\Google\\Chrome\\Application", "…"] }
},
"patch_db": {
  "chrome": {
    "154.0.8037.93": {
      "0x24A6383": "0F 85 FB 00 00 00",
      "0x24A6391": "0F 85 ED 00 00 00"
    }
  }
}
```

- `brands`: brand → DLL file name + install search roots (the only place to add a brand).
- `patch_db`: `debug-port` patch sites; each version maps
  `"file offset (hex)" → "original bytes (hex)"`. The original bytes are checked before any
  write, so a mismatching build is refused. Entries are **append-only** — old versions are
  kept as the basis for rollback and forensics.
- No hand-editing needed: `debug-port --locate --save` writes newly located sites back into
  this file, and `auto` records them automatically when it patches an unknown version.
- `no-debugger` derives its single patch site on every run, so it is not stored here.

## Usage

The command is always `python patch_browser.py <subcommand> [options]`, and there are
three subcommands:

| Subcommand | What it does | Backup suffix |
| --- | --- | --- |
| `debug-port` (alias `cdp`) | CDP port on the default user data directory | `.orig.bak` |
| `no-debugger` (alias `nodebug`) | ignore JS `debugger` statements | `.nodebug.bak` |
| `auto` | one path in (or `--all` for every brand's newest install), everything else derived | same as above |

**Easiest of all: double-click [一键修复.bat](一键修复.bat).** A browser auto-update wipes both
patches; double-clicking it asks for UAC elevation, offers to close the browser if it is running,
and re-applies both patches to the **newest** Chrome / Edge install. A version that is not in
`patch_db.json` yet gets located on the spot and recorded. It is equivalent to
`python patch_browser.py auto --all`. To see what it would do without writing anything (and
without administrator rights):

```
一键修复.bat --dry-run
```

The two patch subcommands take the same options; the examples below use `debug-port` — just
swap the subcommand for the other tool.

Can't be bothered to find the paths? `auto` takes a single path — `chrome.dll` / `msedge.dll`,
the browser exe, the `Application` directory or a version directory — and derives the brand,
the version and the patch sites itself: an unlisted version is located from the instruction
shape on the spot, the sites are then written back into `patch_db.json`, and anything already
patched is skipped. Add `--dry-run` first to see what it intends to do:

```
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe" --dry-run
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

To handle only one of the two patches, pass `--only debug-port` or `--only no-debugger`.
With no path at all, `--all` walks every brand in `patch_db.json` and handles the newest
installed version of each (this is what `一键修复.bat` runs); leftover older version
directories are skipped and reported.

Not sure where you stand? **Run it with no subcommand**: it checks both patches against both
browsers (installed versions, patch state, backups, write permission, whether the browser is
still running) and then prints the exact next command plus a quick tutorial. It is read-only,
writes nothing and needs no administrator rights:

```
python patch_browser.py
```

Close the browser first, then run as Administrator:

```
python patch_browser.py debug-port --browser chrome
python patch_browser.py debug-port --browser edge
```

`Users` has read-only access under `Program Files`, so **elevation is mandatory**.
When you are not an administrator — or the browser is still running — the tool stops
before writing and tells you why; it never leaves a half-written backup or a
half-applied patch. To find out whether you have permission first, run `--dry-run`:
its last line reports whether the target is writable (`写入权限：可写 / 不可写`). If the DLL
is already patched, it stops earlier to say so and never prints that line.

Verify only, no writes:

```
python patch_browser.py debug-port --browser chrome --dry-run
```

Roll back:

```
python patch_browser.py debug-port --browser chrome --restore
```

Re-running on an already-patched DLL writes nothing — it simply reports
`该 dll 已打过补丁：N 个补丁点均为 NOP` (already patched, all N sites are NOPs). That makes
`--dry-run` usable as a health check, and it has two ways of reporting "healthy":

| Output | Meaning | What to do |
| --- | --- | --- |
| `该 dll 已打过补丁：N 个补丁点均为 NOP` | Patch is in place | Nothing |
| `dry-run：N 个补丁点全部匹配，未写入` | Version is supported but the patch is gone (the browser recompiled after an update) | Re-apply, elevated |

Only `校验失败，偏移 0x...` needs action: that build is not in `PATCH_DB` yet, so re-locate
the patch sites with `--locate` first.

Other flags (both subcommands):

- `--path` — target the DLL directly (may be used alone; the brand is inferred from the file name)
- `--version` — pin the version
- `--backup-dir` — where the backup goes (default: next to the DLL)
- `--no-backup` — do not create a backup
- `--list` — list supported versions and what is detected on this machine
- `--locate` — read-only scan that prints patch sites; for `debug-port` the output can be
  pasted straight into `PATCH_DB`

Verify: launch the browser with `--remote-debugging-port=9222` and open
http://127.0.0.1:9222/json/version.

## Adding a new version

**Minor auto-update (the common case):** the gate code barely changes, it just moves.

```
python patch_browser.py debug-port --browser chrome --locate
python patch_browser.py debug-port --browser edge --locate
```

It prints patch sites such as `"0x24A6383": "0F 85 FB 00 00 00",` (a JSON fragment). Paste the
whole block into the matching brand/version section of `patch_db.json`, then check it with
`--dry-run`. To skip the manual paste, add `--save`:

```
python patch_browser.py debug-port --browser chrome --locate --save
python patch_browser.py debug-port --browser chrome --dry-run
```

Even easier is `auto`, which locates, validates, backs up, writes and records in one go:

```
python patch_browser.py auto --path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

`--locate` matches the compiled instruction skeleton
(`mov eax,2` → `cmp byte [rsp+d],1` → `jne` → `cmp byte [rsp+d],0` → `jne`), with stack
offsets and jump displacements wildcarded, so it still recognises the code after it has
moved. When the gates it finds are **already NOPs**, it says "already patched" instead of
misreporting "no gate instructions found". The DLL name and the search roots live in
`brands` in `patch_db.json` — edit that table when you add a brand. If it reports that it
found no gate instructions, a major version changed that code — use the manual route below.

**Major version / code changed:**

1. Open the new build and confirm the error is still there.
2. Search the DLL for the error string, find its references (RIP-relative xrefs), and
   disassemble the surrounding context.
3. Locate those `cmp` + `jne` pairs and note the file offsets and original bytes.
4. Add them to `patch_db` in `patch_db.json` and validate with `--dry-run`.

A more detailed locating method is in
[docs/cdp_user_data_dir_check.md](docs/cdp_user_data_dir_check.md) (Chinese).

## Second subcommand: ignore JS `debugger` statements

Anti-scraping sites love to sprinkle `debugger;` inside hot loops, getters and event
handlers. As long as DevTools is attached the flow stops there, the page freezes and even
the DevTools UI becomes unusable. This subcommand edits the browser binary so it becomes
a no-op.

### How it works (30-second version)

Whether or not the code got JIT-compiled, `debugger;` always ends up calling the same
runtime function `Runtime_HandleDebuggerStatement` (the interpreter goes through
`Bytecode::kDebugger`; optimized code goes through the TurboFan operator `JSDebugger` →
`LowerJSDebugger()`). Its body has exactly one gate:

```cpp
if (isolate->debug()->break_points_active()) { …HandleDebugBreak… }
return isolate->stack_guard()->HandleInterrupts();
```

which compiles to `cmp byte ptr [rcx+disp], 1` + `jne <skip>`. Turning that `jne` into an
**equal-length** `jmp` makes the `if` body unreachable — the file length does not change and
the trailing `HandleInterrupts()` still runs.

**Manual line breakpoints are unaffected**: they go through
`Runtime::kDebugBreakBytecodes`, a different path.

### Locating it inside a 289 MB dll with no symbols

No symbols, no hardcoded offsets: entries of V8's `kIntrinsicFunctions[]` table carry a
pointer to the function name string (`Runtime::Function` is
`{id, type, name*, entry*, nargs, result_size}`, 32 bytes on x64). So: find the VA of
`"HandleDebuggerStatement\0"` → find the 8-byte pointer referencing it → entry at
struct `+16`. Neighbouring entries are structurally validated (ids strictly ±1, type ∈ {0,1},
readable names, entries inside `.text`).

**Because it re-derives on every run, browser auto-updates usually need no code change.**

### Usage

Close the browser first, then run as administrator:

```
python patch_browser.py no-debugger --browser chrome
python patch_browser.py no-debugger --browser edge
```

Locate and verify only, no write:

```
python patch_browser.py no-debugger --browser chrome --dry-run
python patch_browser.py no-debugger --list          # status of every installed version
```

Roll back (suffix `.nodebug.bak`, never clobbers the CDP tool's `.orig.bak`):

```
python patch_browser.py no-debugger --browser chrome --restore
```

Flags match `debug-port`: `--path` / `--version` / `--backup-dir` / `--no-backup` / `--locate` / `--list`.

### Verification

`debugger` is a no-op when no debugger is attached, so **checking that the port responds is a
false PASS**. The debugger must actually be attached (opening DevTools does `Debugger.enable`)
before you can watch for a pause. Manual check:

1. Close the browser, start it with the debugging port, then press F12 for DevTools:

   ```
   chrome.exe --remote-debugging-port=9222
   ```

2. In the Console run `(function(){debugger;return 42})()`:
   - **patched**: it returns `42` right away and the Sources panel never pauses;
   - **unpatched** (control): the Sources panel pauses on the `debugger` line.
3. Regression: set a manual line breakpoint on any function in Sources and trigger it — it
   must still pause, which proves manual breakpoints were not damaged.

Measured 2026-10-01 on a full copy of the install directory (real install untouched):
unpatched → `paused:true` (control), patched → `paused:false / returns 42`, manual breakpoint
still `paused:true @ bpTarget:1`, after `--restore` → `paused:true` again. Exactly one byte
differs: `0x71D1AD2: 0x75 -> 0xEB`.

Full reverse-engineering notes and maintenance steps:
[docs/no_debugger_statement.md](docs/no_debugger_statement.md) (Chinese).

## Disclaimer

- For your own browser on your own machine only.
- Local debugging / automation / testing only. Not for someone else's device, and not in
  an environment you are not authorized to touch.
- The patch edits the original file; a browser auto-update wipes it and it has to be
  re-applied.
- Understand the risks before use; you bear the consequences.

## License

MIT
