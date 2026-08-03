# ADR-0012 — Adapter B (local BLE) approach for WHOOP 5.0 MG

**Status: ACCEPTED 2026-08-03.** The hardware gate in §4 below **passed on the
owner's own WHOOP 5.0 MG strap.** This was the project's single biggest
technical risk (§10.1) and it is now retired.

## Gate result (2026-08-03)

Run by the human in their own terminal — `coach ble scan` then `coach ble probe`
(`adapters/whoop_ble/discover.py`; `bleak` behind the optional `[ble]` extra,
§6.4 signed off). Full enumeration recorded, identifiers scrubbed, at
[`tests/fixtures/whoop_ble/mg_gatt_probe_2026-08-03.json`](../../tests/fixtures/whoop_ble/mg_gatt_probe_2026-08-03.json).

The strap advertised the `fd4b` family in its advertisement data (−53 dBm) and,
on connection, exposed four services:

| Service | What it is |
|---|---|
| `fd4b0001-cce1-4033-93ce-002d5875f58a` | **the 5.0/MG proprietary family** — the gate |
| `0000180d` / `2a37` | **standard SIG Heart Rate Measurement** |
| `0000180f` / `2a19` | Battery Level |
| `0000180a` | Device Information |

**Two findings that change the plan.**

**1. `fd4b0006` is absent on MG.** whoop-vault documents `fd4b0002–0007` on
firmware r52 "Maverick"; this strap exposes `0002, 0003, 0004, 0005, 0007` —
five characteristics, not six. This is precisely the MG-variant divergence this
ADR named as residual risk (a), and it is now a *known concrete difference*
rather than an unknown. The drain implementation must not assume `0006` exists,
and whichever function whoop-vault routes through it needs a different path or
is unavailable here. **This is the thing to check first when the drain is built.**

**2. Live HR needs no reverse engineering at all.** `180d`/`2a37` is plain
Bluetooth SIG. Subscribing to it gives 1 Hz heart rate with no `fd4b` framing,
no CRC, no handshake — a fallback path that survives independently of whether
the proprietary drain keeps working across firmware pushes. That materially
de-risks §10.9: even if WHOOP breaks the `fd4b` protocol tomorrow, the objective
measurement most useful for calibration is still readable.

Residual risk (c) — macOS/CoreBluetooth vs whoop-vault's Linux/BlueZ-only
testing — is now **partly** retired: discovery and GATT enumeration work on
macOS. The *drain* loop is still untested on this stack.

### Blocker archaeology, worth keeping

Three blockers were attached to this task over its life. Two were false when
they were being repeated, and the third was found in seconds by executing it:

- ~~"needs the physical strap"~~ — false for the whole project; it is worn daily.
- ~~"needs a `bleak` dependency decision"~~ — signed off 2026-08-02.
- **macOS TCC Bluetooth denial** — real. `BleakScanner.discover()` died with
  SIGABRT (exit 134) and no Python exception; the C stack showed
  `__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`. Granting Bluetooth to the host
  application, **and restarting it**, cleared it. The restart is the part that
  is easy to miss: TCC decisions are cached per responsible app at launch.

The lesson this ADR should carry forward: **check whether a documented blocker
is still real before repeating it**, and prefer running the thing to reasoning
about it.

## Context

§4's calibration play requires a local BLE adapter (Adapter B) before the WHOOP
membership lapses (~9 months). At the time §4 was written, 5.0 MG local read
was rated **UNPROVEN — the project's single biggest technical risk**. This ADR
records fresh recon (2026-07-25) of the community ecosystem and picks the
approach for the eventual adapter.

## What recon found (state of the art, July 2026)

**The 5.0 protocol is no longer a black box.** Two independent projects speak
it today:

- **[whoop-vault](https://github.com/Sophonbot0/whoop-vault)** — Python 3.10+ /
  [Bleak](https://github.com/hbldh/bleak) / MIT. Talks the same protocol as the
  official Android app (reverse-engineered from the decompiled APK, firmware
  **r52 "Maverick"**). Live 1 Hz HR (standard GATT 0x2A37), skin temp
  (0.01 °C), motion + gravity vector, battery; **full historical drain** —
  per-second HR/temp/motion/activity/on-body spanning weeks — via a
  `SEND_HISTORICAL_DATA` ACK loop at ~120 chunks/s. Protocol facts we inherit:
  custom characteristics `fd4b0002–0007`, CRC16 frame header + CRC32 payload,
  mandatory 4-byte alignment (strap silently drops unaligned commands),
  pre-sync handshake (cmd 96 `ENTER_HIGH_FREQ_SYNC` before cmd 22). Caveats:
  tested only on Linux/BlueZ; no MG-variant mention; no raw PPG, no live IMU.
- **[NOOP](https://github.com/ryanbr/noop)** — Swift/Kotlin apps,
  PolyForm-Noncommercial. **Claims 5.0/MG live HR working today**; recovery/
  strain/sleep on 5.x still experimental. Confirms the service-family split:
  `61080001-…` + CRC8 = 4.0, **`fd4b0001-…` + CRC16-Modbus = 5.0/MG**. ~14-day
  on-strap history auto-offloads.
- **[OpenStrap/edge](https://github.com/OpenStrap/edge)** — 4.0 only, explicitly
  ("haven't touched a WHOOP 5"). Useful as protocol prior art
  ([Hackaday write-up](https://hackaday.com/2026/07/15/making-a-locked-down-wearable-work-without-a-subscription/)),
  not as a 5.0 path.

**Risk downgrade:** 5.0 local read is now *demonstrated* (live + historical) on
at least Maverick-variant 5.0 hardware, and NOOP claims MG live HR. Residual
risk is narrower: (a) the user's exact **MG variant/firmware** vs r52 Maverick,
(b) firmware updates breaking the protocol (WHOOP pushes them via the official
app), (c) macOS/CoreBluetooth vs the Linux-only-tested drain path.

**New constraint discovered — the calibration play needs a schedule.** The
strap holds an **encrypted BLE bond with one client at a time**. While bonded
to the laptop for a local drain, the phone's official app cannot sync (and vice
versa). "Run Adapter A and Adapter B continuously in parallel" is physically
impossible; the play becomes **time-sliced**: let the phone sync normally
(Adapter A gets official scores), then periodically re-bond for a local
historical drain (Adapter B gets the same period's raw series retroactively —
the ~14-day on-strap buffer makes this lossless as long as drains happen more
often than the buffer wraps). Sibling rows + read-time precedence (§2.3)
already model this perfectly; nothing in the schema changes.

## Decision (proposed)

1. **Target the `fd4b` protocol family directly, in Python + Bleak,** using
   whoop-vault (MIT) as the protocol reference — same language as this repo,
   permissive license, and the only implementation of the historical drain we
   can read line-by-line. NOOP's apps are the fallback reference for
   MG-specific quirks.
2. **Historical drain over live streaming.** The calibration currency is §5's
   objective measurements; the drain delivers a complete per-second series
   without keeping a laptop radio connected all day, and it composes with the
   time-sliced bonding schedule.
3. **Adapter shape unchanged:** `adapters/whoop_ble/` writing verbatim drain
   payloads to `raw_events` (`source='whoop_ble'` — the CHECK is gone,
   ADR-0009), pure normalizers deriving objective measurements only
   (`hrv_rmssd_ms`, `resting_hr_bpm`, `skin_temp_c`, …) with
   `score_method='textbook'`/`is_official=0` for any derived score, exactly the
   sibling-row calibration design §5 planned.
4. **Gate before any code:** a one-evening hardware spike — bond the user's own
   MG strap from the laptop, attempt discovery of `fd4b0001` services and a
   short drain. Its outcome (works / MG-differs / blocked) is the acceptance
   test that flips this ADR to Accepted or forces a rethink.
   **→ PASSED 2026-08-03; see the Gate result section above.** Discovery and
   enumeration confirmed; the drain itself is not yet attempted.

## Alternatives rejected

- **Adopt NOOP wholesale as the ingestion path.** Wrong shape (phone apps, not
  a library), PolyForm-Noncommercial complicates any future productization,
  and §2.1 wants raw bytes in *our* store, not another app's silo.
- **Wait for the ecosystem to mature.** The membership clock (~9 months) and
  the firmware-update risk both argue for proving MG viability early, while
  the paid window still provides ground truth to calibrate against.

## Consequences

- CLAUDE.md §4/§10.1's "local-read viability UNPROVEN" is now stale; updated to
  "demonstrated on 5.0 (Maverick), MG variant pending the hardware spike".
- The calibration plan gains an operational requirement: a drain cadence
  shorter than the on-strap buffer (~14 days), and a documented re-pairing
  routine between laptop and phone.
- Firmware updates remain the standing threat (§10.9); mitigation unchanged —
  raw is sacred, adapters are the only vendor-aware code, and the official API
  adapter keeps working while the membership lasts.
