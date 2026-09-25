# Savitar v1.0.0 — Fast Video Downloader (Official Windows Release)

⚡ **Savitar v1.0.0** is the inaugural production release of the fastest multi-threaded 4K video downloader for Windows.

---

### 🌟 What's New & Core Capabilities

- **🚀 16-Thread Turbo Acceleration**: Segregated multi-connection download pipeline with optimized connection pooling via `aria2c` and `yt-dlp`.
- **🎥 4K / 8K / 60fps Preservation**: Lossless DASH video stream capture with automatic audio track muxing.
- **🌐 Platform Gateways**:
  - **YouTube**: Videos, Shorts, Playlists, Channels, and Audio (MP3/AAC/FLAC).
  - **TikTok**: HD watermark-free video downloads and direct audio extraction.
  - **Instagram**: Reels, Stories, and Carousel video preservation.
  - **Facebook**: High-definition video streams and clips.
  - **X (Twitter)**: Full bitrate MP4 capture.
- **🔒 Sandboxed JavaScript Runner**: Embedded `QuickJS` engine safely executes dynamic signature extractors.
- **🎨 Modern Fluent Desktop Interface**: Dual Dark & Light themes, live speed graph, active queue, and history dashboard.
- **📦 Offline Standalone Setup**: Full installer bundles Microsoft Edge WebView2 Evergreen Runtime (258 MB) and VC++ 2015-2022 (25 MB) — **zero internet connection required during installation**.

---

### 📦 Release Asset & Checksum

| Asset Name | Architecture | Type | File Size | SHA256 Checksum |
| :--- | :--- | :--- | :--- | :--- |
| **`Savitar-Setup-v1.0.0.exe`** | Windows x64 | Standalone Installer | 387.3 MB | `127cc2baa53d983c760aadc84099955d60b502ad6fcf2158b0d235c6f2faeae6` |

---

### 🛡️ Verify Download Authenticity

Verify your download in Windows PowerShell:
```powershell
Get-FileHash -Path "Savitar-Setup-v1.0.0.exe" -Algorithm SHA256
```
Expected output:
```text
127cc2baa53d983c760aadc84099955d60b502ad6fcf2158b0d235c6f2faeae6
```

---

### 💻 Compatibility
- **Windows 10 / Windows 11 (64-bit)**
- Single instance lock enabled (avoids corrupted parallel runs).
