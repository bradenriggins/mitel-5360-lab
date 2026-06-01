# Mitel 5360 Reverse Engineering Notes

## Current Ground Truth

- Phone: Mitel 5360, MAC `08:00:0F:69:43:5B`.
- Current mode: SIP.
- Current phone IP: `192.168.4.33`.
- MacBook lab IP: `192.168.4.30`.
- SIP user: `7001`.
- Confirmed working: phone web UI, SIP registration, INVITE ringing, CANCEL stop, basic RTP tone after answer.
- Firmware visible in web UI: main `06.04.01.08`, boot `06.04.00.03`.

## How Far This Can Go

### Tier 0 - Supported Full GUI Replacement Theory

Mitel's HTML Toolkit Developer Guide explicitly supports Full Screen GUI Replacement Applications on the 5360. In the official MCD/3300 flow, these are packaged as SPX applications, uploaded through the Mitel HTML Application Uploader, pushed with `HTMLAPPUPGRADE`, and assigned as an HTML GUI application in the 3300 System Administration Tool. The guide says this mode is intended to completely replace the phone user interface, can capture all keys, receives full-screen touch events on touch-capable phones, starts automatically, and is not intended to be exited.

The limitation for this standalone SIP lab is that we do not currently have the 3300/MCD app deployment path or a proven SPX package/update path. The network-only substitute is a direct HTML Application URL and programmable `HTML Application` key, which gives us a full-screen app when launched, but not yet the official always-on GRM takeover behavior.

Local research artifacts:

- `$RESEARCH_DIR/DeveloperGuide_HTMLToolkit.pdf`
- `$RESEARCH_DIR/DeveloperGuide_HTMLToolkit.txt`
- `$RESEARCH_DIR/official-grm-route.md`
- `$RESEARCH_DIR/artifact-inventory.md`
- `$RESEARCH_DIR/grm-app-source/`

Found toolkit artifacts:

- `Htmltoolkit_2.2.0.4.zip`, containing `HTMLToolKit_Setup.exe`.
- `Htmltoolkit_accessories.zip`, containing 5360 default/help `.spx` files, `mitel-ip5360-SPX-3.0.0.30-01.noarch.rpm`, and `BootIp5360.bin` / `MainIp5360.bin` / `L2Boot5360Ttn32M.bin`.
- `HTML 2.2 Releasenotes.doc`, which confirms Release 2.2.0.4 and states supported 53xx IP sets are `MiNET only`.
- Full installer payload extracted under `$RESEARCH_DIR/extracted/toolkit-full/`.
- Official Java packager is runnable from the MacBook with OpenJDK and the extracted toolkit libraries.
- Official 5360 Full Screen GUI Replacement sample source and prepackaged `.spx` are now available under `DeveloperResources/`.

### Tier 1 - Safe Software Control

This is what we have now. It is high-confidence and low-risk:

- SIP registrar and call controller.
- Caller ID spoofing for local calls.
- Ring/stop/re-register controls.
- RTP audio injection after answer.
- TFTP/HTTP config serving.
- RSS feed serving.
- Phone-side HTML application serving.
- Phone-side replacement shell at `/app` with home, soundboard, status, and tools pages.
- Phone web admin automation over HTTP Basic auth.
- Config backup from `/download.txt`.
- Official `.spx` packaging through `mitel_package_grm.sh`.
- Repeatable `.spx` delivery probe through the dashboard/API. The probe points the phone at a package URL, confirms the HTTP fetch, then restores the safe `/app` URL.
- Official-style rich GRM package through `mitel_package_grm_rich.sh`.

Next targets:

- Program an RSS or HTML application key so the phone display shows lab text.
- Add soundboard audio instead of sine tone.
- Add webhook triggers from Stream Deck, tablet, browser, Home Assistant, Hue, Discord, etc.
- Add a tiny internal extension map: `7001` phone, `7002` laptop bot, `7003` tablet bot.

### Tier 2 - Firmware Acquisition and Static Analysis

This is realistic but needs care:

- Public/forum evidence says a load set containing `BootIp5360.bin`, `MainIp5360.bin`, `L2Boot5360Ttn32M.bin`, language files, tone files, and `FirmwareVersion.txt` exists.
- Mitel documentation says official SIP firmware loads are behind Mitel OnLine / SIP Software Download Page.
- The phone itself asks TFTP for `MainIp5360.bin` during provisioning.
- Static analysis path:
  - acquire a known-good `MainIp5360.bin`;
  - record size/hash/source;
  - run `file`, `strings`, `binwalk`, entropy, and architecture probes;
  - identify compression, headers, checksums, signatures, and embedded resources;
  - extract only if tools identify real containers;
  - never flash a modified image until we understand boot recovery.

Risk:

- Firmware update docs explicitly warn not to remove power during firmware install.
- Mitel docs also describe `SIP MAIN NOT FOUND` after interrupted boot/main firmware handling, meaning bad flashing can leave the phone needing a recovery load.

### Tier 3 - Firmware Modification

Possible, but not yet safe:

- If `MainIp5360.bin` is unsigned or weakly checked, patches might be possible.
- If signed, edits may boot-fail unless the signature can be bypassed, a vulnerable updater exists, or physical debug access is found.
- Candidate patch classes:
  - replace built-in UI strings/images;
  - unlock hidden web/config pages;
  - alter default app URLs/RSS behavior;
  - patch SIP behavior;
  - add debug telnet/console if latent binaries exist.

Prerequisites:

- Full firmware image set and hashes.
- Known recovery path tested with original firmware.
- Serial console/JTAG/SWD/UART board photos or hands-on probing.
- Packet capture of firmware update flow.

### Tier 4 - Physical / Hardware Reversing

This is the deepest route, but it is explicitly deferred for now because the live network path already exposes enough control surfaces:

- Open the phone and identify SoC, flash chip, RAM, UART pads, JTAG/SWD test pads.
- Dump flash externally or through bootloader if exposed.
- Use serial console to watch boot, environment variables, update verification, and panic logs.
- Only after a verified backup exists, attempt modified firmware.

## Immediate Next Experiments

1. Determine whether a fetched `.spx` can be installed/launched from SIP mode, not just downloaded.
2. Emulate the MCD-side FTP path `/db/htmlapps/apps` and `HTMLAPPUPGRADE` control flow if the phone exposes any compatible request path.
3. Add audio-file playback to the lab and send PCMU RTP to the phone after answer.
4. Add packet captures around firmware-update checks from the web UI.
5. Find a real, complete 5360 SIP firmware load set. Do not flash until hashes and recovery plan are documented.
6. Map the config XML tags back to the web UI fields so changes can be replayed safely from the dashboard.

## Live Web Surface Findings

- `FeatureConfig` exposes `html_url`, which accepts an HTML application URL.
- `AdvancedFeatures` exposes `rss_feed`, `htmlpuseraccess`, `remote_reboot`, SIP registration timing, hotline, TLS root certificate URL, and related SIP behavior fields.
- `ProgramKeyConfig` exposes programmable key features including `RSS Feed` and `HTML Application`.
- `ConfigUpDownload` exposes `/download.txt` for phone-to-PC backup and `/upload.cgi` for PC-to-phone restore.
- `FirmwareUpdate` exposes `Apply`, `Update`, and `Update & Reset Config Files`. The update actions are intentionally off-limits until a known-good firmware set and recovery path exist.

## Current Network-Only Customization

- Local phone HTML app: `http://192.168.4.30/app`.
- Local RSS feed: `http://192.168.4.30/feed`.
- The local HTML app now behaves like a replacement shell:
  - Home page
  - Soundboard page
  - Status page
  - Tools page
  - GET-triggered actions with auto-return to avoid repeated actions on refresh
- Phone config now reports:
  - `<html_enable>1</html_enable>`
  - `<html_filename>http://192.168.4.30/app</html_filename>`
  - `<rss_feed>http://192.168.4.30/feed</rss_feed>`
  - programmable keys `Line="28"` and `Line="58"` with `Fea="20"`, `Des="Apartment Lab"`.
  - both Apartment Lab keys currently point at `http://192.168.4.30/files/ApartmentLabGRM.rich.spx`.
- Safe restore target:
  - global HTML app URL: `http://192.168.4.30/app`
  - dashboard restore: select `Safe HTML shell`, then use `Set global URL` and/or `Set Apartment key`.
- Backups:
  - pre-change backup under `$BACKUP_DIR/5360-config-*.xml`
  - post-change backup under `$BACKUP_DIR/5360-config-after-html-app-*.xml`
  - current backup under `$BACKUP_DIR/5360-config-current-20260521-073008.xml`
  - latest current backup under `$BACKUP_DIR/5360-config-current-20260521-083550.xml`

## Official HTML Toolkit Extraction

- `HTMLToolKit_Setup.exe` was extracted with a locally ported build of `lifenjoiner/ISx`.
- The embedded InstallShield payload produced `data1.cab`, `data1.hdr`, `data2.cab`, setup binaries, and scripts under `extracted/isx-normalized/Disk1/`.
- `unshield` extracted the toolkit to `extracted/toolkit-full/`, including:
  - `dist/HtmlAppPackagerAndInstaller.jar`
  - `keys/Sig1.pem`, `keys/public.der`, and `keys/KeyDefaultPasswords.txt`
  - Packager/uploader docs
  - 5360 sample source folders
  - 5360 prepackaged `.spx` samples, including `5360-FullScreenGUISample.spx`
- OpenJDK is installed through Homebrew and the official command-line packager works.
- `./mitel_package_grm.sh` regenerates `ApartmentLabGRM.official.spx` from `grm-app-source/` and copies it to `$STATIC_FILE_DIR/`.
- `./mitel_package_grm_rich.sh` regenerates `ApartmentLabGRM.rich.spx` from `grm-app-source-rich/` and copies it to `$STATIC_FILE_DIR/`.
- Served package URLs:
  - `http://192.168.4.30/files/ApartmentLabGRM.official.spx`
  - `http://192.168.4.30/files/ApartmentLabGRM.rich.spx`
  - `http://192.168.4.30/files/5360-FullScreenGUISample.spx`

## SIP SPX Delivery Breakthrough

- The phone fetches `.spx` packages from the SIP `FeatureConfig` HTML application URL.
- Confirmed by dashboard/API probe and server log:
  - Mitel sample: `GET /files/5360-FullScreenGUISample.spx`, 24,470 bytes.
  - Apartment tiny official package: `GET /files/ApartmentLabGRM.official.spx`, 1,647 bytes.
  - Apartment rich package: `GET /files/ApartmentLabGRM.rich.spx`, 20,014 bytes.
- Cache-busting query strings are required for reliable repeat probes because the phone may cache by URL.
- After each probe, the global HTML URL was restored to `http://192.168.4.30/app` and verified via `/download.txt`.
- Boot-time provisioning test:
  - served `MN_Generic.cfg` and `MN_08000F69435B.cfg` with `<html_filename>http://192.168.4.30/files/ApartmentLabGRM.rich.spx</html_filename>` and `<htmlapp_mandatory_dwnld>1</htmlapp_mandatory_dwnld>`;
  - phone fetched both config files at boot;
  - phone then fetched `GET /files/ApartmentLabGRM.rich.spx bytes=20014`;
  - phone registered as SIP `7001` afterward.
- Cleanup restore:
  - global HTML URL: `http://192.168.4.30/app`;
  - mandatory download: `0`;
  - both observed Apartment Lab keys point to `http://192.168.4.30/files/ApartmentLabGRM.rich.spx`;
  - lab provisioning server is back to safe `/app`, mandatory `0`.
- The official 5360 GUI Replacement PDF says a Full Screen GUI Replacement app is launched on phone startup or whenever new HTML applications are downloaded, so the remaining unknown is whether SIP `html_url` delivery performs the same install/launch step or only downloads the package.

## Source Notes

- Mitel 5300 SIP document center lists the 5360 SIP User and Administrator Guide.
- Mitel 5312/5324 SIP admin guide describes the same Web Configuration Tool pattern, firmware update flow, automatic firmware polling, and warns that power removal during upgrade can severely damage the phone.
- Tek-Tips archived a MiVoice load directory showing `BootIp5360.bin`, `MainIp5360.bin`, `L2Boot5360Ttn32M.bin`, `mips_mitel.tgz`, and `mips_opensource.tgz`.
- Mitel Forums thread lists the file set people used for 53xx SIP firmware loads, including `BootIp5360.bin`, `MainIp5360.bin`, and `L2Boot5360Ttn32M.bin`.
