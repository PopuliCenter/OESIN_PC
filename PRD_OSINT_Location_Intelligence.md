# PRD — OSINT & Consent-Based Location Intelligence Platform

**Version:** 1.0  
**Date:** 2026-09-03  
**Status:** Draft for product/engineering review

## 1. Product Overview

Platform intelligence untuk mengumpulkan, menghubungkan, memvalidasi, dan menyajikan informasi dari sumber terbuka serta data lokasi yang diperoleh secara sah melalui persetujuan pengguna.

Produk memiliki dua domain utama:

1. **OSINT Intelligence** — phone/email/username/domain/IP dan sumber publik.
2. **Consent-Based Location Intelligence** — lokasi GPS dari aplikasi/perangkat yang memberikan izin.

> Prinsip utama: sistem tidak boleh memperoleh GPS, alamat rumah, atau data non-publik seseorang secara diam-diam.

## 2. Problem Statement

Informasi investigasi tersebar di banyak sumber. Investigator membutuhkan satu workspace untuk:
- memasukkan identifier;
- mengumpulkan data dari sumber yang diizinkan;
- menghubungkan entitas;
- menyimpan evidence dan sumber;
- melihat relasi dalam graph;
- menampilkan lokasi secara geospasial;
- menjaga audit trail, consent, dan kontrol akses.

## 3. Objectives

- Mempercepat investigasi berbasis sumber terbuka.
- Menyediakan entity resolution dengan confidence score.
- Memisahkan fakta, inferensi, dan perkiraan.
- Menyediakan GPS presisi hanya melalui mekanisme consent/otorisasi yang sah.
- Menyediakan evidence provenance dan audit trail.
- Menerapkan privacy-by-design dan least privilege.

## 4. Non-Goals

- Melacak perangkat tanpa izin.
- Mengambil GPS hanya dari nomor telepon/email/IP.
- Membobol akun/perangkat.
- Menghindari authentication, CAPTCHA, rate limit, atau kontrol akses sumber.
- Menghasilkan alamat rumah privat dari identifier tanpa dasar yang sah.
- Menyediakan covert tracking atau spyware.

## 5. Target Users

### Investigator / Researcher
Melakukan pencarian dan analisis kasus.

### Analyst
Memvalidasi hubungan antar-entitas dan evidence.

### Case Manager
Membuat case, menetapkan investigator, dan mengatur akses.

### Administrator
Mengelola user, source connector, retention, security, dan audit.

### Respondent / Mobile User
Memberikan consent dan, bila diperlukan, mengirim lokasi melalui aplikasi.

## 6. Core Use Cases

### UC-01 Search Identifier
Input: phone, email, username, domain, IP, URL, nama.

Output: normalized identifier, source findings, related entities, confidence.

### UC-02 Entity Resolution
Menggabungkan temuan yang kemungkinan merujuk ke entitas sama tanpa menganggap kecocokan sebagai kepastian.

### UC-03 Investigation Graph
Menampilkan Person, Phone, Email, Username, Domain, Organization, Location, Source dan Evidence sebagai node/edge.

### UC-04 Evidence Management
Setiap finding menyimpan source, timestamp, collection method, hash/snapshot reference bila legal, dan analyst notes.

### UC-05 Consent-Based GPS
Mobile app meminta permission lokasi, menampilkan tujuan penggunaan, status consent, dan opsi revoke. Hanya setelah izin aktif lokasi dikirim.

### UC-06 Location History
Menampilkan titik lokasi berdasarkan timestamp, accuracy, dan retention policy.

### UC-07 Geofence
Mendefinisikan area penelitian yang sah dan menghasilkan event ketika perangkat consent-based masuk/keluar area.

### UC-08 Case Access
Investigator hanya dapat melihat case dan data sesuai role/scope.

## 7. Functional Requirements

### FR-01 Identity Input
Sistem harus menerima dan menormalisasi:
- E.164 phone number;
- email;
- username;
- domain;
- IPv4/IPv6;
- URL.

### FR-02 Source Connectors
Connector harus memiliki:
- source name;
- source type;
- legal/usage classification;
- rate limit;
- authentication configuration;
- health status;
- collection timestamp.

### FR-03 Evidence Provenance
Setiap finding wajib memiliki:
- source;
- collected_at;
- source_identifier;
- evidence reference;
- confidence;
- processing status.

### FR-04 Entity Resolution
Sistem memberikan score dan alasan matching. Tidak boleh mengubah inferensi menjadi fakta.

### FR-05 Location Permission
Mobile app harus:
- menjelaskan tujuan;
- meminta OS location permission;
- mencatat consent version;
- mencatat consent timestamp;
- menyediakan revoke/stop sharing.

### FR-06 GPS Telemetry
Telemetry minimum:
- latitude;
- longitude;
- accuracy;
- timestamp;
- device/session identifier;
- consent reference.

### FR-07 Location Privacy
- Precision dapat dikurangi sesuai policy.
- Retention dapat dikonfigurasi.
- Raw GPS tidak boleh tersedia untuk semua role.
- Export lokasi memerlukan permission tambahan.

### FR-08 Search & Filtering
Filter case, entity type, source, confidence, date range, location dan evidence status.

### FR-09 Graph
Graph mendukung expand node, path exploration, relationship type, confidence dan evidence drill-down.

### FR-10 Audit
Audit event minimal:
- login;
- case access;
- search;
- data view;
- export;
- consent change;
- source collection;
- admin/security change.

## 8. Privacy & Consent Requirements

- Purpose limitation.
- Data minimization.
- Role-based access.
- Encryption in transit and at rest.
- Consent versioning.
- Revocation handling.
- Retention/deletion policy.
- Access logging.
- Sensitive-location masking.
- Separate storage/permissions untuk raw GPS.

## 9. Non-Functional Requirements

- API p95 target < 500 ms untuk operasi metadata biasa.
- Queue-based collection agar connector tidak memblokir request pengguna.
- Horizontal scaling untuk collectors.
- Idempotent ingestion.
- High availability untuk core API.
- Backup dan disaster recovery.
- Observability: logs, metrics, traces, alerts.

## 10. MVP

### Phase 1
- Authentication/RBAC.
- Case management.
- Phone/email/IP/domain normalization.
- Source connector framework.
- Evidence store.
- Entity graph.
- Basic map.
- Consent-based mobile GPS.
- Audit log.

### Phase 2
- Advanced entity resolution.
- Timeline.
- Geofence.
- Location history.
- More source connectors.
- Analyst collaboration.

### Phase 3
- Advanced analytics.
- Automated anomaly detection.
- Privacy-preserving aggregation.
- Organization-wide intelligence dashboards.

## 11. Success Metrics

- Time-to-first-useful-finding.
- Entity resolution precision.
- Evidence provenance coverage.
- False-positive rate.
- Consent compliance rate.
- Unauthorized-access incidents.
- GPS ingestion reliability.
- Analyst productivity.

## 12. Acceptance Criteria

Produk MVP dianggap siap bila:
1. Semua access ke case tercatat.
2. Semua GPS record memiliki consent reference.
3. Revoke consent menghentikan ingestion berikutnya.
4. Finding dapat ditelusuri ke source.
5. Entity match menampilkan confidence.
6. Raw GPS hanya dapat diakses role berwenang.
7. Export memiliki audit event.
8. Retention/deletion policy dapat diuji.

## 13. Risks

- False positive identity matching.
- Source terms-of-service berubah.
- Data broker/source tidak akurat.
- GPS spoofing.
- Unauthorized insider access.
- Excessive retention.
- Re-identification dari data geospasial.

## 14. Product Principle

**Collect less, prove more, disclose clearly, and never turn an inference into a fact.**
