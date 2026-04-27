# Regulatory Intelligence — Инструкция

## Быстрый старт
1. Распакуйте в папку, например C:\ri-platform
2. Дважды кликните запустить.bat
3. Откройте http://localhost:5000

## Настройка Confluence

### 1. Создайте API-токен
https://id.atlassian.com/manage-api-tokens → Create API token → скопируйте

### 2. Создайте файл .env
Скопируйте .env.example → переименуйте в .env → заполните:

  CONFLUENCE_URL=https://mycompany.atlassian.net
  CONFLUENCE_EMAIL=your@email.com
  CONFLUENCE_TOKEN=вставьте_токен
  CONFLUENCE_SPACE=RI
  CONFLUENCE_PARENT_ID=   (необязательно)

Space Key: Confluence → Space Settings → Space Key
Parent ID: ID страницы в URL → .../pages/123456789/...

### 3. Перезапустите приложение
В консоли: Confluence: OK + начальная синхронизация

## Что создаётся в Confluence
  Дашборд — Regulatory Intelligence
  Реестр проектов НПА
  Действующие НПА
  Календарь изменений
  Карточки проектов (по одной на каждый проект)
  Карточки действующих НПА

Каждое изменение в приложении → мгновенное обновление в Confluence (1-3 сек)

## Доступ для коллег
  Приложение (редактирование): http://[IP]:5000
  Confluence (чтение): ссылка на Дашборд

## Файлы
  app.py               — сервер
  confluence_sync.py   — синхронизация
  .env                 — ваши настройки (не публикуйте!)
  data/ri.db           — база данных
