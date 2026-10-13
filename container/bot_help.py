"""Immediate, static user help. No model calls, media reads, queue slots or quota consumption."""
import re

from speech import MAX_CLIPS, MAX_MEDIA_BYTES, max_seconds

SECTIONS = ("overview", "text", "media", "sources", "research", "context", "3d", "limits")
ALIASES = {
    "все": "overview", "начало": "overview", "обзор": "overview", "overview": "overview",
    "текст": "text", "text": "text",
    "транскрипция": "text", "резюме": "text", "саммари": "text", "transcript": "text", "summary": "text", "summarize": "text",
    "видео": "media", "аудио": "media", "речь": "media", "media": "media",
    "video": "media", "audio": "media", "speech": "media", "voice": "media",
    "файлы": "sources", "ссылки": "sources", "материалы": "sources", "pdf": "sources", "sources": "sources",
    "files": "sources", "links": "sources",
    "research": "research", "рисерч": "research", "ресерч": "research", "форум": "research", "исследование": "research",
    "контекст": "context", "диалог": "context", "команды": "context", "context": "context", "commands": "context",
    "3d": "3d", "3д": "3d", "объекты": "3d", "objects": "3d",
    "лимиты": "limits", "ограничения": "limits", "limits": "limits",
}


def mention_section(text):
    text = re.sub(r"\s+", " ", text).strip().lower().replace("ё", "е").strip(" .!?？")
    if text in ("что ты умеешь", "как тобой пользоваться", "покажи все функции", "что ты можешь", "какие у тебя функции", "what can you do"):
        return "overview"
    match = re.fullmatch(r"(?:/?help|помощь|справка|команды|функции)(?:\s+(.+))?", text)
    if not match:
        return None
    return ALIASES.get(match[1]) if match[1] else "overview"


def help_text(section="overview", locale="ru", bot_id=None):
    section = section if section in SECTIONS else "overview"
    mention = f"<@{bot_id}>" if str(bot_id or "").isdigit() else "@бот"
    size, seconds, clips = MAX_MEDIA_BYTES // (1024 * 1024), max_seconds(), MAX_CLIPS
    if str(locale or "ru").startswith("ru"):
        intro = f"Упомяни меня {mention} и напиши запрос. Чтобы разобрать конкретный пост, ответь на него с моим упоминанием."
        pages = {
            "overview": f"""**Что я умею**
{intro}

• **Объяснять темы в видео с озвучкой:** `объясни, как работает градиентный спуск`.
• **Отвечать текстом:** `объясни что тут говорят текстом`.
• **Расшифровывать речь:** `сделай транскрипцию видео текстом` — слова с таймкодами.
• **Делать резюме:** `summarize видео` — краткие тезисы по речи, без полной расшифровки.
• **Исследовать форум:** `/research` или `рисерч: вопрос + ссылка` — видео с хронологией, версиями сторон и выводами.
• **Разбирать картинки, схемы и GIF:** прикрепи материал или ответь на сообщение с ним. Для картинок могу сделать вертикальное видео с приближениями и выносками.
• **Читать материалы:** PDF, текст и код, веб-страницы, треды и статьи X, Telegram и Reddit. YouTube — по распознанной речи; кадры только по просьбе `разбери покадрово`.
• **Отвечать на уточнения:** ответь на мой результат с упоминанием и новым вопросом. Без упоминания я не вмешиваюсь.
• **Показывать 3D:** `3d объясни, почему качается маятник`. `/objects` — библиотека 3D-объектов.

`/explain` — запрос, контекст канала, приватный ответ и выбор 2D/3D. В меню сообщения → **Приложения** доступны «Объяснить» и «Спросить про это».

Обычно отвечаю видео. `объясни текстом`, транскрипция и `summarize` включают текст. На расплывчатый запрос предложу кнопки; `выбор` открывает их вручную.
Подробнее — выбери **раздел** у `/help` (в русском интерфейсе `/помощь`).""",
            "text": f"""**Текст, транскрипция или резюме**
{intro}

• `объясни, почему это работает, текстом` — объяснение с примерами; это не просто перепечатка источника.
• `сделай транскрипцию видео текстом` — распознанные слова с таймкодами и метками говорящих, без пересказа моделью. Также: `расшифруй видео`, `transcribe video`.
• `summarize видео` — сначала распознаю речь, затем выдаю только краткое резюме. Также: `сделай резюме видео`, `суммаризируй видео`.

Последние две команды работают с видео и аудио. Вопрос можно задать и после моего предыдущего ответа: сохранённая расшифровка используется повторно.
Все три варианта отвечают текстом. Длинный текст делится на несколько сообщений; ответить с уточнением можно на любое из них.

Обычные объяснения могут включать проверку сведений и ссылки на источники. Транскрипция и резюме опираются на саму запись.""",
            "media": f"""**Видео, голосовые и звук**
Ответь на сообщение с записью, упомяни меня {mention} и напиши:
• `объясни, что здесь происходит` — видео с объяснением по кадрам и распознанной речи.
• `объясни что тут говорят текстом` — текстовое объяснение.
• `сделай транскрипцию видео текстом` — сама расшифровка с таймкодами.
• `summarize видео` — только краткое резюме речи.

Поддерживаются вложения Discord, голосовые/аудиофайлы, прямые ссылки на медиа и доступные для скачивания ролики X. Я смотрю 6 кадров из разных моментов и распознаю речь; очень быстрые события между кадрами могу пропустить.
Лимиты: {size} МиБ на файл, первые {seconds} секунд речи, до {clips} записей за запрос. О неполной расшифровке или ошибке сообщаю.
**YouTube:** пришли ссылку с `разбери`, `summarize` или `сделай транскрипцию`. По умолчанию скачиваю только аудио и читаю речь частями по 5 минут, до 60 минут на ролик. Готовую расшифровку использую повторно.
`разбери покадрово 00:10–00:20` дополнительно включает кадры — до 24 снимков из выбранного интервала. Без интервала беру выборку по всему ролику; события между снимками могу пропустить.
Распознавание может ошибаться; метки говорящих не устанавливают их личности.""",
            "sources": f"""**Картинки, документы и ссылки**
Прикрепи материал и упомяни меня {mention}; можно добавить вопрос или ответить на уже отправленный файл.

• **Фото, скриншоты, схемы и графики:** объясню изображение; в видео могу приближать детали, добавлять подсветку и выноски.
• **GIF и стикеры:** смотрю последовательность кадров вместе с контекстом сообщения.
• **PDF:** читаю текст и до 4 страниц-превью. До 10 МиБ, 60 страниц и 120 000 символов. Скан без текстового слоя пока не читается.
• **Текст и код:** можно вставить прямо в сообщение или приложить поддерживаемый текстовый файл.
• **X:** читаю пост, цитаты, статью и цепочку постов автора; ссылка может вести в середину треда. До 80 постов и 80 000 символов; недоступные части отмечаю.
• **Форумы:** могу искать по самому форуму, читать темы и следующие страницы. Например: `найди ответ на https://forum.ss13.ru/`. Авторы, цитаты и подписи рассматриваются отдельно; неясные детали уточняю.
• **Telegram, Reddit и обычные сайты:** читаю доступный текст поста или страницы. Закрытые страницы и часть сайтов могут не открыться.
• **YouTube:** распознаю речь из аудио; кадры отдельно по команде `разбери покадрово`, можно указать диапазон времени.

Для текстового объяснения добавь `объясни текстом`. Для речи и видео см. раздел «Видео и аудио».""",
            "research": f"""**Глубокое исследование форума**
`/research` / `/исследование`: укажи вопрос и ссылку в поле «запрос».
Или напиши {mention} `рисерч: почему закрылся сервер и как на это повлияла группа? Изучи https://forum.ss13.ru/`.
Фраза `изучи` со ссылкой на форум тоже включает этот режим.

Несколько раундов поиска: связанные темы, страницы обсуждений, разные написания ников, свидетельства до и после события. Затем — **видео с озвучкой**: участники, хронология, версии сторон, возможные причины и влияние группы. Под роликом — несколько ссылок; подробные рабочие заметки в чат не отправляю.
Текстовый разбор — только по просьбе `рисерч текстом: …` или с `формат:Текст` у `/research`.

Обвинения проверяю как предположения. Отдельно отмечаю слова участников и выводы по источникам; мотивы не выдумываю. Подпись под постом не доказывает историческую роль автора.

На поиск и сопоставление — до 10 минут, до 48 обращений к страницам форума и 12 веб-поисков; затем время на озвучку и рендер. Показываю ход работы. При частичных данных обозначу пробелы в видео; если сведений совсем нет, кратко сообщу об этом.
`private` / «приватно» у slash-команды скрывает результат от остальных. Один запрос расходует один пункт обычного дневного лимита; внешние API оплачиваются владельцем бота.
Уточнение можно задать ответом на разбор с моим упоминанием.""",
            "context": f"""**Команды и продолжение разговора**
**Упоминание:** {mention} + вопрос. Ответ на чужое сообщение разбирает именно его с доступным контекстом, в том числе пересланные сообщения.
**Уточнение:** ответь на мой результат с упоминанием и новым вопросом. Например: `объясни на примере текстом`. Без явной просьбы о тексте новый ответ снова будет видео. Без упоминания не отвечаю.

**`/explain`**
• `query` / «запрос» — тема или вопрос; здесь тоже работают просьбы ответить текстом.
• `messages` / «сообщений» — прочитать до 50 последних сообщений канала, к которому у меня есть доступ.
• `private` / «приватно» — результат виден только тебе.
• `mode` / «режим» — обычное видео или 3D-класс.

**Меню сообщения → Приложения**
• «Объяснить (видео)» — разобрать выбранный пост.
• «Спросить про это (видео)» — задать свой вопрос; просьба `объясни текстом` переключает ответ на текст.

**`/objects` / `/объекты`** — найти объект в 3D-библиотеке.
**`/help` / `/помощь`** — справка с выбором раздела, видна только тебе. Также можно написать мне `помощь`, `help` или `что ты умеешь`.
**Неясный запрос:** предложу кнопки выбора действия. `выбор` вызывает их вручную. «Уточнить запрос» открывает поле для текста; «Отмена» прекращает запрос. Выбирает только автор, в течение 15 минут; лимит расходуется после выбора.
Справка не расходует лимит запросов и работает, даже когда я занят.""",
            "3d": f"""**3D-класс и библиотека объектов**
Упомяни меня {mention} и начни запрос с `3d`:
`3d объясни, почему качается маятник`
Или выбери `mode:3d` / «режим:3D-класс» у `/explain`.

Сделаю видео в 3D-классе: доска, озвучка, смена ракурсов и анимированные объекты. Можно отвечать на сообщение или уточнять мой прошлый ответ; добавь `3d` в новый запрос, если снова нужен этот режим.

`/objects` / `/объекты` показывает библиотеку. Поле `query` / «поиск» ищет по названию, теме и тегам. Просмотр библиотеки виден только тебе и не расходует лимит видео.
Подходящие объекты используются повторно; новые сохраняются после успешного 3D-ролика. Я не могу обещать точную модель любого предмета — это зависит от доступных объектов.

Команды `объясни текстом`, транскрипции и `summarize` выбирают текстовый ответ.""",
            "limits": f"""**Ограничения**
• **Видео и аудио:** до {size} МиБ на файл, первые {seconds} секунд речи, максимум {clips} записи за запрос. Видео смотрю по 6 кадрам, а не непрерывно.
• **PDF:** до 10 МиБ, 60 страниц и 120 000 символов текста; до 4 страниц-превью. Сканам нужен текстовый слой.
• **Треды X:** до 80 постов автора, 80 000 символов и 12 визуальных материалов. Недоступные части отмечаю.
• **Контекст:** `/explain` может прочитать до 50 сообщений. Для уточнений храню ограниченную историю исходника и ответов; при потере части контекста сообщаю об этом.
• **YouTube:** до 60 минут речи; кадры только по явной просьбе, до 24 снимков. Прямые эфиры и недоступные ролики не обрабатываются. Повторные вопросы используют кэш расшифровки.
• **Очередь:** при занятости или достижении дневного лимита прошу попробовать позже. Справка и `/objects` лимит не расходуют.
• **Точность:** распознавание речи и объяснения могут ошибаться. Ссылки на источники помогают проверить утверждения.

Чтобы получить ответ текстом, скажи `объясни текстом`. Для разбора конкретного материала ответь на сообщение с ним и упомяни меня {mention}.""",
        }
        footer = "\n\nДругие темы: `/help` → «раздел»."
    else:
        intro = f"Mention me {mention} and ask a question, or reply to the specific message with my mention."
        pages = {
            "overview": f"""**What I can do**
{intro}

• **Narrated explainers:** `explain gradient descent` — video by default.
• **Text answers:** `explain in text why this works`.
• **Speech transcripts:** `transcribe this video` — recognized words with timestamps and speaker labels.
• **Summaries:** `summarize this video` — key points from the speech, not the full transcript.
• **Deep research:** `/research` or `research: question + forum URL` — a narrated video with a timeline, competing accounts and conclusions.
• **Pictures, diagrams and GIFs:** attach a source or reply to it. Image explanations can use portrait zooms and callouts.
• **Sources:** PDFs, text/code, web pages, X threads/articles, Telegram and Reddit. YouTube speech is transcribed; frames require an explicit frame-by-frame request.
• **Follow-ups:** reply to my result with my mention and your next question. No mention means no automatic reply.
• **3D:** `3d explain a pendulum`; `/objects` browses reusable 3D objects.

`/explain` offers a query, channel context, private replies and 2D/3D mode. The message Apps menu offers Explain and Ask about this.
Transcript and summary requests always return text. Vague requests get choice buttons; `options` opens them manually. Use `/help`'s **section** option for details.""",
            "text": f"""**Text, transcript or summary**
{intro}

`explain in text why this works` gives an explanation with examples.
`transcribe this video` returns the recognized speech with timestamps, without an LLM rewriting or shortening it.
`summarize this video` transcribes internally and returns just the main point and key takeaways.

Transcript and summary commands work with video/audio and with retained source transcripts in follow-ups. Long text is split into messages; you can reply to any part.
Ordinary explanations may check facts and cite sources. Transcripts and summaries stay grounded in the recording.""",
            "media": f"""**Video and audio**
Reply to the recording and mention me {mention}:
`explain what happens here` — narrated video explanation.
`explain in text what they are saying` — text explanation.
`transcribe this video` — the transcript itself.
`summarize this video` — a short summary of the speech.

Supports Discord video/voice/audio attachments, direct media links and downloadable X clips. I inspect six sampled frames and recognize speech; brief visual events can be missed.
Limits: {size} MiB per file, the first {seconds} seconds of audio, up to {clips} recordings per request. Partial or failed transcription is disclosed.
**YouTube:** send a link with an explanation, summary or transcription request. Audio only by default, transcribed in five-minute parts for recordings up to 60 minutes. Transcripts are reused.
`analyze frame-by-frame 00:10–00:20` additionally samples up to 24 frames. Without a range, samples span the video; intervening events may be missed. Speaker labels are local to each audio part and do not identify people.""",
            "sources": f"""**Images, files and links**
Attach a source and mention me {mention}, or reply to its message.

• Photos, screenshots, diagrams and charts: visual explanations, with zooms and callouts where useful.
• GIFs and stickers: sampled frames plus conversation context.
• PDFs: text and up to four page previews; up to 10 MiB, 60 pages and 120,000 text characters. Scans need a text layer.
• Text and code: paste them or attach a supported text file.
• X: posts, quotes, articles and author threads, including links to the middle. Up to 80 posts and 80,000 characters; missing parts are disclosed.
• Forums: native search where supported, topic pages and pagination; authors, quotes and current signatures remain separate. Give the forum URL and your question.
• Telegram, Reddit and websites: accessible post/page text. Private or unsupported pages may not open.
• YouTube: audio transcription by default; visual samples only after an explicit frame-by-frame request, optionally with a time range.

Add `explain in text` for a text explanation. See the Video/audio section for speech.""",
            "research": f"""**Deep forum research**
Use `/research` with your question and a source URL, or mention {mention}: `research: why did the server close, and what role did this group play? https://forum.example/`.

I search across related topics, spelling variants, pages and accounts, then make a narrated video covering the actors, timeline, competing accounts, causes and conclusions. Only a short caption and a few source links accompany it; detailed working notes stay internal.
For a written report, explicitly request text or select `format:Text` in `/research`.
Allegations are hypotheses to check. Participant accounts and tentative inferences stay separate; private motives are not invented.

Research limits: up to 10 minutes, 48 forum page requests and 12 web searches, followed by voice and rendering time. Progress is shown. Partial evidence is qualified in the video; if no usable evidence is found, I return a short notice.
The slash command's `private` option hides the report from others. Each run uses one normal daily quota unit; external APIs are billed to the bot owner. Reply with my mention for follow-ups.""",
            "context": f"""**Commands and conversation**
Mention {mention} with your question. Replying targets that message, including forwarded posts and available context.
Reply to my result with my mention for a follow-up. Request text explicitly each time; otherwise a new explanation is a video.

**`/explain`**
• `query`: topic or question; text instructions also work here.
• `messages`: include up to 50 recent messages I can access.
• `private`: only you see the result.
• `mode`: normal video or a 3D classroom.

**Message → Apps**
• Explain (video): explain the selected message.
• Ask about this (video): enter your question; `explain in text` switches its response to text.

**`/objects`**: browse the 3D library.
**`/help`**: private help with a section selector. Mention me with `help` or `помощь` for a channel reply.
Vague requests get action buttons. `options` opens them manually; Edit request opens a form, and Cancel discards the request. Only the requester can choose, within 15 minutes. Quota starts after selection.
Help uses no request quota and remains available when rendering is busy.""",
            "3d": f"""**3D classroom and object library**
Mention me {mention} and start with `3d`, for example `3d explain a pendulum`. Or select `mode:3d` in `/explain`.
The video uses a classroom, board, narration, camera angles and animated objects. Replies and follow-ups are supported; specify 3d again when you want it in the next request.

`/objects` browses reusable objects; `query` searches names, topics and tags. Library results are private and use no video quota.
Suitable objects can be reused, and new ones are retained after a successful 3D video. An accurate model of every object is not guaranteed.
Text, transcript and summary instructions return text.""",
            "limits": f"""**Limits**
• Video/audio: {size} MiB per file, the first {seconds} seconds of speech, up to {clips} recordings per request. Six sampled video frames.
• PDF: 10 MiB, 60 pages, 120,000 text characters and four page previews. A text layer is required.
• X threads: 80 author posts, 80,000 characters and 12 visual inputs. Missing content is disclosed.
• Context: `/explain` can read up to 50 recent messages. Follow-ups retain a bounded source and answer history.
• YouTube: up to 60 minutes of speech; up to 24 visual samples only on request. Live streams and unavailable recordings are not processed. Transcripts are cached for follow-ups.
• A queue and daily quota apply to generation. Help and `/objects` do not consume that quota.
• Speech recognition and explanations can be wrong; use cited sources to check claims.

For a text answer, say `explain in text`. To target a source, reply to it and mention me {mention}.""",
        }
        footer = "\n\nOther topics: `/help` → section."
    return pages[section] + (footer if section != "overview" else "")
