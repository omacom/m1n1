# Startup and recovery options

Configured Stage 1 images boot their selected Stage 2 immediately by default.
`tools/fill_stage1_config.py` sets the proxy window to zero unless
`--window-ms` explicitly requests a development interception window.
Changing this default does not modify an already enrolled boot object:
configure and enroll a new Stage 1 to replace an older 15-second window.

The boot-argument tokens are matched exactly:

- `m1n1-debug` selects DebugUSB early and prevents CDC from taking its carrier.
- `m1n1-proxy` skips Stage 1 payload loading and enters the proxy. It is ignored
  by an ESP-loaded Stage 2, so a deliberately chainloaded Stage 2 can boot.
- `m1n1.wdt=keep` leaves the SoC watchdog as this m1n1 found it instead of
  disabling it at start. It is for a chainloading stage that armed the watchdog
  before starting this one and appends the token to the next stage's boot-args
  command line: a hang in this m1n1, in the proxy or in a kernel that never
  takes the watchdog over then resets the machine, and iBoot counts a failed
  boot, so that stage can boot its fallback. Linux's `apple_wdt` keeps an armed
  watchdog fed once it probes. m1n1 itself does not feed it, so everything
  before that, including waiting in the proxy, has to finish within the timeout
  the earlier stage chose. On T8140 the CDC proxy window then neither
  re-arms the watchdog for 170 s nor disarms it when the host opens the
  proxy. Without the token m1n1 disables the watchdog as before. It is honoured by every stage, so it does not belong in the NVRAM
  boot-args: there it would leave iBoot's watchdog running.

The options may be combined. `m1n1-debug` alone does not request a proxy pause.
Normal payload failure still follows the existing recovery fallback.

## Common build profiles

Stage 1: `make RELEASE=1 CHAINLOADING=1 BUILTIN_LOGO=aurora`.
Stage 2: add `ESP_STAGE2=1` to that command. Preserve each stage before rebuilding.
These profiles are shared across boards. The embedded ESP configuration is
consumed on every supported chip, and normal boot attempts its payload before
initializing the USB proxy. An explicit proxy request or missing payload starts
the platform's USB transport; `m1n1-debug` retains DebugUSB instead.

The former Neo CDC/KIS make switches and Neo Stage 2 alias have been removed.
The raw image links both USB protocol engines; the PHY chooses the engine at
runtime. The native m1n1 image retains its native USB/DART implementation.
Physical register and firmware handoff differences remain runtime driver logic.

Cross-platform hardware qualification is required before replacing installed
boot objects; host tests and successful links do not establish that qualification.
