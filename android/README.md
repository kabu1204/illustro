# illustro sync (Android uploader)

Minimal native app that uploads images from an Android phone to an illustro
server — one-way, in the background, with live progress.

Why native: on Android, browser file access goes through the system Storage
Access Framework, which is far too slow for thousand-file folders and gives no
progress feedback during enumeration. The app reads storage directly
(`java.io` fast path on internal storage, SAF walker as fallback) and reports
every file as it is discovered, hashed, and uploaded — in the app and in an
ongoing notification.

## Get the APK

No local Android toolchain needed: pushing anything under `android/` to `main`
triggers the `android-build` workflow. Download **illustro-sync-debug-apk**
from the latest run on the Actions page, transfer it to the phone, and install
(allow "install unknown apps" for your browser/file manager when prompted).

## Use

1. Enter the server URL (e.g. `http://192.168.1.10:8000`) and the sync token if
   one is set (`sync.token` in config.yaml).
2. **Pick folder…** — the grant is remembered; re-picking is only needed for a
   different folder. Grant the photos/media permission when asked (it enables
   the fast direct-storage walk; without it the app still works via SAF).
3. **Start upload**. The service runs as a foreground service with a partial
   wake lock, so it keeps going with the screen off. Progress: current file,
   processed/total, new vs already-known counts, bytes and speed.
   Only files directly inside the picked folder are uploaded — subfolders are
   skipped. Processing on the server is not auto-triggered; start it from the
   web UI's worker controls ("Process now") when you want tagging to run.

Interrupted (killed app, lost Wi-Fi, tapped Stop)? Tap Start again — files
already uploaded are recognized via a local ledger (name+size+mtime -> hash)
and skipped instantly; the server additionally drops exact duplicates.
Same image with different compression is uploaded and later flagged by
illustro's near-duplicate analytics, just like the web uploader.

## Notes

- Android 14+ caps `dataSync` foreground services at ~6h/day. If a huge
  backfill is cut short, just tap Start again to resume.
- Plain `http://` LAN URLs are allowed (`usesCleartextTraffic`).
- Rebuilding locally: open `android/` in Android Studio, or
  `gradle -p android assembleDebug` with any Gradle 8.7+ / JDK 17.

## Layout

- `app/src/main/java/com/illustro/sync/MainActivity.kt` — settings UI, folder pick, permissions
- `app/src/main/java/com/illustro/sync/UploadService.kt` — foreground service: enumerate -> hash -> upload
- `app/src/main/java/com/illustro/sync/State.kt` — live progress state, prefs, local hash ledger (SQLite)
