# 📸 Photobooth Web System

Event-ready web-based photobooth system built with **Flask and OpenCV**. Photos are captured with each visitor's own device camera in the browser and processed on the server.

Supports:

* Browser camera capture on phones, tablets, and computers
* Multi-slot frame auto detection (transparent or green key)
* Photo capture count automatically matches the selected frame's detected slots
* A QR-linked gallery for the composed result and individual photos

Can run on a local network or a public HTTPS domain.

---

# 🏗 Architecture Overview

Frontend:

* Vanilla HTML/CSS/JS
* Browser camera preview and capture (`getUserMedia`)
* Countdown, multi-shot capture, and individual retakes
* QR result gallery

Backend:

* Flask API
* Frame slot auto-detection
* Photo strip composer
* Local per-session photo storage

---

# 📂 Project Structure

```
photobooth_web/
│
├── app.py
├── make_frame.py
├── requirements.txt
├── README.md
│
├── templates/
│   ├── index.html
│   └── session_results.html
│
├── frames/
│   ├── frame1.png
│   ├── frame2.png
│
└── output/        (auto-generated, ignored by git)
```

---

# ⚙️ Installation

## 1️⃣ Clone Repository

```bash
git clone https://github.com/USERNAME/photobooth_web.git
cd photobooth_web
```

---

## 2️⃣ Create Virtual Environment (Recommended)

```bash
python -m venv venv
venv\Scripts\activate      # Windows
```

---

## 3️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

---

# 🎞 FFmpeg (Optional but Recommended)

For better video compatibility (H264):

Install FFmpeg and ensure:

```bash
ffmpeg -version
```

works in terminal.

Or put `ffmpeg.exe` inside:

```
_ffmpeg/
```

---

# 🚀 How To Run

```bash
python app.py
```

Server runs at:

```
http://localhost:8000
```

For a public VPS deployment, serve the Flask app behind a web server with HTTPS. Set `PUBLIC_BASE_URL` to the public HTTPS origin before starting the app so the QR gallery uses the public domain. In PowerShell:

```powershell
$env:PUBLIC_BASE_URL = "https://booth.example.com"
python app.py
```

To mount the app at a path on an existing domain, such as `https://example.com/photobooth`, set `APP_BASE_PATH=/photobooth` and `PUBLIC_BASE_URL=https://example.com/photobooth`. Configure the reverse proxy to strip `/photobooth/` before forwarding requests to Flask, and preserve the trailing slash with a redirect from `/photobooth`.

The default local URL fallback is useful for development, but is not suitable behind a reverse proxy. Browser camera access requires HTTPS (localhost is also allowed by browsers). Configure DNS and TLS for the domain; opening an HTTP page on a phone, tablet, or computer will not grant camera access.

Each capture is stored under its own `output/sessions/<session-id>/` directory on the server, with the composed result and individual original photos. If the frame has transparent areas, the composed result is saved as PNG to preserve them instead of adding a white background; otherwise it is saved as JPEG. When transparency is present, a second JPEG uses the frame's dominant edge color as its background for sharing as a regular photo. The QR code opens a gallery containing the composed result, the optional color-background JPG, and every individual photo. Photos are delivered through this QR gallery only; no email or Google Drive connection is used.

Session metadata is stored in SQLite at `output/photobooth.sqlite3`; photos remain separate files and are not stored in the database. Session records persist across app restarts, while only the latest 300 sessions are kept in the in-memory cache and older sessions are loaded from SQLite on demand. Anyone with a session's QR link can view and download its photos, so keep the database private and include it in backups. Sessions are not automatically deleted.

For local event-network use, the server can also be opened via its local IP:

```
http://192.168.x.x:8000
```

---

# 📷 How To Use (Event Flow)

## 1️⃣ Landing Screen

* Pilih salah satu frame.
* Tentukan jumlah foto, dari 3 sampai 6.

---

## 2️⃣ Booth Screen

* Izinkan kamera browser, pilih kamera belakang/depan, lalu sentuh **MULAI FOTO** dan ikuti hitung mundur untuk setiap jepretan.
* Preview hasil sudah menampilkan foto di dalam frame. Pilih **GESER & PAS** untuk menggeser foto pada slotnya dan gunakan tombol minus/plus untuk zoom. Tekan **SIMPAN POSISI** untuk menerapkan perubahan atau **POSISI TENGAH** untuk mereset geseran.
* Pilih **RETAKE** jika ingin mengambil ulang foto tertentu.
* Lanjutkan untuk menyusun foto sesuai tata letak frame yang dipilih.

---

## 3️⃣ Result Screen

* Scan QR untuk membuka galeri dan mengunduh hasil gabungan serta foto satuan.
* Mulai sesi baru untuk peserta berikutnya.

Tautan galeri memakai ID sesi acak; siapa pun yang memegang QR atau tautannya dapat membuka dan mengunduh foto sesi tersebut.

URL lokal lama yang memakai nama file deterministik tidak lagi disajikan setelah pembaruan keamanan. File lama tetap berada di `output/`, tetapi buat hasil baru untuk mendapatkan tautan QR dengan ID sesi acak. Endpoint kamera server lama juga dinonaktifkan; kamera diambil langsung oleh browser pengunjung.

---

## 📱 Camera Devices

Buka domain HTTPS dari browser di smartphone, tablet, atau komputer berkamera. Setelah pengguna memberi izin kamera, foto diambil oleh perangkat itu sendiri dan dikirim ke VPS untuk dirangkai. Pilihan kamera depan/belakang akan digunakan jika perangkat menyediakannya; komputer dengan satu webcam tetap dapat menggunakan kamera default. Gunakan tombol **LAYAR PENUH** untuk tampilan booth. Untuk domain publik, pastikan `PUBLIC_BASE_URL` sama dengan alamat HTTPS yang dibuka pengunjung agar QR menunjuk ke tautan yang benar.

# 🖼 Frame System

Place PNG frames inside:

```
frames/
```

The project currently includes 11 frame designs. Portrait frames place one photo in each detected slot; **Six Snap Grid** places one photo in each of six slots arranged in a 3-column × 2-row layout.

Supported slot types:

### 1️⃣ Transparent Slot

Make hole area truly transparent in PNG (alpha 0). A checkerboard drawn by an image generator is still opaque pixel data and will not count as transparency. The app automatically captures one photo per detected slot; a frame with one slot takes one photo, while a multi-slot frame such as **Six Snap Grid** requires the same number of photos as slots.

### 2️⃣ Green Key Slot

Use exact color:

```
#01e678
```

System auto-detects slot positions.

---

# 🛠 Create Frame From Design

Use:

```bash
python make_frame.py --in newspaper.png --out frame_transparent.png
```

Advanced options available:

* Feather
* Despill
* Margin
* Green tolerance

---

# 🧠 Session Handling

* SQLite metadata storage (`output/photobooth.sqlite3`)
* Bounded in-memory cache of the latest 300 sessions

---

# 🖥 Deployment Notes

Recommended for:

* Local event machine
* Windows laptop with DSLR capture
* Same WiFi network for QR access

Not optimized for:

* Public cloud deployment
* High concurrency SaaS

---

# 📦 Future Improvements

* Docker container
* Upload progress bar
* Admin dashboard
* Multi-event configuration
