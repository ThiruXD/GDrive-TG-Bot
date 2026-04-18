# 🌀 GDrive-TG-Bot

A powerful Telegram bot that uploads files to **Google Drive**, registers them in **FilePress**, supports custom **URL shorteners**, tracks upload history, and provides complete account management — built with **Python**, **Pyrogram**, and **MongoDB**.

This bot is designed for users who want a clean, automated, reliable GDrive + FilePress file manager with link shortening & history management.

---

## 🚀 Features

### 📤 File Upload

* Upload any Telegram file directly to **Google Drive**
* Automatically sets file permission to **public**
* Displays upload progress with real-time percentage

### 🗂 FilePress Integration

* Automatic metadata registration via FilePress API
* Supports custom FilePress domain
* Generates link:
  `https://{domain}/file/{file_id}`

### 🔗 URL Shortening

* Add personal shortener
* Shorten GDrive & FilePress links
* Per-user shortener configuration

### 📝 Upload History

* Every upload saved in MongoDB
* `/myuploads` with:

  * Paginated listing (10 per page)
  * File buttons
  * Details view (links + short links)
* `/clear_uploads` — remove all or last N uploads

### 🔐 Account Manager

Handles:

* Google Drive
* FilePress API
* Shortener config

Full inline button UI for managing:

* View details
* Remove stored credentials

### ⚙️ Admin Tools

* `/eval` for admin execution
* `/check` test Google Drive
* Debug logs

---

## 📚 Commands

### 🔰 Basic

| Command              | Description                       |
| -------------------- | --------------------------------- |
| `/start`             | Welcome + menu                    |
| `/help`              | Complete help                     |
| `/upload`            | Upload file to GDrive + FilePress |
| `/myuploads`         | Show upload history               |
| `/clear_uploads`     | Remove all uploads                |
| `/clear_uploads <N>` | Remove last N uploads             |

---

### ☁ Google Drive

| Command               | Description                     |
| --------------------- | ------------------------------- |
| `/connect_gdrive`     | Reply to client JSON to connect |
| `/gdrive_auth <code>` | Authenticate                    |
| `/check`              | Test GDrive                     |

---

### 📦 FilePress

| Command                        | Description           |
| ------------------------------ | --------------------- |
| `/connect_filepress <API_KEY>` | Save FilePress key    |
| `/filepress_url <domain>`      | Save FilePress domain |

---

### 🔗 Shortener

| Command                           | Description          |
| --------------------------------- | -------------------- |
| `/shortener_set <host> <api_key>` | Add custom shortener |
| `/shortener_view`                 | View shortener       |
| `/shortener_remove`               | Remove shortener     |
| `/shorten <url> [alias]`          | Create short link    |

---

### 👤 Accounts

| Command     | Description                              |
| ----------- | ---------------------------------------- |
| `/accounts` | View/remove GDrive, FilePress, shortener |

---

### 🛠 Admin

| Command | Description           |
| ------- | --------------------- |
| `/eval` | Run code (admin only) |

---

# 🖥 Deployment (VPS)

### 1️⃣ Clone Repository

```bash
git clone https://github.com/ThiruXD/GDrive-TG-Bot.git
cd GDrive-TG-Bot
```

### 2️⃣ Install Requirements

```bash
pip3 install -r requirements.txt
```

### 3️⃣ Run the Bot (background)

```bash
nohup python3 bot.py &
```

---

# 🛑 Stop the Bot

### Find the process:

```bash
ps -ef | grep python3
```

### Kill the process:

```bash
kill -9 PID
```

Replace **PID** with the actual process ID.

---

# 🔧 Environment Variables

Create a `.env` file:

```
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token
ADMIN_ID=your_id
MONGO_URI=mongodb_connection_string

FILEPRESS_UPLOAD_URL=https://api.filebee.xyz/api/v1/file/add

CHANNEL_URL=https://t.me/yourchannel
GROUP_URL=https://t.me/yourgroup
DEVELOPER_URL=https://t.me/yourprofile

WELCOME_PHOTO=https://your-photourl
```

---

# 👑 Developer & Credits

### 💻 Developer / Maintainer

**Thiru XD**

### 🔗 Connect With Me

* **GitHub:** [https://Github.com/ThiruXD](https://Github.com/ThiruXD)
* **Portfolio:** [https://thiruxd.is-a.dev](https://thiruxd.is-a.dev)
* **Twitter / X:** [https://X.com/ThiruXD](https://X.com/ThiruXD)
* **LinkedIn:** [https://linkedin.com/in/thiruxd/](https://linkedin.com/in/thiruxd/)
* **Telegram:** [https://telegram.me/ThiruXD](https://telegram.me/ThiruXD)

---

# ⭐ Support

If you like this project, give it a **star ⭐ on GitHub** and share it with others!
