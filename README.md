# 📥 Content Downloader

Телеграм-бот: присылаешь ссылку — получаешь видео.
Поддержка **TikTok (без вотермарки)**, **YouTube Shorts**, **Instagram Reels**, **Threads**.

Стек: [aiogram 3](https://aiogram.dev/) (async) + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg.

## Как работает без вотермарки на TikTok

TikTok отдаёт **оригинал без вотермарки** на часть CDN-эндпоинтов — watermark
накладывается позже, при выдаче в приложение/веб-плеер. yt-dlp забирает поток
до этого шага, поэтому TikTok качается чистым по умолчанию, без доп. настроек.

## Установка

```bash
# 1. ffmpeg (нужен для склейки видео+аудио)
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Debian/Ubuntu

# 2. зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. конфиг
cp .env.example .env
# впиши BOT_TOKEN от @BotFather
```

## Запуск

```bash
source .venv/bin/activate
python bot.py
```

Открой бота в Telegram, нажми `/start`, кинь ссылку.

## Конфигурация (`.env`)

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) (обязательно) |
| `MAX_FILE_SIZE_MB` | лимит размера файла (`49` без локального сервера, до `1900` с ним) |
| `COOKIES_FILE` | путь к `cookies.txt` для Instagram / приватного контента |
| `YTDLP_AUTO_UPDATE_HOURS` | период самоперезапуска ради обновления yt-dlp (`0` = выкл) |
| `TELEGRAM_API_BASE` | URL локального Bot API server для файлов до 2 ГБ |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | креды с my.telegram.org — **только для локального сервера** |

## 🐳 Деплой

Два сервиса: бот и локальный Bot API server (файлы до 2 ГБ). Сейчас крутится на
хосте `ubuntu` (192.168.0.185, SSD): код — `/root/contentdl-bot`, данные —
`/root/contentdl-data`.

Все данные (скачанные видео + данные Bot API server) живут в одном каталоге —
`CONTENTDL_DATA_DIR` из `.env` (по умолчанию `./data`). Он монтируется в **оба**
контейнера по одному пути `/var/lib/telegram-bot-api`, иначе path-режим сервера
не найдёт файлы бота. Постоянного там всего ~100 КБ (`cookies.txt` + состояние
Bot API server), видео — только на время отправки, но на пике до ~10 ГБ
(3 задачи × до 1.9 ГБ, при склейке файл лежит дважды).

Каталог данных держи **вне** каталога с кодом: `.dockerignore` исключает только
корневые `downloads/` и `cookies.txt`, а `data/cookies.txt` через `COPY . .`
уехал бы в образ.
ffmpeg в образе — **отдельный бинарь** (сборки [BtbN](https://github.com/BtbN/FFmpeg-Builds),
не apt), чтобы не раздувать образ; вариант `gpl` — в нём нужный боту libx264.

### Первый деплой

```bash
# на хосте: каталог данных с владельцем uid 101 (= юзер telegram-bot-api)
ssh root@YOUR_SERVER 'mkdir -p /path/to/contentdl-data/downloads && chown -R 101:101 /path/to/contentdl-data'

# .env на хосте: BOT_TOKEN, TELEGRAM_API_ID/HASH, MAX_FILE_SIZE_MB=1900,
#                CONTENTDL_DATA_DIR=/path/to/contentdl-data
# залить код (если на хосте нет rsync — scp/tar):
tar czf - --exclude=.git --exclude=.venv --exclude=.env . | ssh root@YOUR_SERVER 'tar xzf - -C /path/to/contentdl-bot'

ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose up -d --build'
```

### Передеплой после правок кода

```bash
scp -r bot.py downloader.py config.py yt_dlp_plugins root@YOUR_SERVER:/path/to/contentdl-bot/
ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose up -d --build bot'
```

### Полезное

```bash
ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose logs -f bot'   # логи
ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose ps'            # статус
```

## 🔄 Авто-обновление (двухуровневое)

| Что | Механизм |
|---|---|
| **yt-dlp** (ломается чаще всего) | `entrypoint.sh` делает `pip install -U --pre yt-dlp` при каждом старте + бот сам перезапускается раз в `YTDLP_AUTO_UPDATE_HOURS` (по умолч. 24 ч) через `SIGTERM` → Docker поднимает заново → свежий yt-dlp |
| **telegram-bot-api server** | общий Watchtower хоста раз в сутки тянет новый образ из registry |

> Своего Watchtower в стеке нет: на `ubuntu` уже работает общий
> (`/root/docker-compose.yml`), и `contentdl-bot-api-1` вписан в его список
> контейнеров. Второй экземпляр без scope при старте останавливает и удаляет
> первый — проверено на себе. На новом хосте без Watchtower — добавь туда свой.
>
> Образ бота собирается локально, поэтому Watchtower его не трогает — yt-dlp
> внутри обновляется сам (см. выше), а базовый образ/код — пересборкой.

## Лимит размера файла

Официальный Telegram Bot API **не даёт боту отправлять файлы больше 50 МБ**.
Shorts / TikTok / Reels почти всегда меньше, поэтому без локального сервера хватает.

**Локальный Bot API server** (поднимается в `docker-compose.yml`) снимает лимит
до **2 ГБ**. Как это работает:

- сервис `telegram-bot-api` (образ [`aiogram/telegram-bot-api`](https://github.com/aiogram/telegram-bot-api))
  запускается в `--local` режиме рядом с ботом;
- боту передаётся `TELEGRAM_API_BASE=http://telegram-bot-api:8081`, и aiogram
  шлёт запросы туда вместо `api.telegram.org`;
- серверу нужны **api_id / api_hash** с [my.telegram.org](https://my.telegram.org)
  (это креды Telegram-приложения, не путать с токеном бота);
- после этого `MAX_FILE_SIZE_MB` можно поднять до `1900`.

Без Docker можно запустить сервер вручную и указать его URL в `TELEGRAM_API_BASE`.

## Cookies для Instagram и Threads

Instagram и Threads бот качает **сначала гостем**: публичные посты, рилсы и
карусели отдаются без логина. Сессия из `cookies.txt` нужна только для того,
что гостю не отдают: сторис, закрытые аккаунты, посты, скрытые от гостей.
Без cookies такие ссылки просто не качаются, а владельцу бот не пишет.

Если сессия нужна:

1. Поставь в браузер расширение **«Get cookies.txt LOCALLY»**.
2. Залогинься в Instagram (или Threads), экспортируй `cookies.txt`.
3. Пришли его боту документом (бот вольёт его в общий файл) или положи рядом
   с ботом и укажи путь в `COOKIES_FILE`.

Если ссылка не скачалась и под сессией из-за логина, бот пишет владельцу:
сессия протухла, пришли свежий `cookies.txt`.

> ⚠️ Только второстепенный аккаунт. Бот открыт всем, и запросы под сессией
> Instagram засчитывает аккаунту: в 2026-11 аккаунт, через который шли все
> ссылки, залочили за автоматическую активность.

## Структура

```
bot.py                — хендлеры aiogram, polling, плановый self-restart
downloader.py         — обёртка yt-dlp (детекция ссылок, скачивание, лимиты)
config.py             — загрузка .env
yt_dlp_plugins/       — плагины yt-dlp (экстрактор Threads, пока его нет в апстриме)
Dockerfile            — образ бота (python + ffmpeg)
docker/entrypoint.sh  — апдейт yt-dlp при старте → запуск бота
docker-compose.yml    — бот + локальный Bot API server + Watchtower
```
