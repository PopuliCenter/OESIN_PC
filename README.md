# OESIN_PC — OSINT & Consent-Based Location Intelligence (MVP)

Implementasi runnable dari PRD/SDD di repo ini: FastAPI + dashboard statis (tanpa
build tool), PostgreSQL + PostGIS, enrichment nomor **offline**, lokasi GPS **hanya
berbasis consent**. Semua jalan lewat Docker Compose.

## Prinsip (PRD §4, §14)

- **Tidak** mengambil GPS/alamat dari nomor HP. Enrichment = metadata publik/offline saja.
- Lokasi hanya masuk lewat **agen consent** yang dijalankan subjek dengan izin eksplisit.
- Setiap titik lokasi merujuk **consent aktif**; revoke menghentikan ingestion.
- Akses raw GPS dibatasi **role (RBAC)**; semua aksi tercatat di **audit trail** append-only.

## Fitur

### Phase 1
| Kemampuan | Endpoint |
|---|---|
| Normalisasi identifier (phone/email/domain/ip/url/username) | `POST /v1/lookup` |
| Enrichment nomor offline (carrier, region, tipe, timezone) | `POST /v1/lookup` |
| **IP geolocation** online (negara/kota/ISP/ASN/koordinat perkiraan, via ip-api.com) | `POST /v1/lookup` (input IP) |
| **Username lookup** online (cek profil publik di GitHub/GitLab/Reddit/dll) | `POST /v1/lookup` (input username) |
| Entity resolution minimal (exact-match + confidence) | internal |
| Consent grant / revoke | `POST /v1/consents`, `/{id}/revoke` |
| Sesi lokasi + ingest titik (consent-gated) | `POST /v1/location/sessions`, `/points` |
| Peta lokasi consent (Leaflet) | dashboard |
| Audit trail | `GET /v1/audit` |

### Phase 2
| Kemampuan | Endpoint |
|---|---|
| **Auth + JWT** | `POST /v1/auth/login`, `GET /v1/auth/me` |
| **RBAC** (5 role) | proteksi di semua endpoint |
| **User management** | `GET/POST /v1/users` (admin) |
| **Geofence** circle/polygon (UI gambar di peta) | `POST/GET/DELETE /v1/geofences` |
| **Enter/exit events** otomatis (PostGIS `ST_Contains`/`ST_DWithin`) | `GET /v1/geofence/events` |
| **Entity relationship graph** (vis-network) | `GET /v1/graph` |
| **Tautkan nomor HP ke consent** (opsional; resolve ke entity OSINT yang sama) | field `phone` di `POST /v1/consents` → edge `Consent-HAS_PHONE->entity` |

### Analitik spasial (PostGIS)
| Kemampuan | Endpoint |
|---|---|
| Ringkasan pergerakan (jarak tempuh, bbox, rentang waktu, akurasi) | `GET /v1/analytics/movement` |
| Activity area = convex hull + luas | `GET /v1/analytics/activity-area` |
| Titik di dalam geofence (+ rentang waktu) | `GET /v1/analytics/in-geofence` |
| Dwell time per geofence (dari pasangan enter/exit) | `GET /v1/analytics/dwell` |
| **Heatmap density** (grid `ST_SnapToGrid` + count per sel) | `GET /v1/analytics/heatmap?cell_m=` — toggle di tab Map (leaflet.heat) |

### Timeline / playback
| Kemampuan | Mekanisme |
|---|---|
| Track kronologis per-consent | `GET /v1/location/points?order=asc` |
| Playback peta: marker bergerak + trail tumbuh | tab **Timeline** (play/pause, scrubber, kecepatan 1–10×) |
| Precision tier & konteks geofence saat playback | selector precision + overlay geofence |

### Export laporan per-case (consent)
| Kemampuan | Endpoint | Role |
|---|---|---|
| Laporan PDF (subjek, movement, dwell, audit) | `GET /v1/reports/consent/{id}/report.pdf` | location_analyst, case_manager |
| CSV titik lokasi | `GET /v1/reports/consent/{id}/points.csv` | location_analyst |
| CSV geofence events | `GET /v1/reports/consent/{id}/events.csv` | location_analyst, case_manager |
| CSV findings OSINT | `GET /v1/reports/findings.csv` | investigator, case_manager |

PDF dibuat dengan `fpdf2` (pure-Python). Tombol export ada di tab **Analytics**
(PDF / CSV titik / CSV events) dan tab **Lookup** (findings CSV). Download via
fetch ber-token (`downloadFile`), bukan link biasa. Export laporan masih per-consent;
consent & findings kini bisa dikelompokkan ke **case** (lihat di bawah).

### Case management (PRD Phase 1, UC-08)
| Kemampuan | Endpoint | Role |
|---|---|---|
| Daftar case (case_manager/admin: semua; lainnya: hanya case yang diikuti) | `GET /v1/cases` | investigator, case_manager, location_analyst |
| Buat case (pembuat otomatis jadi anggota) | `POST /v1/cases` | case_manager |
| Detail case: anggota, consent, findings — **setiap akses tercatat `CASE_ACCESS`** | `GET /v1/cases/{id}` | anggota / case_manager |
| Tutup / buka kembali case (case tertutup menolak data baru, 409) | `POST /v1/cases/{id}/close`, `/reopen` | case_manager |
| Tambah / keluarkan anggota | `POST /v1/cases/{id}/members`, `DELETE /v1/cases/{id}/members/{user_id}` | case_manager |
| Tautkan consent yang sudah ada ke case | `POST /v1/cases/{id}/consents/{consent_id}` | investigator (anggota), case_manager |
| Simpan finding / consent baru langsung ke case | field `case_id` di `POST /v1/lookup` & `POST /v1/consents` | investigator (anggota), case_manager |

Bukan anggota → **404** (bukan 403) agar id case tidak bisa ditebak. Tab **📁 Cases**
di dashboard; selector case juga ada di tab Lookup, Consent, dan Map & Geofence.

### ABAC per-case (berlaku di semua endpoint)

Role (RBAC) menentukan **aksi** apa yang boleh; keanggotaan case menentukan **data
mana**. Semua aturan ada di satu modul: [backend/access.py](backend/access.py).

| Siapa | Data yang terlihat |
|---|---|
| admin, case_manager | semua case + data **tanpa case** |
| investigator, location_analyst | **hanya** data di case yang mereka ikuti; data tanpa case tidak terlihat |
| auditor | audit trail + total hitungan organisasi di `/v1/summary` |

- **Wajib pilih case**: investigator/analyst harus mengisi `case_id` (case yang mereka
  ikuti) saat lookup dan membuat consent → tanpa itu **422**. Manager boleh tanpa case.
- Titik lokasi, sesi, dan geofence event tidak punya `case_id` sendiri — **mewarisi
  case dari consent-nya**. Berlaku untuk: daftar titik, analytics (movement, area,
  heatmap, dwell, in-geofence), geofence events, export CSV/PDF, entity graph, summary.
- **Geofence per-case**: geofence milik satu case hanya terlihat oleh anggota case itu
  dan **hanya dievaluasi untuk consent di case itu**. Geofence tanpa case = **area
  bersama**: terlihat semua, dievaluasi untuk semua consent, tapi hanya bisa
  dibuat/dihapus case_manager/admin (analyst → 403).
- Entity/identifier dipakai bersama lintas case (entity resolution exact-match);
  sebuah entity terlihat bila ada finding/consent yang terlihat menunjuk ke entity itu.
- Menautkan consent ke case (`POST /v1/cases/{id}/consents/{consent_id}`) mensyaratkan
  consent itu sudah terlihat oleh pemanggil — non-manager tidak bisa "menarik" consent
  case lain atau consent tanpa case ke case-nya.
- Keanggotaan dibaca ulang tiap request: anggota yang dikeluarkan langsung kehilangan akses.

> **Data lama (sebelum fitur ini) tidak punya case** → hanya terlihat oleh
> case_manager/admin. Agar investigator/analyst bisa melihatnya, manager menautkan
> consent tersebut ke case (tab **📁 Cases** → *Tautkan consent*). Geofence lama
> otomatis menjadi **area bersama**.

### Phase 3 (privacy controls, SDD §11)
| Kemampuan | Endpoint / mekanisme |
|---|---|
| **Precision tiers**: exact / reduced (~110m) / area (~1.1km) | param `precision` di `GET /v1/location/points` |
| Role cap presisi (mis. case_manager maksimal *reduced*) | `resolve_precision()` — FR-07 |
| **Retention policy** per-consent (`retention_days`) | field saat `POST /v1/consents` |
| Preview & purge raw GPS kedaluwarsa | `GET /v1/retention/preview`, `POST /v1/retention/run` (admin) |

## Role & hak akses (RBAC — admin selalu boleh)

| Aksi | Role yang diizinkan |
|---|---|
| Lookup / consent write | investigator, case_manager |
| Findings / graph view | investigator, case_manager, location_analyst |
| GPS points (presisi di-cap per role) | location_analyst (exact), case_manager (≤reduced) |
| Geofence create / delete | location_analyst (geofence case-nya), case_manager (termasuk area bersama) |
| Analytics (movement / area / dwell) | location_analyst, case_manager |
| Analytics in-geofence (raw points) | location_analyst |
| Geofence events | location_analyst, case_manager |
| Retention preview / purge | admin |
| Audit | auditor |
| Users | admin |
| Case create / close / anggota | case_manager |
| Case view / tambah data | anggota case (investigator, location_analyst) + case_manager |

Semua baris di atas **juga** dibatasi ABAC per-case (lihat bagian *ABAC per-case*).
| Consent agent (session + point) | **publik**, gated oleh consent aktif (subjek, bukan operator) |

## Akun demo

`admin/admin123` · `investigator/invest123` · `analyst/analyst123` ·
`manager/manager123` · `auditor/audit123`

Akun ini **dev-only**. Dashboard hanya terbuka di `127.0.0.1` dan tidak bisa
dijangkau lewat link publik. Meski begitu, ganti password-nya (tab Users) sebelum
dipakai dengan data sungguhan.

## Menjalankan (Docker)

Stack = PostgreSQL + **PostGIS** (`db`) + FastAPI (`app`) + Cloudflare quick tunnel (`tunnel`).

```bash
git clone https://github.com/PopuliCenter/OESIN_PC.git && cd OESIN_PC
cp .env.example .env          # lalu isi JWT_SECRET dengan nilai acak:
python -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d --build
```

- Dashboard: <http://127.0.0.1:8000/> (hanya dari PC ini; login dulu)
- API docs (Swagger): <http://127.0.0.1:8000/docs>
- Stop: `docker compose down` · reset total (hapus data): `docker compose down -v`
- DB tidak di-expose ke host. Akses psql: `docker exec -it osint_db psql -U osint -d osint`

> Image DB memakai mirror `ghcr.io/baosystems/postgis:16-3.4` (identik dengan
> `postgis/postgis:16-3.4`) karena CDN Docker Hub sempat memutus unduhan layer besar.

## Test otomatis

```bash
docker compose run --rm app pytest
```

Test berjalan terhadap PostgreSQL + PostGIS sungguhan (service `db`), karena ABAC
dan geofencing ditulis dalam SQL/PostGIS. Setiap run membuat database sementara
`osint_test_*` lalu menghapusnya; database `osint` tidak tersentuh.

| File | Yang diuji |
| --- | --- |
| `tests/test_rbac.py` | Gerbang peran: siapa boleh memanggil endpoint apa (401/403) |
| `tests/test_abac.py` | Isolasi antar case: consent, temuan, titik, analitik, export, graph, summary; aturan pembuatan data; case tertutup; pencabutan anggota; scoping evaluasi geofence |
| `tests/test_public_surface.py` | Guard tunnel, token tulis per sesi, consent yang di-revoke, nilai titik palsu |

## Uji GPS dari HP sungguhan (tunnel HTTPS)

Browser HP hanya memberi GPS ke halaman **HTTPS**. Service `tunnel` (cloudflared
quick tunnel, tanpa akun) membuat URL publik `https://<acak>.trycloudflare.com`.

1. Tab **Consent** → buat consent (opsional: isi nomor HP subjek) → klik **📱 QR**
2. Scan QR dengan HP subjek → **Setuju & Mulai** → izinkan lokasi
3. Biarkan halaman terbuka (layar dijaga menyala via Wake Lock) → titik muncul di **Map / Timeline**
4. **Berhenti** menutup sesi di server; **Revoke** di dashboard langsung menolak kiriman berikutnya

URL quick tunnel **berganti setiap container `tunnel` restart**. Dashboard selalu
menampilkan URL yang aktif, jadi bagikan ulang QR yang baru. Untuk URL tetap, pakai
*named tunnel* Cloudflare (perlu akun + domain).

### Model keamanan link publik

- Lewat tunnel, **hanya** consent agent yang bisa diakses: `GET /consent`,
  `GET /static/style.css`, `GET /v1/public/consent/{id}`, `POST /v1/location/sessions`,
  `POST /v1/location/sessions/{id}/close`, `POST /v1/location/points`.
  Dashboard, login, dan semua API operator → **403** (`tunnel_guard` di `backend/app.py`).
- Setiap sesi punya **token tulis** acak (hanya dipegang perangkat yang membukanya);
  memegang link consent saja tidak cukup untuk menulis ke sesi orang lain.
- Validasi input ketat: koordinat/akurasi harus angka hingga, `captured_at` dibatasi
  24 jam ke belakang s.d. 5 menit ke depan, body publik maks 16 KB, rate limit 120 req/menit per IP (IPv6 per /64).
- Tanpa CORS dan dengan allowlist `Host` (anti DNS-rebinding), situs lain tidak bisa
  membaca API lokal lewat browser operator.
- Secret JWT dibaca dari `.env` (tidak di-commit).

## Alur uji cepat

1. Login `investigator` → tab **Lookup** → `0812xxxxxxx` (region ID) → carrier/region/tipe.
2. Tab **Consent** → buat consent → **📱 QR** → scan di HP → **Setuju & Mulai** → izinkan lokasi.
3. Login `analyst` → tab **Map & Geofence** → isi nama+radius → **klik peta** untuk buat geofence circle.
4. Saat titik consent masuk/keluar area → muncul **enter/exit event** + tercatat di audit.
5. Tab **Entity Graph** → lihat relasi entity–identifier–consent–session–geofence.
6. Login `auditor` → tab **Audit** → semua aksi tercatat.

## Geospasial (PostGIS — sudah aktif)

Data layer sudah PostgreSQL + PostGIS ([backend/database.py](backend/database.py)).
Geofence dievaluasi lewat query spasial di [backend/geo.py](backend/geo.py):
`ST_DWithin` (circle), `ST_Contains` (polygon), dengan GIST index pada `geom` &
`polygon_geom`. `data/postgis_migration.sql` disimpan sebagai referensi skema.

Geofence circle dan polygon bisa digambar langsung di peta (tab Map & Geofence),
atau lewat API (`POST /v1/geofences`, kind=polygon dengan array `[[lat,lng],...]`).

## Catatan produksi (belum di MVP)

Auth OIDC + MFA, graph DB
(Neo4j), queue/collectors, hardening keamanan (rate limit, secrets manager),
observability. Enrichment online (reputasi/breach) = stub opt-in di `enrichment.py`.

## OSINT online (IP & username) — catatan

`backend/osint_online.py` menambah dua sumber **online** (public record):
- **IP geolocation** (ip-api.com): lokasi **jaringan/ISP** yang **perkiraan** (level kota),
  bukan lokasi orang, bukan GPS live.
- **Username scan**: hanya cek apakah URL profil publik **ada** (200/404). Situs
  dengan login/JS wall ditandai "cek manual".

Berbeda dari tool seperti Seeker, **tidak ada** jalur phishing/pencurian GPS di sini.
Keduanya butuh koneksi internet dari container app (offline enrichment nomor tetap lokal).

## Legal & etika

Gunakan hanya pada nomor/dataset milik sendiri atau yang Anda berwenang, dan
lokasi hanya dari subjek yang memberi consent. Sistem ini sengaja dirancang
agar tidak bisa dipakai untuk covert tracking.
