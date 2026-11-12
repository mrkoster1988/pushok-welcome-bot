# Pushok Sketch Icons v1 — Онбординг новичков

Оригинальный набор пиктограмм Pushok: тонкий бирюзовый карандашный контур, небольшие тёплые акценты, прозрачность и сетка 24×24. Не использует ассеты Crypto Bot.

## Привязка к существующим действиям

- `/rules` → `pi-rules`
- `/agree` → `pi-check`
- `/mine` → `pi-user`
- `/faq` → `pi-book`
- `/faqfind` → `pi-search`
- `/setwelcome` → `pi-gift`
- `/welcome` → `pi-message`

Файл: `assets/pushok-icons.svg`. Для будущих Web/Mini Apps: `<svg aria-hidden="true"><use href="/assets/pushok-icons.svg#pi-settings"></use></svg>` (путь адаптировать под сервер).

**Telegram:** кнопка Bot API принимает `icon_custom_emoji_id`, а не PNG/SVG. Для настоящих кнопок потребуется выпуск собственного набора custom emoji с реальными IDs и проверка условий Telegram. До выпуска IDs текущие команды/кнопки не менять. Для VK/MAX способ применения проверить отдельно.

Статус: ассеты и карта соответствия подготовлены, кнопки не изменены; перед слиянием проверить WebView, доступность, права и UI. Matrix/Element X остаётся вне текущей разработки.
