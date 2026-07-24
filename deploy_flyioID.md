# 🚀 Panduan Deploy WidgetFM ke Fly.io

## Prasyarat
- Akun [Fly.io](https://fly.io) (bisa daftar dengan GitHub)
- Fly.io mungkin meminta kartu kredit untuk verifikasi — **tidak akan dicharge** selama di dalam free tier

---

## Langkah 1 — Install flyctl (CLI Fly.io)

Buka **PowerShell** dan jalankan:

```powershell
winget install flyctl
```

Atau download manual dari: https://fly.io/docs/hands-on/install-flyctl/

Verifikasi instalasi:
```powershell
flyctl version
```

---

## Langkah 2 — Login ke Fly.io

```powershell
flyctl auth login
```

Browser akan terbuka untuk login/signup.

---

## Langkah 3 — Inisialisasi App (jalankan dari folder widgetfm)

```powershell
cd e:\Bot\widgetfm
flyctl launch --no-deploy
```

Fly.io akan bertanya:
- **App name**: masukkan nama unik, misal `widgetfm-rizukiretz`
- **Region**: pilih `sin` (Singapore) — terdekat ke Indonesia
- **Would you like to set up a PostgreSQL database?**: **No**
- **Would you like to set up an Upstash Redis database?**: **No**

Ini akan membuat file `fly.toml` secara otomatis.

---

## Langkah 4 — Set Secrets (API Keys & Tokens)

**JANGAN taruh .env di server** — gunakan Fly.io secrets sebagai gantinya.
Secrets disimpan terenkripsi di Fly.io dan tidak akan pernah terlihat setelah di-set.

```powershell
flyctl secrets set `
  LAST_FM_USERNAME="(username Last.FM kamu)" `
  API_KEY="(Last.FM API key kamu)" `
  USER_ID="(Discord user ID kamu)" `
  APPLICATION_ID="(Application ID widget Listening Stats)" `
  BOT_TOKEN="(Bot Token widget Listening Stats)" `
  TOPARTISTS_APPLICATION_ID="(Application ID widget Top Artists)" `
  TOPARTISTS_BOT_TOKEN="(Bot Token widget Top Artists)"
```

**Secrets opsional** — tambahkan hanya jika kamu menggunakan fitur ini:

```powershell
# stats.fm — wajib untuk widget Top Artists dan slot hoursstreamed/minutesstreamed
flyctl secrets set STATSFM_USERNAME="(username stats.fm kamu)"

# Spotify — fallback album art (jalankan spotify_auth.py di lokal dulu)
flyctl secrets set `
  SPOTIFY_CLIENT_ID="(Spotify Client ID)" `
  SPOTIFY_CLIENT_SECRET="(Spotify Client Secret)" `
  SPOTIFY_REFRESH_TOKEN="(Spotify Refresh Token)"

# imgfixer — wajib hanya jika IMGFIXER_ENABLED = True di config.py
flyctl secrets set DISCORD_IMAGE_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

> Lihat file `.env.example` untuk deskripsi lengkap setiap variabel.

> ⚠️ **Spotify auth harus dilakukan di lokal.** `spotify_auth.py` membuka browser dan menjalankan HTTP server lokal — tidak bisa berjalan di Fly.io. Dapatkan refresh token di lokal dulu (`python spotify_auth.py`), lalu tambahkan sebagai secret di atas.

> ℹ️ **Value di file `.env` menggunakan tanda kutip sebagai delimiter** (`KEY='value'` atau `KEY="value"`). Library `python-dotenv` otomatis menghapus tanda kutip tersebut saat berjalan di lokal. Saat set Fly.io secrets, paste **hanya nilai tokennya saja — tanpa tanda kutip pembungkusnya**.
> Contoh: jika `.env` berisi `SPOTIFY_REFRESH_TOKEN='AQDxxxxx'`, maka command Fly.io yang benar adalah `flyctl secrets set SPOTIFY_REFRESH_TOKEN="AQDxxxxx"` (tanpa single quote).

---

## Langkah 4b — Menambahkan Secrets ke App yang Sudah Ter-deploy

Jika kamu sudah deploy sebelumnya dan ingin menambahkan secrets baru (misal Spotify keys yang baru ditambahkan):

```powershell
# Set satu atau beberapa secrets — Fly.io akan otomatis redeploy
flyctl secrets set `
  SPOTIFY_CLIENT_ID="..." `
  SPOTIFY_CLIENT_SECRET="..." `
  SPOTIFY_REFRESH_TOKEN="..."
```

Untuk menambahkan beberapa secrets sekaligus tanpa memicu redeploy berulang, gabungkan dalam satu command seperti di atas — Fly.io hanya redeploy sekali.

Untuk melihat secrets yang sudah ter-set (nilainya tersembunyi):
```powershell
flyctl secrets list
```

---

## Langkah 5 — Deploy

```powershell
flyctl deploy
```

Fly.io akan:
1. Build Docker image dari `Dockerfile`
2. Push ke registry Fly.io
3. Deploy ke server di Singapore
4. Script langsung berjalan otomatis

---

## Monitoring — Cek Log Real-Time

```powershell
flyctl logs
```

Output normal saat startup:
```
=======================================================
   WidgetFM — Starting
   Listening Stats : ✓ fast=20s / slow=60s
   Top Artists     : ✓ rotate=ON | All Time → 6 Months → 30 Days | 25s/step
=======================================================
[Cache] Global cache ready: 12 artist(s) cached
[LS] Thread started
[LS] Status: Now Playing | Some Song — Some Artist
[LS] Discord → 204 | Rate: 2/3 remaining, resets in 20.0s
```

---

## Perintah Berguna

| Perintah | Fungsi |
|---|---|
| `flyctl logs` | Lihat log real-time |
| `flyctl status` | Cek status app (running/stopped) |
| `flyctl restart` | Restart script |
| `flyctl secrets list` | Lihat nama secrets (nilai tersembunyi) |
| `flyctl secrets set KEY=VALUE` | Update satu secret |
| `flyctl scale count 0` | Stop server (sebelum run lokal) |
| `flyctl scale count 1` | Pastikan 1 instance berjalan |
| `flyctl deploy` | Re-deploy setelah ada perubahan kode atau cache |
| `flyctl open` | Buka dashboard web Fly.io |

---

## Catatan Penting

### Image Cache

`image_cache.json` di-commit ke repository dan dibundel ke Docker image saat deploy.
Setiap URL gambar artist di-cache selama 30 hari (bisa diubah di `config.py`).
Untuk refresh cache yang expired, jalankan script di lokal beberapa saat lalu re-deploy.

> ⚠️ **Scraping Last.FM dari Fly.io:** IP datacenter Fly.io kemungkinan di-rate-limit oleh Last.FM untuk scraping gambar. Karena `image_cache.json` sudah dibundel saat deploy, server tidak perlu scraping untuk artist yang sudah di-cache. Untuk artist baru yang belum ter-cache atau cache-nya sudah expired, fallback-nya adalah tidak ada gambar — widget akan menampilkan gambar default fallback-nya.

### Running Lokal vs Server

**Jangan jalankan script di lokal dan di server secara bersamaan.**
Keduanya akan meng-patch widget Discord yang sama secara simultan, menyebabkan konflik dan konsumsi rate limit dua kali lipat.

Alur yang disarankan:
1. `flyctl scale count 0` — stop server
2. Jalankan di lokal untuk testing atau build cache
3. `flyctl deploy` — re-deploy (otomatis start server dengan 1 instance)

### Auto-restart
Fly.io otomatis me-restart container jika crash. Script tidak akan mati permanen.

### Free Tier Fly.io
- 3 shared-cpu-1x VM (256MB RAM) — **gratis selamanya**
- Script ini hanya butuh ~30MB RAM dan hampir 0% CPU
- Masuk jauh di bawah batas free tier

---

## Jika Ada Error saat Deploy

```powershell
# Lihat detail error
flyctl logs --tail

# Build ulang tanpa cache
flyctl deploy --no-cache

# Reset app dan coba lagi
flyctl apps destroy widgetfm-rizukiretz
flyctl launch --no-deploy
```
