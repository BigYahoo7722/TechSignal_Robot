"""
GameSignal (گیم‌سیگنال) — ربات خودکار جمع‌آوری و انتشار اخبار گیمینگ در تلگرام
----------------------------------------------------------------------------
نسخه‌ی خواهر TechSignal، مخصوص اخبار دنیای بازی‌های ویدیویی.
- اخبار را از فیدهای RSS منابع معتبر گیمینگ می‌خواند
- خبرهای تکراری را با استفاده از یک فایل JSON جداگانه فیلتر می‌کند
- عنوان و خلاصه‌ی هر خبر را به فارسی ترجمه می‌کند
- پیام را با فرمت مناسب در کانال تلگرام گیمینگ منتشر می‌کند

متغیرهای محیطی مورد نیاز (از GitHub Secrets خوانده می‌شوند):
    GAMING_BOT_TOKEN   -> توکن ربات تلگرام مخصوص گیمینگ
    GAMING_CHANNEL_ID  -> آیدی یا یوزرنیم کانال گیمینگ
    GEMINI_API_KEY     -> (مشترک با ربات تک‌سیگنال) برای ترجمه‌ی روان‌تر
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
log = logging.getLogger("gamesignal")

# ---------------------------------------------------------------------------
# تنظیمات قابل شخصی‌سازی
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("GAMING_BOT_TOKEN")
CHANNEL_ID = os.environ.get("GAMING_CHANNEL_ID")

# کلید Gemini اختیاری است: اگر تنظیم شود، برای ترجمه‌ی روان‌تر استفاده می‌شود.
# اگر خالی باشد یا خطا بدهد، به‌صورت خودکار از گوگل ترنسلیت (deep-translator) استفاده می‌شود.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# فیدهای RSS منابع خبری معتبر گیمینگ
RSS_FEEDS = {
    "IGN": "https://feeds.ign.com/ign/games-all",
    "Polygon": "https://www.polygon.com/rss/index.xml",
    "GameSpot": "https://www.gamespot.com/feeds/news/",
    "PC Gamer": "https://www.pcgamer.com/rss/",
    "Eurogamer": "https://www.eurogamer.net/feed",
    "VG247": "https://www.vg247.com/feed",
}

SENT_FILE = "sent_news_gaming.json"

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

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_PHOTO_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

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


def build_message(source: str, title_fa: str, summary_fa: str, link: str) -> str:
    """ساخت متن نهایی پیام تلگرام با فرمت HTML."""
    parts = [
        f"📡 <b>{title_fa}</b>",
        "",
    ]
    if summary_fa:
        parts.append(summary_fa)
        parts.append("")
    parts.append(f"🗞 منبع: {source}")
    parts.append(f"🔗 {link}")
    parts.append("")
    parts.append("#GameSignal #گیمینگ")
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


def send_news_item(image_url: str | None, message: str) -> bool:
    """ابتدا تلاش می‌کند خبر را همراه با عکس (در بالای پیام) بفرستد؛
    اگر عکسی نبود یا ارسالش شکست خورد، به پیام متنی ساده برمی‌گردد."""
    if image_url:
        caption = message
        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            caption = caption[: TELEGRAM_CAPTION_LIMIT - 1].rsplit(" ", 1)[0] + "…"
        if send_photo_to_telegram(image_url, caption):
            return True
        log.info("ارسال با عکس ناموفق بود، بازگشت به پیام متنی ساده.")

    return send_to_telegram(message)


# ---------------------------------------------------------------------------
# منطق اصلی
# ---------------------------------------------------------------------------

def run():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit(
            "متغیرهای محیطی GAMING_BOT_TOKEN و GAMING_CHANNEL_ID تنظیم نشده‌اند. "
            "این مقادیر باید از GitHub Secrets تزریق شوند."
        )

    sent_links = load_sent_links()
    new_sent_count = 0

    for source, feed_url in RSS_FEEDS.items():
        if new_sent_count >= MAX_ITEMS_TOTAL_PER_RUN:
            log.info("به سقف کلی %s خبر در این اجرا رسیدیم؛ باقی منابع در اجرای بعدی بررسی می‌شوند.", MAX_ITEMS_TOTAL_PER_RUN)
            break

        log.info("در حال بررسی منبع: %s", source)
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.error("خطا در دریافت فید %s: %s", source, e)
            continue

        if feed.bozo and not feed.entries:
            log.warning("فید %s قابل پردازش نبود.", source)
            continue

        items_sent_this_feed = 0
        for entry in feed.entries:
            if items_sent_this_feed >= MAX_ITEMS_PER_FEED:
                break
            if new_sent_count >= MAX_ITEMS_TOTAL_PER_RUN:
                break

            link = entry.get("link")
            if not link:
                continue

            news_id = make_id(link)
            if news_id in sent_links:
                continue  # قبلاً ارسال شده

            if not is_within_max_age(entry):
                log.info("رد شد (قدیمی‌تر از %s روز): %s", MAX_AGE_DAYS, entry.get("title", "")[:60])
                continue

            title = entry.get("title", "").strip()
            raw_summary = entry.get("summary", "") or entry.get("description", "")
            summary = clean_summary(raw_summary)
            image_url = extract_image_url(entry)

            title_fa = translate_to_fa(title)
            summary_fa = translate_to_fa(summary)

            message = build_message(source, title_fa, summary_fa, link)

            if send_news_item(image_url, message):
                log.info("ارسال شد: %s", title[:60])
                sent_links.add(news_id)
                new_sent_count += 1
                items_sent_this_feed += 1
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
