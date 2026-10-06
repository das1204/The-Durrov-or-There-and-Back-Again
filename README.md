# The Durrov, or There and Back Again

В документе собраны требования к инфраструктуре, настройка учётных данных, команды регистрации webhook и процедуры проверки сервиса.

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-web%20service-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Render](https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=111111)](https://render.com/)
[![Neon](https://img.shields.io/badge/Database-Neon-00E599?logo=neon&logoColor=111111)](https://neon.tech/)

## Содержание

- [Требования](#требования)
- [Подготовка интеграций](#подготовка-интеграций)
- [Переменные окружения](#переменные-окружения)
- [Развёртывание](#развёртывание)
- [Подключение webhook](#подключение-webhook)
- [Проверка работоспособности](#проверка-работоспособности)
- [Локальный запуск](#локальный-запуск)
- [Безопасность](#безопасность)

## Требования

Для подготовки сервиса потребуются:

- аккаунт Telegram и бот, созданный через [@BotFather](https://t.me/BotFather);
- сообщество VK с включёнными сообщениями и доступом к Callback API;
- база данных PostgreSQL в [Neon](https://neon.tech/);
- веб-сервис в [Render](https://render.com/);
- задача мониторинга в [cron-job.org](https://console.cron-job.org/jobs).

Для локального запуска требуется Python 3.12.

## Подготовка интеграций

### Telegram

Создайте бота через BotFather и сохраните выданный токен как секрет. Узнайте числовой Telegram ID владельца сервиса: он задаётся в конфигурации, чтобы бот принимал сообщения из форума только от владельца.

Подготовьте отдельный секрет для проверки webhook-запросов. Он передаётся Telegram при регистрации webhook и должен совпадать со значением `TG_WEBHOOK_SECRET`. Для переменной допустим секрет из букв латинского алфавита, цифр, `_` и `-`.

Работа бота ведётся только в темах форума — личный чат и выбор собеседника через `/list` отключены. Создайте супергруппу Telegram, включите в ней «Темы», добавьте бота администратором с правом управления темами и обязательно задайте ID группы (отрицательное число) в `TG_FORUM_CHAT_ID`. Бот принимает сообщения только от владельца и только из указанной группы; права администратора позволяют ему видеть обычные сообщения при включённом privacy mode.

Когда VK-пользователь отправит `/connect`, бот создаст для него тему, сохранит привязку в PostgreSQL и будет пересылать сообщения в обе стороны через эту тему. Существующие согласия, созданные до включения форума, не имеют привязанной темы: попросите таких пользователей отправить `/connect` ещё раз. Темы, созданные вручную заранее, автоматически не связываются с VK-пользователями; используйте создаваемые ботом темы.

### VK

Включите сообщения в настройках сообщества и создайте ключ доступа сообщества. Для отправки сообщений ключ должен иметь соответствующее право. Если используемые сценарии передают файлы или изображения, предоставьте ключу также необходимые права на документы и фотографии.

Вложения входящего сообщения VK (включая фото и прикреплённые записи со стены) пересылаются в Telegram-тему, связанную с автором сообщения. Фотографии бот скачивает с VK и загружает в Telegram, поэтому они не зависят от доступности исходной ссылки для серверов Telegram. Для пересланных сообщений добавляется подпись «Сообщение от ИМЯ (ССЫЛКА)»; Telegram-пересылка со скрытым автором может не содержать доступной ссылки на профиль.

В настройках Callback API или через VK API:

1. добавьте endpoint сервиса `/vk_callback`;
2. задайте секретный ключ и сохраните его для `VK_CALLBACK_SECRET`;
3. включите событие `message_new`;
4. используйте выданную VK строку подтверждения как значение `VK_CONFIRMATION`.

Для `VK_GROUP_ID` укажите числовой ID сообщества.

Ключ сообщества для работы приложения и учётные данные для администрирования Callback API могут требовать разные права. Не выдавайте runtime-ключу административные права только ради регистрации сервера; при необходимости настройте Callback API через интерфейс сообщества.

При программной настройке получите код подтверждения до регистрации Callback server. Задайте полученное значение как `VK_CONFIRMATION` и дождитесь успешного deploy приложения: при проверочном запросе VK сервис должен вернуть этот код без JSON-обёртки.

### Neon

Создайте PostgreSQL-проект в Neon и получите строку подключения. Она используется как `DATABASE_URL`. Приложение создаёт необходимые таблицы при первом подключении к базе.

## Переменные окружения

Заполните переменные в настройках Render. Для локальной разработки используйте `.env`, созданный на основе [.env.example](./.env.example).

| Переменная | Назначение |
| --- | --- |
| `TG_TOKEN` | Токен Telegram-бота |
| `TG_OWNER_ID` | Числовой Telegram ID владельца |
| `TG_WEBHOOK_SECRET` | Секрет заголовка Telegram webhook |
| `TG_FORUM_CHAT_ID` | Обязательный отрицательный ID супергруппы Telegram с включёнными темами |
| `VK_TOKEN` | Ключ доступа сообщества VK |
| `VK_GROUP_ID` | Числовой ID сообщества VK |
| `VK_CALLBACK_SECRET` | Секрет Callback API VK |
| `VK_CONFIRMATION` | Строка подтверждения Callback API |
| `DATABASE_URL` | Строка подключения к PostgreSQL в Neon |

Не добавляйте реальные значения в README, исходный код или систему контроля версий.

## Развёртывание

1. Подключите репозиторий к Render и создайте сервис типа **Web Service** через Blueprint.
2. Проверьте параметры сервиса в [`render.yaml`](./render.yaml): версия Python, команда сборки и команда запуска задаются в конфигурации проекта.
3. Добавьте все обязательные переменные окружения из таблицы выше в настройках сервиса.
4. Запустите deploy и дождитесь его успешного завершения.
5. Скопируйте публичный HTTPS-адрес сервиса: он понадобится для настройки webhook и мониторинга.

Сборка устанавливает зависимости из `requirements.txt`; приложение запускается через Gunicorn.

## Подключение webhook

Используйте публичный HTTPS-адрес Render и следующие пути:

| Интеграция | Endpoint | Конфигурация |
| --- | --- | --- |
| Telegram | `/tg_webhook` | При регистрации webhook передайте секретный токен; Telegram отправляет его в заголовке `X-Telegram-Bot-Api-Secret-Token`. |
| VK Callback API | `/vk_callback` | Укажите секрет Callback API, строку подтверждения и событие `message_new` в настройках сообщества. |

### Telegram Bot API

В примерах ниже используются переменные окружения; не подставляйте реальные токены непосредственно в команду. Задайте `TG_TOKEN`, `TG_WEBHOOK_SECRET` и `PUBLIC_BASE_URL` в защищённом окружении терминала. `PUBLIC_BASE_URL` — корневой HTTPS-адрес Render без завершающего `/`.

Зарегистрируйте webhook:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.telegram.org/bot${TG_TOKEN}/setWebhook" \
  --data-urlencode "url=${PUBLIC_BASE_URL}/tg_webhook" \
  --data-urlencode "secret_token=${TG_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
```

Успешный ответ содержит `"ok": true`. Проверьте текущую регистрацию и последние ошибки доставки:

```bash
curl --fail-with-body --silent --show-error \
  "https://api.telegram.org/bot${TG_TOKEN}/getWebhookInfo"
```

Чтобы отключить webhook:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.telegram.org/bot${TG_TOKEN}/deleteWebhook"
```

Токен входит в URL запроса к Bot API. Не публикуйте команду с раскрытым значением токена и не сохраняйте её в общедоступной истории shell.

### VK Callback API

Callback server можно добавить через настройки сообщества либо через VK API. Для API-варианта используйте ключ, которому разрешено управлять Callback API. `VK_ADMIN_TOKEN` ниже — токен для административной настройки; приложению по-прежнему передаётся отдельный `VK_TOKEN` с минимально необходимыми правами. Не включайте административный токен в конфигурацию приложения.

Получите строку подтверждения, если она ещё не скопирована из настроек сообщества:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.vk.com/method/groups.getCallbackConfirmationCode" \
  --data-urlencode "group_id=${VK_GROUP_ID}" \
  --data-urlencode "access_token=${VK_ADMIN_TOKEN}" \
  --data-urlencode "v=5.199"
```

Сохраните поле `response.code` как `VK_CONFIRMATION` в Render и дождитесь deploy до регистрации callback-сервера.

Добавьте сервер. Ответ метода `groups.addCallbackServer` содержит `server_id`; сохраните его для следующего запроса:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.vk.com/method/groups.addCallbackServer" \
  --data-urlencode "group_id=${VK_GROUP_ID}" \
  --data-urlencode "url=${PUBLIC_BASE_URL}/vk_callback" \
  --data-urlencode "title=Telegram VK bridge" \
  --data-urlencode "secret_key=${VK_CALLBACK_SECRET}" \
  --data-urlencode "access_token=${VK_ADMIN_TOKEN}" \
  --data-urlencode "v=5.199"
```

VK передаст запрос подтверждения на `/vk_callback`. Приложение отвечает значением `VK_CONFIRMATION`; сервер Callback API считается подключённым после успешного подтверждения адреса.

После создания сервера включите обработку входящих сообщений, заменив `<SERVER_ID>` значением `server_id` из предыдущего ответа:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.vk.com/method/groups.setCallbackSettings" \
  --data-urlencode "group_id=${VK_GROUP_ID}" \
  --data-urlencode "server_id=<SERVER_ID>" \
  --data-urlencode "api_version=5.199" \
  --data-urlencode "message_new=1" \
  --data-urlencode "access_token=${VK_ADMIN_TOKEN}" \
  --data-urlencode "v=5.199"
```

Ответ VK должен содержать `"response": 1`. Альтернативно укажите адрес сервера, секрет, строку подтверждения и событие `message_new` в интерфейсе Callback API сообщества. Не помещайте ключи доступа в URL и не сохраняйте команды с раскрытыми ключами в публичном shell history.

Проверьте регистрацию серверов и сохранённые настройки Callback API:

```bash
curl --fail-with-body --silent --show-error \
  --request POST "https://api.vk.com/method/groups.getCallbackServers" \
  --data-urlencode "group_id=${VK_GROUP_ID}" \
  --data-urlencode "access_token=${VK_ADMIN_TOKEN}" \
  --data-urlencode "v=5.199"
```

В списке должен присутствовать сервер с URL `${PUBLIC_BASE_URL}/vk_callback`; у него должны быть включены нужное событие и подтверждённый статус.

## Проверка работоспособности

В cron-job.org настройте периодический **GET**-запрос к endpoint `/healthz` с интервалом 5 минут. Используйте публичный HTTPS-адрес своего сервиса.

Проверить endpoint вручную можно командой:

```bash
curl --fail-with-body --silent --show-error "${PUBLIC_BASE_URL}/healthz"
```

Для ручной проверки доступны:

| Endpoint | Проверка | Успешный ответ |
| --- | --- | --- |
| `/healthz` | Наличие обязательной конфигурации сервиса | HTTP 200 |
| `/readyz` | Конфигурация и подключение к базе данных | HTTP 200 |

При ошибке проверьте состояние последнего deploy в Render, переменные окружения и логи сервиса. Для проблем подключения к базе дополнительно проверьте строку `DATABASE_URL` и доступность проекта Neon.

## Безопасность

- Храните токены, ключи доступа и строку подключения к базе только в переменных окружения или защищённом хранилище секретов.
- Не публикуйте файл `.env` и не включайте реальные секреты в коммиты.
- Ограничивайте доступ к панели Render, Neon, VK и Telegram учётными записями с многофакторной аутентификацией.
- При утечке секрета отзовите или замените его в соответствующей платформе и обновите переменную окружения сервиса.
