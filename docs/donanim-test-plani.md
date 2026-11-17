# Donanım Analizi ve Pertinaks Test Planı

Kaynak: `ocpp-evse-hasan.kicad_sch` (KiCad netlist'i `kicad-cli` ile çıkarıldı).

## 1. Şematiğin özeti

| Blok | Bileşenler | Bağlantı |
|---|---|---|
| Kontrolcü | Raspberry Pi Zero 2 W (A1) | I2C: GPIO2/3 · PWM: GPIO12 (PWM0) |
| Negatif besleme | TC1044S (IC4), C13, C5, C26 | +12 V → ≈ -12 V (op-amp V-) |
| Pilot üreteci | LM358BA (IC1A), R8/R9 100k, C15 | +IN = GPIO12, -IN = 1.65 V referans → **karşılaştırıcı** |
| Pilot çıkışı | R1 1k, TVS1 P6SMB16CA | IC1A OUT → R1 → **PILOT** (Type 2 CP) |
| Pilot ölçümü | R7 200k, R6 56k, R5 47k → ADS1115 **A0** | Ofsetli bölücü: ±12 V → 0.23…2.95 V |
| Proximity | R2 330R (+3.3 V), C1 → ADS1115 **A1** | Type 2 PP |
| ADC | ADS1115 (U1), ADDR=GND | I2C **0x48** |
| NFC | PN532 (U2) | I2C **0x24** |

**Çalışma mantığı**
- GPIO12 = HIGH → op-amp çıkışı +12 V, LOW → -12 V. Yazılım GPIO12'ye **1 kHz donanım PWM'i** verir.
  - %100 = sabit +12 V (A/B: hazır, şarj izni yok)
  - %26.7 = 16 A teklifi (IEC 61851-1: duty = A / 0.6)
  - %0 = -12 V (F: kullanım dışı)
- ADS1115 PWM'i "ortalama" olarak görür (delta-sigma ADC). Yazılım, bilinen duty ve -12 V seviyesinden pozitif tepe değerini geri hesaplar (`pilot_high_from_average`).

### Yazılımda düzeltilen uyumsuzluklar
1. **Eski formül `V_cp = V_adc × 4.0303` bu devreye uymuyordu.** Devre ofsetli bölücü kullanıyor (R5 +3.3 V'a çekiyor). Eski formülle 6 V (C) "Connected", 0 V ise "Charging" olarak okunuyordu. Yeni formül `cp_voltage_from_adc` şematikteki R7/R6/R5 değerlerini kullanıyor.
2. **GPIO12'ye hiç PWM verilmiyordu.** GPIO boşta kaldığında karşılaştırıcı -12 V üretir, araç da hiçbir zaman şarja başlamaz (gerçek araç PWM görmeden C durumuna geçmez). Artık `hardware/pilot.py` dosyası bunu yönetiyor.
3. **PP (A1) okunmuyordu.** Artık kablo kapasitesi (13/20/32/63 A) okunuyor ve teklif edilen akım `min(MAX_CURRENT_A, kablo)` ile sınırlanıyor.

## 2. Şematikte dikkat edilmesi gerekenler

| # | Konu | Öneri |
|---|---|---|
| 1 | **LM358 çıkışı +12 V'a tam çıkamaz** (pozitif rail'e ~1–1.5 V kalır). A durumu ~10.5 V olur ve 10.5 V'luk eşiğin tam sınırına düşer. | Gerçek seviyeyi ölçüp eşikleri `.env`'den ayarla (bkz. Adım 4). Kalıcı çözüm: ≥30 V beslemeli, rail-to-rail çıkışlı ve daha hızlı bir op-amp. |
| 2 | **LM358 yavaştır** (slew rate ≈ 0.3–0.5 V/µs). 24 V'luk geçiş onlarca µs sürebilir. IEC 61851-1 pilot kenarları için yaklaşık 10 µs mertebesinde bir sınır koyar (güncel değeri standarttan doğrula). | Osiloskopla yükselme/düşme süresini ölç. Uzunsa daha hızlı bir op-amp kullan. |
| 3 | +12 V kaynağı şematikte yok ("power supply +12 temp"). TC1044S en fazla ~12 V ile çalışır (mutlak sınır ~13 V). | Regüleli 12 V kullan. Pi ile ortak GND'nin bağlı olduğunu kontrol et (netlist'te bağlı). |
| 4 | U3'te **L2 ve L3 birbirine kısa devre**, L1 ve N ise boşta. | Bilinçli değilse düzelt. Şebeke tarafı henüz tasarlanmamış. |
| 5 | Yanlış veya eksik footprint'ler: C1 → `SOIC-16`, R5 → `hasan-lib:ADS1115`, R2/U3 boş, U2 → `PN532` (kütüphane yok). | PCB'ye geçmeden önce düzelt. Pertinaksı etkilemez. |
| 6 | ERC'de 78 hata var, çoğu boşta kalan Pi pinleri ve PWR_FLAG eksikliğinden kaynaklanıyor. | +12 V, GND ve +3.3 V'a `PWR_FLAG` ekle, kullanılmayan pinlere "no connect" (X) koy. |
| 7 | Nextion UART'ı (GPIO14/15) şematikte görünmüyor. | Header olarak ekle. Pi Zero 2 W'da `/dev/serial0` kullan ve `dtoverlay=disable-bt` ile PL011'i GPIO14/15'e ver. |

### Gerçek bir şarj ünitesi için eksik bloklar (yol haritası)
1. **Kontaktör/röle sürücüsü:** GPIO → MOSFET + flyback diyot → 12 V bobin. Yazılımda PWM ile birlikte açılıp kapanacak.
2. **Kaçak akım koruması:** Type A RCD + 6 mA DC algılama (ör. RDC-DD modülü). Hata durumunda kontaktör açılır ve `GroundFailure` raporlanır.
3. **Kontaktör yapışma algılama:** AC algılama veya yardımcı kontak.
4. **Sıcaklık sensörü** (NTC): soket ve kontaktör yanına.
5. **Watchdog:** Kernel PWM, program çökse bile çalışmaya devam eder. `systemd` ile `Restart=always` ve donanım watchdog'u (`dtparam=watchdog=on`) önerilir.
6. **Soket kilidi** (Type 2 soketli tasarımda). Şu an `UnlockConnector` → `NotSupported` dönüyor.

> ⚠️ Kontaktör ve RCD eklenmeden karta **şebeke gerilimi bağlamayın**. Aşağıdaki testler yalnızca düşük gerilimle (12 V / 3.3 V) yapılır.

## 3. Pi hazırlığı

```bash
sudo raspi-config              # Interface Options → I2C: Enable, Serial: login shell NO, hardware YES
```

`/boot/firmware/config.txt` dosyasına ekle:
```
dtparam=i2c_arm=on
dtoverlay=pwm,pin=12,func=4
dtoverlay=disable-bt
```

```bash
pip install -r requirements-pi.txt
i2cdetect -y 1                 # 0x24 (PN532) ve 0x48 (ADS1115) görünmeli
```

## 4. Pertinaks test adımları

Araçlar: multimetre, osiloskop (tercihen), 1N4148 diyot, 2.74 kΩ, 1.3 kΩ, 270 Ω, 1.5 kΩ, 680 Ω, 220 Ω ve 100 Ω dirençler, 2 anahtar.

**Adım 0 — Enerjisiz kontroller**
- +12 V–GND, +3.3 V–GND ve -12 V–GND arasında kısa devre olmamalı.
- LM358 ve TC1044S pin sırası adaptör üzerinde doğru olmalı (pin 1 işareti).

**Adım 1 — Besleme** (önce Pi'yi, sonra 12 V'u aç)

| Ölçüm noktası | Beklenen |
|---|---|
| IC1 pin 8 | +12 V |
| TC1044S pin 5 / IC1 pin 4 | ≈ -11 … -12 V |
| IC1 pin 2 (referans) | 1.65 V |
| ADS1115 VDD | 3.3 V |

**Adım 2 — Pilot çıkışı** (`python main.py pilot-test`)

| Komut | CP pini (yüksüz) | A0 (`read`) |
|---|---|---|
| `high` | +10.5 … +12 V → **değeri not et = Vh** | 2.78 … 2.95 V |
| `low` | ≈ -11.5 … -12 V → **değeri not et = Vl** | ≈ 0.23 V |
| `pwm 16` | Osiloskop: 1 kHz, %26.7, ±12 V kare dalga. Multimetre DC ≈ -5.6 V | ≈ 0.96 V |

- Ölçtüğün negatif seviyeyi `.env`'e yaz: `CP_LOW_VOLTAGE=<Vl>`.
- `table` komutu bu devrenin beklenen ADC değerlerini listeler.
- Osiloskopta yükselme/düşme süresini de ölç (bkz. 2. bölüm, madde 2).

**Adım 3 — Araç simülatörü** (CP ile PE/GND arasına)

```
CP ──▶|── (1N4148) ──┬── 2.74k ──────────── PE
                     ├── 1.3k ── S2 ─────── PE      (S2 kapalı = C)
                     └── 270R ── S3 ─────── PE      (S3 kapalı = D)
```

`high` modunda (DC) `watch` ile beklenen değerler:

| Durum | Yük | CP (Vh=12 V) | CP (Vh=10.5 V) | A0 (Vh=12) | A0 (Vh=10.5) |
|---|---|---|---|---|---|
| A | yok | 12.00 | 10.50 | 2.950 | 2.780 |
| B | diyot + 2.74k | 8.98 | 7.88 | 2.608 | 2.484 |
| C | + 1.3k (S2) | 5.99 | 5.29 | 2.270 | 2.190 |
| D | + 270R (S3) | 2.93 | 2.63 | 1.923 | 1.889 |
| E | CP–PE kısa devre | 0 | 0 | 1.591 | 1.591 |

Ardından `pwm 16` ile tekrarla. "CP(high)" sütunu yukarıdaki değerleri vermeli ve durum harfi aynı kalmalı. **Diyotu söküp** yalnızca 2.74k ile denediğinde PWM sırasında durum `E` (diyot yok) olmalı.

**Eşik ayarı:** Ölçtüğün A/B/C/D değerlerinin orta noktalarını `.env`'e yaz. Örneğin Vh = 10.5 V ise:
```
CP_THRESHOLD_A=9.2
CP_THRESHOLD_B=6.6
CP_THRESHOLD_C=4.0
CP_THRESHOLD_D=1.3
```

**Adım 4 — PP** (PP ile PE arasına direnç takıp `read` ile oku)

| Rc | A1 beklenen | Kablo |
|---|---|---|
| yok | 3.30 V | kablo yok |
| 1.5 kΩ | 2.705 V | 13 A |
| 680 Ω | 2.222 V | 20 A |
| 220 Ω | 1.320 V | 32 A |
| 100 Ω | 0.767 V | 63 A |

**Adım 5 — NFC:** `python main.py` ile başlat. Kart okutunca logda `Kart okundu: UID=...` görünmeli.

**Adım 6 — Uçtan uca (PowerFill ile)**
1. `python main.py` (`HARDWARE_MODE=auto`). Sayaç yoksa simüle sayaç kullanılır ve uyarı loglanır.
2. Kartı okut → `Authorize` Accepted → durum `Preparing`.
3. Simülatörü B'ye al → `StartTransaction` gönderilir ve PWM başlar (osiloskopta gör) → durum `SuspendedEV`.
4. S2'yi kapat (C) → durum `Charging`, `MeterValues` gelmeye başlar.
5. Aynı kartı tekrar okut → PWM durur, `StopTransaction` (Local) gönderilir → `Finishing`.
6. Simülatörü çıkar (A) → `Available`.
7. Aynı akışı PowerFill'den RemoteStart/RemoteStop ile tekrarla.
