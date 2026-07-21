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
fly version
```

---

## Langkah 2 — Login ke Fly.io

```powershell
fly auth login
```

Browser akan terbuka untuk login/signup.

---

## Langkah 3 — Inisialisasi App (jalankan dari folder widgetfm)

```powershell
cd e:\Bot\widgetfm
fly launch --no-deploy
```

Fly.io akan bertanya:
- **App name**: masukkan nama unik, misal `widgetfm-rizukiretz`
- **Region**: pilih `sin` (Singapore) — terdekat ke Indonesia
- **Would you like to set up a PostgreSQL database?**: **No**
- **Would you like to set up an Upstash Redis database?**: **No**

Ini akan membuat file `fly.toml` secara otomatis.

---

## Langkah 4 — Set Secrets (API Keys & Tokens)

**JANGAN taruh .env di server** — gunakan Fly.io secrets sebagai gantinya:

```powershell
fly secrets set `
  LAST_FM_USERNAME="(username Last.FM kamu)" `
  API_KEY="(Last.FM API key kamu)" `
  USER_ID="(Discord user ID kamu)" `
  STATSFM_USERNAME="(username stats.fm kamu)" `
  APPLICATION_ID="(Application ID widget Listening Stats)" `
  BOT_TOKEN="(Bot Token widget Listening Stats)" `
  TOPARTISTS_APPLICATION_ID="(Application ID widget Top Artists)" `
  TOPARTISTS_BOT_TOKEN="(Bot Token widget Top Artists)"
```

> Lihat file `.env.example` untuk deskripsi lengkap setiap variabel.

Secrets disimpan terenkripsi di Fly.io dan tidak akan pernah terlihat setelah di-set.

---

## Langkah 5 — Deploy

```powershell
fly deploy
```

Fly.io akan:
1. Build Docker image dari `Dockerfile`
2. Push ke registry Fly.io
3. Deploy ke server di Singapore
4. Script langsung berjalan otomatis

---

## Monitoring — Cek Log Real-Time

```powershell
fly logs
```

Output yang normal:
```
[TA] Thread dimulai
[LS] Thread dimulai
[Cache] Loaded: 0 artist dari image_cache.json
[TA] Artist baru terdeteksi, load pool untuk 5 artist...
[TA] Fetch Last.FM untuk 'Holly Humberstone'...
[TA] Pool 'Holly Humberstone': 40 gambar (Last.FM, cache disimpan)
...
[LS] Status: Now Playing | GIRLI — Pedestal
[LS] Discord → 204 | Rate: 2/3 remaining, resets in 20.0s
```

---

## Perintah Berguna

| Perintah | Fungsi |
|---|---|
| `fly logs` | Lihat log real-time |
| `fly status` | Cek status app (running/stopped) |
| `fly restart` | Restart script |
| `fly secrets list` | Lihat nama secrets (nilai tersembunyi) |
| `fly secrets set KEY=VALUE` | Update satu secret |
| `fly scale count 1` | Pastikan 1 instance berjalan |
| `fly dashboard` | Buka dashboard web |

---

## Catatan Penting

### Image Cache
Di server, `image_cache.json` dibuat ulang saat startup (karena tidak ada persistent storage).  
Ini hanya membutuhkan ~10-15 detik ekstra di awal — **tidak masalah**.

Jika ingin cache persisten (opsional):
```powershell
fly volumes create widgetfm_data --size 1 --region sin
```
Lalu update `fly.toml` dan `IMAGE_CACHE_FILE` di `upstats.py`. Ini opsional.

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
fly logs --tail

# Build ulang tanpa cache
fly deploy --no-cache

# Reset app dan coba lagi
fly apps destroy widgetfm-rizukiretz
fly launch --no-deploy
```
