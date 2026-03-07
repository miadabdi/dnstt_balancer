# dnstt-balancer

[English](README.md) | فارسی

لودبالانسر چند-تونلی SOCKS5 برای [dnstt-client](https://www.bamsoftware.com/software/dnstt/).

این ابزار چندین پردازش `dnstt-client` (برای هر DNS resolver یکی) اجرا می‌کند، یک پراکسی SOCKS5 یکپارچه ارائه می‌دهد و اتصال‌ها را بین تونل‌های سالم با مسیریابی وزن‌دار بر اساس latency پخش می‌کند. همچنین مانیتورینگ سلامت، احیای resolverهای مرده، retry خودکار در خطا و داشبورد زنده ترمینال دارد.

## قابلیت‌ها

- **لودبالانس چند-تونلی** — اجرای همزمان تا N تونل `dnstt-client` و توزیع ترافیک بین آن‌ها
- **مسیریابی وزن‌دار بر اساس latency** — تونل‌های سریع‌تر به‌صورت خودکار اتصال بیشتری می‌گیرند
- **مانیتورینگ سلامت** — پروب دوره‌ای SOCKS5 CONNECT تونل‌های خراب را تشخیص می‌دهد و از pool ذخیره جایگزین می‌کند
- **احیای resolverهای مرده** — resolverهایی که قبلا fail شده‌اند به‌صورت دوره‌ای دوباره تست می‌شوند تا pool کوچک نشود
- **محافظت در برابر idle stall** — اگر یک relay بی‌فعالیت بماند، اتصال بسته می‌شود تا کلاینت بتواند دوباره از تونل سالم وصل شود
- **بازیافت تونل** — جایگزینی اختیاری تونل‌های قدیمی برای تازه نگه داشتن تونل‌های long-lived
- **retry خودکار** — اگر اتصال از یک تونل fail شود، روی تونل دیگر به‌صورت شفاف retry می‌شود
- **داشبورد زنده TUI** — رابط ترمینالی رنگی با وضعیت سلامت، latency، نرخ throughput و رویدادهای اخیر
- **چندسکویی** — قابل اجرا روی Linux، macOS و Windows
- **بدون dependency خارجی** — فقط با کتابخانه استاندارد Python 3

## پیش‌نیازها

- **Python 3.8+**
- **باینری `dnstt-client`** — باینری مناسب سیستم‌عامل شما باید در مسیر کاری باشد (یا با `--dnstt` مسیر بدهید)
- یک فایل متنی شامل IP resolverهای DNS (هر خط یک مورد)

## نصب

در حال حاضر dependency خارجی نیاز نیست. اگر در آینده نیاز شد، اسکریپت نصب موجود است:

```bash
# Linux / macOS
./install_deps.sh

# Windows
install_deps.bat
```

## اجرا

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <SERVER_PUBLIC_KEY> \
    --domain <YOUR_DOMAIN>
```

سپس مرورگر، تلگرام یا هر برنامه SOCKS5-aware را روی **`127.0.0.1:8081`** تنظیم کنید.

### فرمت فایل DNS List

فایل متنی ساده، هر خط یک resolver. خط خالی و کامنت (`#`) نادیده گرفته می‌شود:

```text
# Google
8.8.8.8
8.8.4.4

# Cloudflare
1.1.1.1
1.0.0.1
```

## گزینه‌های خط فرمان

| Flag | مقدار پیش‌فرض | توضیح |
| --- | --- | --- |
| `--dnstt PATH` | پیش‌فرض خودکار بر اساس پلتفرم | مسیر باینری `dnstt-client` (تشخیص خودکار بر اساس سیستم‌عامل) |
| `--dns-list FILE` | _(اجباری)_ | فایل متنی resolverهای DNS |
| `--pubkey KEY` | _(اجباری)_ | کلید عمومی سرور dnstt |
| `--domain DOMAIN` | _(اجباری)_ | دامنه dnstt |
| `--dns-port PORT` | `53` | پورت DNS |
| `--protocol {udp,dot,doh}` | `udp` | پروتکل انتقال DNS |
| `--utls FINGERPRINT` | _(ندارد)_ | fingerprint برای uTLS (مثلا `Chrome_120`) |
| `--listen HOST:PORT` | `127.0.0.1:8081` | آدرس پراکسی SOCKS5 برای listen |
| `--max-tunnels N` | `15` | حداکثر تعداد تونل همزمان |
| `--startup-wait SECS` | `6.0` | زمان انتظار برای بالا آمدن هر تونل |
| `--health-interval SECS` | `30.0` | فاصله زمانی health check |
| `--health-timeout SECS` | `15.0` | حداکثر زمان هر health probe قبل از timeout |
| `--revive-interval SECS` | `600.0` | فاصله زمانی برای retry resolverهای مرده |
| `--tui-interval SECS` / `--stats-interval SECS` | `2.0` | فاصله refresh داشبورد |
| `--idle-timeout SECS` | `120.0` | timeout بی‌فعالیتی هر relay |
| `--recycle-age SECS` | `0` | بازیافت تونل‌های قدیمی‌تر از این مقدار (`0` یعنی غیرفعال) |
| `--no-dashboard` | _(غیرفعال)_ | غیرفعال‌کردن داشبورد زنده (لاگ روی stderr) |
| `--log-file PATH` | _(ندارد)_ | ذخیره لاگ در فایل (کنار داشبورد توصیه می‌شود) |

## مثال‌ها

اجرای پایه با UDP:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <pub key> \
    --domain <domain>
```

DoH با uTLS، حداکثر 10 تونل، listen سفارشی:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <KEY> \
    --domain t.example.com \
    --protocol doh \
    --utls Chrome_120 \
    --max-tunnels 10 \
    --listen 127.0.0.1:1080
```

حالت headless همراه با لاگ:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <KEY> \
    --domain t.example.com \
    --no-dashboard \
    --log-file balancer.log
```

فعال‌کردن بازیافت تونل و idle-timeout سخت‌گیرانه‌تر:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <KEY> \
    --domain t.example.com \
    --recycle-age 3600 \
    --idle-timeout 60
```

## نحوه کار

1. **Startup** — فایل DNS خوانده و shuffle می‌شود، سپس تا `--max-tunnels` پردازش `dnstt-client` به‌صورت موازی اجرا می‌شود. برای هر تونل، یک پورت local SOCKS5 موقت توسط سیستم‌عامل انتخاب می‌شود.
2. **Proxy** — سرور روی آدرس `--listen` منتظر اتصال می‌ماند. هر اتصال به بهترین تونل موجود (بر اساس latency و بار فعلی) هدایت می‌شود. در صورت fail شدن اتصال upstream، تا 2 بار روی تونل دیگر retry می‌شود.
3. **Health checks** — هر `--health-interval` ثانیه، برای هر تونل یک SOCKS5 CONNECT به `www.google.com:443` انجام می‌شود (با محدودیت `--health-timeout`). تونلی که 3 بار پیاپی fail شود unhealthy علامت‌گذاری و جایگزین می‌شود.
4. **احیای resolverهای مرده** — هر `--revive-interval` ثانیه، resolverهای مرده دوباره به reserve pool برگردانده می‌شوند تا دوباره تست شوند.
5. **مدیریت stall در relay** — فعالیت داده در هر دو جهت پایش می‌شود. اگر تا `--idle-timeout` داده‌ای رد و بدل نشود، relay بسته می‌شود.
6. **بازیافت اختیاری تونل‌ها** — اگر `--recycle-age` بزرگ‌تر از صفر باشد، تونل‌های قدیمی و بدون اتصال فعال جایگزین می‌شوند.
7. **خاموش شدن** — با Ctrl+C خاموش‌سازی graceful شروع می‌شود: پذیرش اتصال جدید متوقف می‌شود، زمان کوتاهی برای drain شدن اتصال‌های فعال داده می‌شود، سپس همه پردازش‌های `dnstt-client` متوقف می‌شوند و آمار نهایی نمایش داده می‌شود. با Ctrl+C دوم، برنامه force-quit می‌شود.

## ساختار پروژه

```text
dnstt-balancer/
├── dnstt-balancer.py      # اسکریپت اصلی (تک‌فایل، بدون dependency)
├── dns.txt                # لیست DNS resolverها
├── requirements.txt       # خالی - فقط stdlib
├── install_deps.sh        # نصب dependency برای Linux/macOS
├── install_deps.bat       # نصب dependency برای Windows
├── README.md              # مستندات انگلیسی
└── README.fa.md           # مستندات فارسی
```

## لایسنس

MIT
