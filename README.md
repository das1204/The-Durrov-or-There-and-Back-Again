<h1 align="center">The Durrov, or There and Back Again</h1>

<p align="center">
  <img src="./imgs/banner/banner_DenisCuster.png" alt="Баннер">
</p>

## Структура приложения

- `flask_app.py` — точка входа Gunicorn (`flask_app:app`) и совместимый фасад.
- `webhooks.py` — Flask-маршруты, приём вебхуков и обработка очереди.
- `telegram_handlers.py` / `vk_handlers.py` — обработка сообщений и логика интеграции платформ.
- `bridge_common.py` — общие настройки, HTTP/API-клиенты, работа с файлами и отчёты об ошибках.
- `storage.py` — PostgreSQL-схема и операции с контактами, темами, настройками и очередью вебхуков.

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="https://flask.palletsprojects.com/"><img src="https://img.shields.io/badge/Flask-web%20service-000000?logo=flask&logoColor=white" alt="Flask"></a>
  <a href="https://render.com/"><img src="https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=111111" alt="Render"></a>
  <a href="https://neon.tech/"><img src="https://img.shields.io/badge/Database-Neon-00E599?logo=neon&logoColor=111111" alt="Neon"></a>
</p>
