# Резервная копия и восстановление — alpha.4

`maintenance.py` работает локально, без токенов и запросов к платформам. Нужен Python 3.12+.
Проверены настоящий SQLite/WAL, целостность снимка, профильный сценарий после восстановления,
отказ от перезаписи существующего файла и блокировка запуска восстановленной базы.
Выполнение этих команд в Docker на вашем VPS ещё нужно проверить.

## Создать и проверить копию

Бот должен хотя бы один раз успешно запуститься: база должна содержать ID бота/сообщества.
Копирование использует SQLite Backup API и включает подтверждённые записи из WAL.
Бота для создания копии останавливать не требуется. Обычный `cp` живой SQLite-базы использовать не нужно.

Пример для Telegram в обычном Compose (если DB_PATH другой, замените путь):

```bash
docker compose exec bot python maintenance.py backup --db data/telegram.sqlite3 --output data/backups/telegram-before-update.zip
docker compose exec bot python maintenance.py verify data/backups/telegram-before-update.zip
docker compose cp bot:/app/data/backups/telegram-before-update.zip ./telegram-before-update.zip
chmod 600 ./telegram-before-update.zip
```

Пример для MAX webhook:

```bash
docker compose -f compose.max-webhook.yaml exec worker python maintenance.py backup --db data/max.sqlite3 --output data/backups/max-before-update.zip
docker compose -f compose.max-webhook.yaml exec worker python maintenance.py verify data/backups/max-before-update.zip
docker compose -f compose.max-webhook.yaml cp worker:/app/data/backups/max-before-update.zip ./max-before-update.zip
chmod 600 ./max-before-update.zip
```

Для VK используйте `data/vk.sqlite3`, если DB_PATH не менялся. Для следующей копии задайте новое имя:
существующий архив не перезаписывается. Скопируйте архив ещё и на другой защищённый носитель/сервер:
копия в том же Docker volume не спасает при потере VPS. `down -v` удаляет том вместе с локальными копиями.

При прямом запуске достаточно `python3 maintenance.py backup --db data/max.sqlite3 --output backups/max.zip`
и `python3 maintenance.py verify backups/max.zip`. Файл `.env` автоматически не читается и для этих команд не нужен.

## Что сохранено

Архив содержит только `database.sqlite3` и `manifest.json`: данные чатов, ID событий, курсоры,
ID ботов/сообществ и очереди webhook. Манифест содержит проект, версию кода, время UTC, размер и SHA-256 базы.
Проверяются размер, контрольная сумма, `integrity_check`, структура таблиц и соответствие манифеста базе.
SHA-256 выявляет повреждение, но не удостоверяет автора: используйте только собственные доверенные копии.

`.env`, `groups.json`, PEM-сертификаты и настройки Caddy не включены. Сохраните их отдельно в защищённом месте:
права администратора задаются в groups.json, а токены и webhook-секрет — в .env.
Архив содержит данные участников и тексты ответов, он не зашифрован. Файлы создаются с правами 0600;
не загружайте их в GitHub или общий чат. `.gitignore` и `.dockerignore` исключают каталог backups и ZIP.

## Проверить восстановление без запуска бота

Выберите **новое имя** базы. Текущая база и её журналы не изменяются. Команда откажет,
если назначение, его WAL/SHM/journal или занятый процессом lock уже существуют.

```bash
docker compose -f compose.max-webhook.yaml exec worker python maintenance.py restore data/backups/max-before-update.zip --to data/max-restored.sqlite3
```

Результат `delivery: held` означает: данные восстановлены, отправка приостановлена.
Наличие pending-записей в восстановленном файле не означает, что их можно безопасно отправить.
Для проверки на другом компьютере: `python3 maintenance.py restore max-before-update.zip --to data/max-restored.sqlite3`.
Запускать этот второй экземпляр с настоящим токеном для проверки не нужно.

## Переключить рабочий процесс после аварии

Восстановление возвращает состояние к моменту снимка. Более поздние изменения отсутствуют;
сохранённые курсоры Telegram/VK могут привести к повторному чтению событий и повторным ответам.
Перед активацией сравните время копии, данные и реальный чат. Убедитесь, что прежний экземпляр остановлен,
в том числе на других серверах: файловая блокировка действует только на одном сервере.

Для MAX webhook:

```bash
docker compose -f compose.max-webhook.yaml stop receiver worker
docker compose -f compose.max-webhook.yaml run --rm --no-deps worker python maintenance.py activate --db data/max-restored.sqlite3 --acknowledge-rollback
```

После этого вручную измените в `.env` только `DB_PATH=data/max-restored.sqlite3`, оставив прежний токен
и секрет, затем выполните:

```bash
docker compose -f compose.max-webhook.yaml up -d
docker compose -f compose.max-webhook.yaml exec worker python max_webhook.py status
curl --fail http://127.0.0.1:8080/healthz
```

`activate` снимает блокировку запуска и переводит старые pending/sending/uncertain ответы и pending
входящие события в **held**, чтобы они не отправились автоматически после отката. Уже sent/done остаются
без изменения. Эти held-записи сохраняются для ручного разбора; автоматического повтора в этой версии нет.
Из-за удержанных событий отдельные команды могут требовать сверки и ручного выполнения.
Новые команды после запуска обрабатываются обычным образом. Снова отправьте `/help` в тестовую группу.

Для Telegram/VK используйте `docker compose stop bot`, затем
`docker compose run --rm --no-deps bot python maintenance.py activate --db data/telegram-restored.sqlite3 --acknowledge-rollback`,
задайте этот путь в DB_PATH и выполните `docker compose up -d`. Для VK замените имя базы.
Удержание старой webhook-очереди не решает риск повторного чтения Telegram/VK после отката курсора.

Исходная база остаётся на месте. Не удаляйте её до проверки восстановления. Возврат к ней — остановка
процессов, обратное изменение DB_PATH и запуск; смешивать изменения двух баз автоматически нельзя.

## Совместимость и ограничения

- Нужна база alpha.2 или новее с привязкой к ID бота; при первом обновлении проверьте прежний токен.
- С alpha.4 рабочая база также привязывается к PROJECT_ID. Подмена базы другим проектом блокируется.
  Для старой базы без такой метки первый запуск/backup предполагает, что вы выбрали правильный проект.
- Архив другого проекта отклоняется. Не меняйте PROJECT_ID ради обхода проверки.
- Резервная копия ограничена базой до 1 ГиБ; копирование ограничено 30 секундами. На диске нужен запас
  под снимок и архив. Ошибка копирования не публикует частичный архив под конечным именем.
- Автоматическое расписание, ротация копий, шифрование и удалённое хранение пока не реализованы.
- Проверка копии не подтверждает подключение к платформе или доставку сообщений.

Механизм: [Python sqlite3.backup](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup),
[SQLite Online Backup API](https://sqlite.org/backup.html).
