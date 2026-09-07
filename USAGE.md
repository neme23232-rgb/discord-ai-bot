# 📘 Руководство пользователя

Всё, что нужно, чтобы запустить этого Discord-бота у себя: откуда взять ключи,
какой ИИ-провайдер выбрать, как всё настроить и что делать, если что-то сломалось.

---

## 1. Что вам понадобится

| Что | Где взять | Сколько стоит |
|---|---|---|
| **Python 3.11+** | [python.org/downloads](https://www.python.org/downloads/) — при установке отметьте ☑ *Add Python to PATH* | бесплатно |
| **Токен Discord-бота** | [discord.com/developers/applications](https://discord.com/developers/applications) | бесплатно |
| **Ключ ИИ-провайдера** | см. раздел 3 ниже | зависит от провайдера |

---

## 2. Токен Discord-бота (5 минут)

1. Откройте [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application** → имя → **Create**.
2. Вкладка **Bot** → **Reset Token** → скопируйте токен. Он показывается **один раз**.
3. Там же, ниже: раздел **Privileged Gateway Intents** → включите **MESSAGE CONTENT INTENT** → **Save Changes**. ⚠️ Без этого бот не запустится — самая частая ошибка.
4. Вкладка **OAuth2 → URL Generator**: отметьте `bot`; в Bot Permissions отметьте `View Channels`, `Send Messages`, `Read Message History`; откройте сгенерированную ссылку и добавьте бота на свой сервер.

## 3. Ключ ИИ: какой выбрать?

Бот работает с **любым** из трёх вариантов — переключается одной строкой в `.env`.

| Провайдер | Что вписать в `.env` | Плюсы | Минусы |
|---|---|---|---|
| **B.AI / GLM** (и любой OpenAI-совместимый сервис) | `AI_PROVIDER=custom` + `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL` | бесплатный доступ к моделям GLM, часто не нужен VPN | нужно знать точное имя модели (см. шаг 4) |
| **Gemini** | `AI_PROVIDER=gemini` + `GEMINI_API_KEY` | щедрый бесплатный тариф, без карты | нужен доступ к Google (в некоторых регионах — VPN) |
| **OpenAI** | `AI_PROVIDER=openai` + `OPENAI_API_KEY` | эталонное качество | только платно, нужен пополненный баланс |

**Рекомендация:** начните с GLM через B.AI (бесплатно и без VPN) или с Gemini,
если Google вам доступен.

### Где взять ключ

- **B.AI** — зайдите на сайт сервиса, где вы получали ключ (например, `chat.b.ai` → раздел API-ключей). Скопируйте ключ и уточните базовый URL их API (обычно `https://api.b.ai/v1`).
- **Gemini** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → *Create API key* (ключ вида `AIza...`).
- **OpenAI** — [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → *Create new secret key* (ключ вида `sk-...`).

⚠️ Ключ — это доступ к вашим деньгам/квоте. Не публикуйте его, не отправляйте в чаты. Утек — сразу пересоздайте.

---

## 4. Установка и настройка (10 минут)

```bash
# 1) скачайте проект и перейдите в папку
git clone https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПОЗИТОРИЙ.git
cd ВАШ_РЕПОЗИТОРИЙ

# 2) установите зависимости
pip install -r requirements.txt

# 3) создайте файл настроек
copy .env.example .env        # Windows   (Linux/macOS: cp .env.example .env)
```

Откройте `.env` любым блокнотом и заполните.

**Вариант А — GLM/B.AI (custom):**

```ini
DISCORD_TOKEN=ваш_токен_из_шага_2
AI_PROVIDER=custom
AI_BASE_URL=https://api.b.ai/v1
AI_API_KEY=ваш_ключ_bai
AI_MODEL=glm-5.3-flash
```

Проверить, какие модели доступны вашему ключу (и точные их имена):

```bash
python -c "import os; from dotenv import load_dotenv; load_dotenv(); from openai import OpenAI; c=OpenAI(api_key=os.getenv('AI_API_KEY'), base_url=os.getenv('AI_BASE_URL')); print([m.id for m in c.models.list()])"
```

**Вариант Б — Gemini:**

```ini
DISCORD_TOKEN=ваш_токен
AI_PROVIDER=gemini
GEMINI_API_KEY=AIza...
```

**Вариант В — OpenAI:**

```ini
DISCORD_TOKEN=ваш_токен
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Запуск:

```bash
python bot.py
```

Должно появиться: `Logged in as ИмяБота#1234 — serving 1 guild(s)` и список
`Registered commands: ask, chat, clear, ...`. Окно закрывать нельзя — пока оно
открыто, бот работает.

---

## 5. Как пользоваться ботом

### Чат

| Действие | Результат |
|---|---|
| Просто напишите сообщение в канале | бот ответит (свободный чат включён по умолчанию) |
| `.ask <текст>` | то же самое, явно |
| `.chat off` / `.chat on` | выключить/включить свободный чат в канале (бот отвечает только на команды) |
| Написать боту в ЛС или @упомянуть | ответит всегда, независимо от `.chat` |

Бот **помнит контекст** последних 40 сообщений в каждом канале отдельно и
переживает перезапуски (память лежит в `data/history.json`).

### Характер ИИ (системный промпт)

| Команда | Результат |
|---|---|
| `.promt Ты пират, отвечай как пират` | задать свой характер ИИ в канале |
| `.promt` | показать текущий характер |
| `.promt-fast` | ⚡ режим «кратко и по делу» |
| `.promt-brain` | 🧠 режим «глубокий разбор по шагам» |
| `.clear` | стереть память канала и его характер |

### Консоль (окно, где запущен бот)

Пишите прямо туда и жмите Enter:

```
nick <имя>                            сменить ник бота
activity playing|listening|watching|competing <текст>   статус-активность
activity clear                        убрать активность
status online|idle|dnd|invisible      цвет статуса
stats                                 статистика по серверам/каналам
stop                                  сохранить и выключить бота
```

---

## 6. Смена ключа/провайдера на лету

1. Остановите бота: в консоли `stop` (или Ctrl+C).
2. Отредактируйте `.env` — впишите новый `AI_PROVIDER` и нужный ключ (переменные
   других провайдеров можно не удалять, бот возьмёт только нужную).
3. Запустите снова: `python bot.py`.
   Память каналов при этом **не теряется** — она в `data/history.json`.

---

## 7. Если что-то не работает

| Симптом | Причина и решение |
|---|---|
| `DISCORD_TOKEN is missing` / `AI_... is missing` | файл `.env` не создан, лежит не в папке проекта или назван с опечаткой (именно `.env`, без `.txt`) |
| `PrivilegedIntentsRequired` | не включён Message Content Intent — раздел 2, шаг 3 |
| `Discord rejected the token` | токен устарел/скопирован не полностью — Reset Token в кабинете разработчика |
| 🛑 «AI backend rejected the request» | неверный ключ ИИ или имя модели — пересоздайте ключ, проверьте `AI_MODEL` по списку моделей (шаг 4) |
| 🟠 «service seems down» | лимит/сбой провайдера — подождите минуту; на бесплатных тарифах частая история |
| Бот в сети, но молчит | его нет в канале (права View/Send) или он не приглашён на сервер — пере-пригласите по OAuth2-ссылке |
| Хочу всё с нуля | остановите бота, удалите `data/history.json`, запустите снова |

Подробный лог всегда в `logs/bot.log`.
