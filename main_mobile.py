"""
MobileSignal — ربات خودکار جمع‌آوری و انتشار اخبار موبایل در تلگرام
----------------------------------------------------------------------------
نسخه‌ی سوم از خانواده‌ی TechSignal، مخصوص اخبار گوشی‌های موبایل —
طراحی‌شده برای فروشندگان موبایل: گوشی‌های جدید، مشخصات فنی، اخبار بازار.
- اخبار را از فیدهای RSS منابع معتبر موبایل می‌خواند
- خبرهای تکراری را با استفاده از یک فایل JSON جداگانه فیلتر می‌کند
- عنوان و خلاصه‌ی کامل هر خبر را به فارسی ترجمه می‌کند
- پیام را بدون لینک منبع (فقط اسم منبع) در کانال تلگرام منتشر می‌کند

متغیرهای محیطی مورد نیاز (از GitHub Secrets خوانده می‌شوند):
    CHANEL_MOBILE_SENDER -> توکن ربات تلگرام مخصوص موبایل
    MOBILE_CHANNEL_ID    -> آیدی یا یوزرنیم کانال موبایل
    GEMINI_API_KEY       -> (مشترک با بقیه ربات‌ها) برای ترجمه‌ی روان‌تر
"""

import os
import re
import json
import time
import calendar
import hashlib
import logging
import requests
import feedparser
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mobilesignal")

# ---------------------------------------------------------------------------
# تنظیمات قابل شخصی‌سازی
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("CHANEL_MOBILE_SENDER")
CHANNEL_ID = os.environ.get("MOBILE_CHANNEL_ID")

# کلید Gemini اختیاری است: اگر تنظیم شود، برای ترجمه‌ی روان‌تر استفاده می‌شود.
# اگر خالی باشد یا خطا بدهد، به‌صورت خودکار از گوگل ترنسلیت (deep-translator) استفاده می‌شود.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# فیدهای RSS منابع خبری معتبر موبایل
RSS_FEEDS = {
    "GSMArena": "https://www.gsmarena.com/rss-news-reviews.php3",
    "Android Authority": "https://www.androidauthority.com/feed/",
    "9to5Google": "https://9to5google.com/feed/",
    "MacRumors": "https://feeds.macrumors.com/MacRumors-All",
    "PhoneArena": "https://www.phonearena.com/feed",
    "زومیت": "https://www.zoomit.ir/feed/",
}

SENT_FILE = "sent_news_mobile.json"

# منابعی که از اول فارسی هستند و نباید دوباره ترجمه شوند
PERSIAN_SOURCES = {"زومیت"}

# حداکثر تعداد خبر جدیدی که در هر اجرا از هر منبع بررسی/ارسال می‌شود
# (سقفی جدا برای هر منبع، تا یک منبع به‌تنهایی سهمیه‌ی کل را اشغال نکند)
MAX_ITEMS_PER_FEED = 2

# حداکثر تعداد کل خبرهایی که در یک اجرا (هر ۳۰ دقیقه) از مجموع همه‌ی منابع ارسال می‌شود
# تا کانال در یک اجرا با تعداد زیادی پیام شلوغ نشود
MAX_ITEMS_TOTAL_PER_RUN = 5

# حداکثر عمر مجاز خبر (بر حسب روز). خبرهای قدیمی‌تر از این، حتی اگر تکراری هم نباشند،
# ارسال نمی‌شوند (چون بایگانی محسوب می‌شوند، نه خبر تازه).
MAX_AGE_DAYS = 7
MAX_AGE_SECONDS = MAX_AGE_DAYS * 24 * 60 * 60

# فاصله‌ی بین ارسال پیام‌ها برای رعایت محدودیت نرخ ارسال تلگرام
SEND_DELAY_SECONDS = 2

# فاصله‌ی بین درخواست‌های Gemini، تا سهمیه‌ی لحظه‌ای (Rate Limit) آن پر نشود
# (چون این کلید بین چند ربات مشترک است، این فاصله شانس موفقیت Gemini را بالا می‌برد)
GEMINI_CALL_DELAY_SECONDS = 2

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_PHOTO_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
TELEGRAM_VIDEO_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"

# محدودیت طول کپشن تلگرام برای پیام‌های همراه با عکس
TELEGRAM_CAPTION_LIMIT = 1024


# ---------------------------------------------------------------------------
# مدیریت وضعیت (جلوگیری از ارسال تکراری)
# ---------------------------------------------------------------------------

def load_sent_links() -> set:
    """خواندن لیست خبرهای قبلاً ارسال‌شده از فایل JSON."""
    if not os.path.exists(SENT_FILE):
        return set()
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("sent_links", []))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("فایل sent_news.json قابل خواندن نبود، از ابتدا شروع می‌شود: %s", e)
        return set()


def save_sent_links(sent_links: set) -> None:
    """ذخیره‌ی لیست به‌روزشده‌ی خبرهای ارسال‌شده.
    تعداد رکوردها را محدود می‌کنیم تا فایل بی‌نهایت بزرگ نشود."""
    trimmed = list(sent_links)[-2000:]
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent_links": trimmed}, f, ensure_ascii=False, indent=2)


def make_id(link: str) -> str:
    """ساخت یک شناسه‌ی یکتا و کوتاه از لینک خبر."""
    return hashlib.sha256(link.encode("utf-8")).hexdigest()


def is_within_max_age(entry) -> bool:
    """بررسی می‌کند که خبر بیشتر از MAX_AGE_DAYS روز قدیمی نباشد.
    اگر فید اصلاً تاریخ انتشار نداشته باشد (به‌ندرت پیش می‌آید)،
    برای جلوگیری از حذف اشتباهی اخبار، اجازه‌ی عبور داده می‌شود."""
    published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published_struct:
        return True
    try:
        published_epoch = calendar.timegm(published_struct)
    except (TypeError, ValueError, OverflowError):
        return True
    age_seconds = time.time() - published_epoch
    return age_seconds <= MAX_AGE_SECONDS


# ---------------------------------------------------------------------------
# ترجمه و قالب‌بندی متن
# ---------------------------------------------------------------------------

def translate_with_gemini(text: str) -> str | None:
    """ترجمه‌ی روان به فارسی با استفاده از Gemini (رایگان، نیازمند GEMINI_API_KEY).
    در صورت نبود کلید یا بروز خطا، None برمی‌گرداند تا fallback فعال شود."""
    if not GEMINI_API_KEY:
        return None

    prompt = (
        "متن خبری تکنولوژی زیر را به فارسیِ روان، طبیعی و روزنامه‌نگارانه ترجمه کن. "
        "لحن باید مثل یک خبرگزاری فارسی‌زبان باشد، نه ترجمه‌ی تحت‌اللفظی. "
        "فقط و فقط متن ترجمه‌شده را برگردان، بدون هیچ توضیح، مقدمه یا علامت نقل‌قول اضافه:\n\n"
        f"{text}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = requests.post(
            f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
            json=payload,
            timeout=25,
        )
        if resp.status_code != 200:
            log.warning("Gemini خطا داد (%s): %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None
        translated = parts[0].get("text", "").strip()
        return translated or None
    except Exception as e:
        log.warning("خطای ارتباط با Gemini: %s", e)
        return None


def translate_with_mymemory(text: str) -> str | None:
    """ترجمه‌ی رایگان و بدون کلید با MyMemory API؛ معمولاً روی سرورهای ابری
    (مثل GitHub Actions) بهتر از گوگل ترنسلیت کار می‌کند و مسدود نمی‌شود."""
    try:
        text_trimmed = text[:490]  # محدودیت این سرویس حدود ۵۰۰ کاراکتر است
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text_trimmed, "langpair": "en|fa"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText", "").strip()
        if translated and translated.lower() != text_trimmed.lower():
            return translated
        return None
    except Exception as e:
        log.warning("خطای ارتباط با MyMemory: %s", e)
        return None


def translate_to_fa(text: str) -> str:
    """ترجمه‌ی متن به فارسی، با زنجیره‌ای از سرویس‌های رایگان برای اطمینان بیشتر:
    ۱) Gemini (روان‌ترین، نیازمند GEMINI_API_KEY)
    ۲) MyMemory (رایگان، بدون کلید، مطمئن روی سرورهای ابری)
    ۳) گوگل ترنسلیت (deep-translator)
    ۴) در صورت شکست همه، متن اصلی برگردانده می‌شود."""
    if not text:
        return ""

    gemini_result = translate_with_gemini(text)
    if GEMINI_API_KEY:
        time.sleep(GEMINI_CALL_DELAY_SECONDS)  # فاصله برای رعایت سهمیه‌ی لحظه‌ای Gemini
    if gemini_result:
        return gemini_result
    log.info("Gemini جواب نداد، تلاش با MyMemory...")

    mymemory_result = translate_with_mymemory(text)
    if mymemory_result:
        return mymemory_result
    log.info("MyMemory هم جواب نداد، تلاش با گوگل‌ترنسلیت...")

    try:
        text_trimmed = text[:1000]
        return GoogleTranslator(source="auto", target="fa").translate(text_trimmed)
    except Exception as e:
        log.warning("گوگل‌ترنسلیت هم شکست خورد، متن اصلی (انگلیسی) فرستاده می‌شود: %s", e)
        return text


def clean_summary(raw_summary: str, max_len: int = 220) -> str:
    """حذف تگ‌های HTML احتمالی از خلاصه‌ی RSS و کوتاه کردن آن."""
    text = re.sub(r"<[^>]+>", "", raw_summary or "")
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def extract_image_url(entry) -> str | None:
    """تلاش برای استخراج لینک تصویر خبر از فرمت‌های مختلف RSS.
    ترتیب بررسی: media:content -> media:thumbnail -> enclosure -> تگ img داخل خلاصه/محتوا."""
    try:
        media_content = entry.get("media_content")
        if media_content:
            for m in media_content:
                url = m.get("url")
                if url:
                    return url

        media_thumbnail = entry.get("media_thumbnail")
        if media_thumbnail:
            for m in media_thumbnail:
                url = m.get("url")
                if url:
                    return url

        for enc in entry.get("links", []):
            if enc.get("rel") == "enclosure" and str(enc.get("type", "")).startswith("image"):
                url = enc.get("href")
                if url:
                    return url

        html_blob = entry.get("summary", "") or ""
        if not html_blob:
            content_list = entry.get("content")
            if content_list:
                html_blob = content_list[0].get("value", "")
        match = re.search(r'<img[^>]+src="([^"]+)"', html_blob)
        if match:
            return match.group(1)
    except Exception as e:
        log.debug("استخراج عکس ناموفق بود: %s", e)
    return None


def extract_video_url(entry) -> str | None:
    """تلاش برای استخراج لینک فایل ویدیوی مستقیم (mp4 و مشابه) از خبر، در صورت وجود.
    توجه: اکثر فیدهای خبری فقط عکس دارند نه ویدیو؛ این تابع فقط زمانی نتیجه می‌دهد
    که منبع واقعاً یک فایل ویدیوی قابل‌پخش مستقیم (نه لینک یوتیوب یا صفحه) ارائه داده باشد."""
    try:
        media_content = entry.get("media_content")
        if media_content:
            for m in media_content:
                media_type = str(m.get("type", "") or m.get("medium", ""))
                url = m.get("url")
                if url and ("video" in media_type.lower() or url.lower().endswith((".mp4", ".mov", ".webm"))):
                    return url

        for enc in entry.get("links", []):
            enc_type = str(enc.get("type", ""))
            href = enc.get("href")
            if href and enc.get("rel") == "enclosure" and (
                enc_type.startswith("video") or href.lower().endswith((".mp4", ".mov", ".webm"))
            ):
                return href

        html_blob = entry.get("summary", "") or ""
        content_list = entry.get("content")
        if content_list:
            html_blob += " " + content_list[0].get("value", "")
        match = re.search(r'<video[^>]+src="([^"]+)"', html_blob)
        if match:
            return match.group(1)
    except Exception as e:
        log.debug("استخراج ویدیو ناموفق بود: %s", e)
    return None


def build_message(source: str, title_fa: str, summary_fa: str, link: str) -> str:
    """ساخت متن نهایی پیام تلگرام با فرمت HTML.
    توجه: طبق درخواست، لینک منبع نمایش داده نمی‌شود، فقط اسم منبع می‌آید."""
    parts = [
        f"📱 <b>{title_fa}</b>",
        "",
    ]
    if summary_fa:
        parts.append(summary_fa)
        parts.append("")
    parts.append(f"🗞 منبع: {source}")
    parts.append("")
    parts.append("#اخبار_موبایل #MobileSignal")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ارسال به تلگرام
# ---------------------------------------------------------------------------

def send_to_telegram(text: str) -> bool:
    """ارسال پیام متنی ساده (بدون عکس)."""
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN یا CHANNEL_ID تنظیم نشده است.")
        return False

    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(TELEGRAM_API_URL, data=payload, timeout=20)
        if resp.status_code == 200:
            return True
        log.error("خطای تلگرام (%s): %s", resp.status_code, resp.text)
        return False
    except requests.RequestException as e:
        log.error("خطای شبکه هنگام ارسال به تلگرام: %s", e)
        return False


def send_photo_to_telegram(image_url: str, caption: str) -> bool:
    """ارسال عکس به همراه کپشن (متن خبر در بالای پیام)."""
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN یا CHANNEL_ID تنظیم نشده است.")
        return False

    payload = {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(TELEGRAM_PHOTO_API_URL, data=payload, timeout=25)
        if resp.status_code == 200:
            return True
        log.warning("ارسال عکس ناموفق بود (%s): %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.warning("خطای شبکه هنگام ارسال عکس: %s", e)
        return False


def send_video_to_telegram(video_url: str, caption: str, thumbnail_url: str | None = None) -> bool:
    """ارسال ویدیوی مستقیم به همراه کپشن. اگر thumbnail_url داده شود، تلاش می‌شود
    عکس آن دانلود و به‌عنوان کاور ویدیو ضمیمه شود (تلگرام فقط فایل آپلودی برای
    کاور قبول می‌کند، نه لینک مستقیم عکس)."""
    if not BOT_TOKEN or not CHANNEL_ID:
        log.error("BOT_TOKEN یا CHANNEL_ID تنظیم نشده است.")
        return False

    payload = {
        "chat_id": CHANNEL_ID,
        "video": video_url,
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": True,
    }

    files = None
    if thumbnail_url:
        try:
            thumb_resp = requests.get(thumbnail_url, timeout=15)
            if thumb_resp.status_code == 200 and len(thumb_resp.content) < 200_000:
                files = {"thumbnail": ("thumb.jpg", thumb_resp.content, "image/jpeg")}
                payload["thumbnail"] = "attach://thumbnail"
        except Exception as e:
            log.debug("دانلود کاور ویدیو ناموفق بود، بدون کاور ارسال می‌شود: %s", e)

    try:
        resp = requests.post(TELEGRAM_VIDEO_API_URL, data=payload, files=files, timeout=60)
        if resp.status_code == 200:
            return True
        log.warning("ارسال ویدیو ناموفق بود (%s): %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.warning("خطای شبکه هنگام ارسال ویدیو: %s", e)
        return False


def send_news_item(video_url: str | None, image_url: str | None, message: str) -> bool:
    """اولویت ارسال: ویدیوی مستقیم (با کاور عکس در صورت وجود) -> فقط عکس -> فقط متن.
    در هر مرحله، اگر ارسال شکست بخورد، به مرحله‌ی بعدی سقوط می‌کند."""
    caption = message
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        caption = caption[: TELEGRAM_CAPTION_LIMIT - 1].rsplit(" ", 1)[0] + "…"

    if video_url:
        if send_video_to_telegram(video_url, caption, thumbnail_url=image_url):
            return True
        log.info("ارسال ویدیو ناموفق بود، بازگشت به عکس/متن.")

    if image_url:
        if send_photo_to_telegram(image_url, caption):
            return True
        log.info("ارسال با عکس ناموفق بود، بازگشت به پیام متنی ساده.")

    return send_to_telegram(message)


# ---------------------------------------------------------------------------
# منطق اصلی
# ---------------------------------------------------------------------------

def get_published_epoch(entry) -> float:
    """زمان انتشار خبر را به‌صورت epoch (عدد قابل‌مقایسه) برمی‌گرداند.
    اگر فید تاریخ نداشته باشد، ۰ برگردانده می‌شود (یعنی در مرتب‌سازی، آخر صف قرار می‌گیرد)."""
    published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published_struct:
        return 0.0
    try:
        return float(calendar.timegm(published_struct))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def run():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit(
            "متغیرهای محیطی CHANEL_MOBILE_SENDER و MOBILE_CHANNEL_ID تنظیم نشده‌اند. "
            "این مقادیر باید از GitHub Secrets تزریق شوند."
        )

    sent_links = load_sent_links()

    # ---------------------------------------------------------------------
    # مرحله‌ی ۱: جمع‌آوری همه‌ی خبرهای واجدشرایط از تمام منابع
    # (غیرتکراری و کمتر از MAX_AGE_DAYS روز عمر)
    # ---------------------------------------------------------------------
    candidates = []
    for source, feed_url in RSS_FEEDS.items():
        log.info("در حال بررسی منبع: %s", source)
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.error("خطا در دریافت فید %s: %s", source, e)
            continue

        if feed.bozo and not feed.entries:
            log.warning("فید %s قابل پردازش نبود.", source)
            continue

        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue

            news_id = make_id(link)
            if news_id in sent_links:
                continue  # قبلاً ارسال شده

            if not is_within_max_age(entry):
                continue

            candidates.append({
                "source": source,
                "entry": entry,
                "link": link,
                "news_id": news_id,
                "published_epoch": get_published_epoch(entry),
            })

    # ---------------------------------------------------------------------
    # مرحله‌ی ۲: مرتب‌سازی سراسری بر اساس زمان انتشار — جدیدترین خبر اول
    # (این ترتیب فقط برای «انتخاب بهترین/تازه‌ترین خبرها» استفاده می‌شود)
    # ---------------------------------------------------------------------
    candidates.sort(key=lambda c: c["published_epoch"], reverse=True)

    # ---------------------------------------------------------------------
    # مرحله‌ی ۳: انتخاب خبرهای نهایی، با رعایت سقف هر منبع و سقف کلی هر اجرا
    # ---------------------------------------------------------------------
    selected = []
    per_source_count = {}
    for cand in candidates:
        if len(selected) >= MAX_ITEMS_TOTAL_PER_RUN:
            log.info("به سقف کلی %s خبر در این اجرا رسیدیم؛ باقی در اجرای بعدی بررسی می‌شوند.", MAX_ITEMS_TOTAL_PER_RUN)
            break
        source = cand["source"]
        if per_source_count.get(source, 0) >= MAX_ITEMS_PER_FEED:
            continue  # سهمیه‌ی این منبع در این اجرا پر شده
        selected.append(cand)
        per_source_count[source] = per_source_count.get(source, 0) + 1

    # ---------------------------------------------------------------------
    # مرحله‌ی ۴: ترتیب ارسال را برعکس می‌کنیم (قدیمی‌ترین از بین انتخاب‌شده‌ها اول
    # ارسال می‌شود، جدیدترین خبر آخرین پیام است). چون تلگرام پیام‌ها را به ترتیب
    # زمان ارسال نشان می‌دهد، این یعنی تازه‌ترین خبر پایین‌ترین/آخرین پیام کانال می‌شود.
    # ---------------------------------------------------------------------
    selected.sort(key=lambda c: c["published_epoch"])

    new_sent_count = 0
    for cand in selected:
        source = cand["source"]
        entry = cand["entry"]
        link = cand["link"]
        news_id = cand["news_id"]

        title = entry.get("title", "").strip()
        raw_summary = entry.get("summary", "") or entry.get("description", "")
        summary = clean_summary(raw_summary, max_len=500)
        video_url = extract_video_url(entry)
        image_url = extract_image_url(entry)

        if source in PERSIAN_SOURCES:
            title_fa = title
            summary_fa = summary
        else:
            title_fa = translate_to_fa(title)
            summary_fa = translate_to_fa(summary)

        message = build_message(source, title_fa, summary_fa, link)

        if send_news_item(video_url, image_url, message):
            log.info("ارسال شد: %s", title[:60])
            sent_links.add(news_id)
            new_sent_count += 1
            time.sleep(SEND_DELAY_SECONDS)
        else:
            log.warning("ارسال ناموفق برای: %s", title[:60])

    if new_sent_count > 0:
        save_sent_links(sent_links)
        log.info("مجموع %s خبر جدید ارسال شد.", new_sent_count)
    else:
        log.info("خبر جدیدی برای ارسال یافت نشد.")


if __name__ == "__main__":
    run()
