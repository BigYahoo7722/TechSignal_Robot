"""
TechSignal (تک‌سیگنال) — ربات خودکار جمع‌آوری و انتشار اخبار تکنولوژی در تلگرام
----------------------------------------------------------------------------
- اخبار را از فیدهای RSS منابع معتبر می‌خواند
- خبرهای تکراری را با استفاده از یک فایل JSON فیلتر می‌کند
- عنوان و خلاصه‌ی هر خبر را به فارسی ترجمه می‌کند
- پیام را با فرمت مناسب در کانال تلگرام منتشر می‌کند

متغیرهای محیطی مورد نیاز (از GitHub Secrets خوانده می‌شوند):
    BOT_TOKEN   -> توکن ربات تلگرام
    CHANNEL_ID  -> آیدی عددی یا یوزرنیم کانال (مثل @TechSignalNews یا -1001234567890)
"""

import os
import json
import time
import hashlib
import logging
import requests
import feedparser
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("techsignal")

# ---------------------------------------------------------------------------
# تنظیمات قابل شخصی‌سازی
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

# کلید Gemini اختیاری است: اگر تنظیم شود، برای ترجمه‌ی روان‌تر استفاده می‌شود.
# اگر خالی باشد یا خطا بدهد، به‌صورت خودکار از گوگل ترنسلیت (deep-translator) استفاده می‌شود.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# فیدهای RSS منابع خبری معتبر تکنولوژی
RSS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Wired": "https://www.wired.com/feed/rss",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
}

SENT_FILE = "sent_news.json"

# حداکثر تعداد خبر جدیدی که در هر اجرا (هر ۳۰ دقیقه) از هر منبع ارسال می‌شود
# تا در اولین اجرا یا در صورت انباشته شدن اخبار، کانال با پیام‌های زیاد شلوغ نشود
MAX_ITEMS_PER_FEED = 3

# فاصله‌ی بین ارسال پیام‌ها برای رعایت محدودیت نرخ ارسال تلگرام
SEND_DELAY_SECONDS = 2

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


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


def translate_to_fa(text: str) -> str:
    """ترجمه‌ی متن به فارسی.
    اول با Gemini (روان‌تر) تلاش می‌کند؛ در صورت نبود کلید یا خطا،
    به گوگل ترنسلیت و در نهایت متن اصلی fallback می‌کند."""
    if not text:
        return ""

    gemini_result = translate_with_gemini(text)
    if gemini_result:
        return gemini_result

    try:
        text_trimmed = text[:1000]
        return GoogleTranslator(source="auto", target="fa").translate(text_trimmed)
    except Exception as e:
        log.warning("ترجمه ناموفق بود، متن اصلی استفاده می‌شود: %s", e)
        return text


def clean_summary(raw_summary: str, max_len: int = 220) -> str:
    """حذف تگ‌های HTML احتمالی از خلاصه‌ی RSS و کوتاه کردن آن."""
    import re
    text = re.sub(r"<[^>]+>", "", raw_summary or "")
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


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
    parts.append("#TechSignal #تکنولوژی")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ارسال به تلگرام
# ---------------------------------------------------------------------------

def send_to_telegram(text: str) -> bool:
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


# ---------------------------------------------------------------------------
# منطق اصلی
# ---------------------------------------------------------------------------

def run():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit(
            "متغیرهای محیطی BOT_TOKEN و CHANNEL_ID تنظیم نشده‌اند. "
            "این مقادیر باید از GitHub Secrets تزریق شوند."
        )

    sent_links = load_sent_links()
    new_sent_count = 0

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

        items_sent_this_feed = 0
        for entry in feed.entries:
            if items_sent_this_feed >= MAX_ITEMS_PER_FEED:
                break

            link = entry.get("link")
            if not link:
                continue

            news_id = make_id(link)
            if news_id in sent_links:
                continue  # قبلاً ارسال شده

            title = entry.get("title", "").strip()
            raw_summary = entry.get("summary", "") or entry.get("description", "")
            summary = clean_summary(raw_summary)

            title_fa = translate_to_fa(title)
            summary_fa = translate_to_fa(summary)

            message = build_message(source, title_fa, summary_fa, link)

            if send_to_telegram(message):
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
