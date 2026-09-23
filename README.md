# 📡 TechSignal Telegram News Bot | ربات خبری تک‌سیگنال

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/BigYahoo7722/TechSignal_Robot/bot.yml?branch=main&label=GitHub%20Actions&logo=github&style=for-the-badge" alt="Build Status">
  <img src="https://img.shields.io/badge/Python-3.10-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Bot">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

---

## 🌐 Language Navigation / تغییر زبان
- [English Documentation](#-english-documentation)
- [راهنمای فارسی](#--راهنمای-فارسی)

---

## 🇬🇧 English Documentation

### 📌 Overview
**TechSignal Bot** is an automated tech news aggregator for Telegram channels. Powered by Python and GitHub Actions, it fetches the latest technology news from reliable RSS feeds and posts them directly to a Telegram channel without requiring any dedicated server or VPS.

### ✨ Features
* ⚡ **100% Serverless & Free:** Runs automatically on GitHub Actions infrastructure.
* 📰 **RSS Feed Integration:** Pulls news from top technology websites.
* 🔄 **Smart Deduplication:** Keeps track of sent posts using `sent_news.json` to prevent duplicates.
* ⏱️ **Automated Cron Jobs:** Scheduled to wake up and fetch news every 30 minutes.
* 🔒 **Secure:** Sensitive data (`BOT_TOKEN` & `CHANNEL_ID`) are protected via GitHub Secrets.

### 📁 Repository Structure
```text
TechSignal_Robot/
├── .github/
│   └── workflows/
│       └── bot.yml         # GitHub Actions workflow configuration
├── main.py                 # Core Python script for fetching & sending news
├── requirements.txt        # Python dependencies
├── sent_news.json          # Local database for sent articles tracking
└── .gitignore              # Files ignored by Git
