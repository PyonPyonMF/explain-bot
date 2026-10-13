"""Explicit opt-in render modes; ordinary requests remain 2D."""
import re


def mention_mode(text):
    match = re.match(r"^\s*(?:\[3[dд]\]|--3[dд](?=\s|$)|3[dд](?=\s|:|$))\s*:?\s*", text, flags=re.IGNORECASE)
    return (text[match.end():].strip(), "3d") if match else (text, "2d")


def selected_mode(value):
    return "3d" if value == "3d" else "2d"


def _request_words(text):
    """Only the caller's own words can select an action or output format."""
    text = re.sub(r"```[\s\S]*?```|`[^`\n]*`|«[^»]*»|“[^”]*”|\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)", " ", text)
    text = re.sub(r"(?m)^\s*>.*$", " ", text).lower().replace("ё", "е")
    return re.sub(r"https?://\S+", " ", text)


def text_task(text):
    """Explicit transcript and summary requests are distinct from an ordinary explanation."""
    if research_requested(text):
        return "research"
    text = _request_words(text)
    command_start = r"(?:^|[.!?;:\n,])\s*(?:(?:пожалуйста|please|а|лучше|теперь)\s+)*"
    patterns = [
        ("transcript", r"\b(?:сделай(?:те)?|дай(?:те)?|напиши(?:те)?|покажи(?:те)?|выведи(?:те)?|нужна|нужен|хочу)\s+(?:мне\s+)?(?:полную\s+|полный\s+)?(?:транскрипцию|транскрибацию|транскрипт|расшифровку)\b"),
        ("transcript", rf"{command_start}(?:транскрипция|транскрибация|транскрипт|расшифровка)\b"),
        ("transcript", rf"{command_start}(?:расшифруй(?:те)?|транскрибируй(?:те)?|transcribe)\b"),
        ("summary", rf"{command_start}(?:summari[sz]e|суммаризируй(?:те)?|саммаризируй(?:те)?|резюмируй(?:те)?|саммари|summary)\b"),
        ("summary", r"\b(?:сделай(?:те)?|дай(?:те)?|напиши(?:те)?|покажи(?:те)?|выведи(?:те)?|хочу)\s+(?:мне\s+)?(?:краткое\s+)?(?:резюме|саммари|summary)\b"),
        ("summary", r"\bподведи(?:те)?\s+итог(?:и)?\s+(?:видео|ролика|записи)\b"),
    ]
    matches = []
    for task, pattern in patterns:
        for match in re.finditer(pattern, text):
            before = text[max(0, match.start() - 70):match.start()]
            if re.search(r"\b(?:не|не надо|не нужно|не хочу|не нужен|не нужна|do not|don't|что значит|что означает)\s*$", before):
                continue
            matches.append((match.start(), task))
    return max(matches)[1] if matches else "explain"


def research_requested(text):
    """Opt-in deep research, including a natural instruction to study a named forum."""
    words = _request_words(text)
    patterns = [r"(?:^|[.!?;:\n,])\s*(?:пожалуйста\s+|please\s+)?(?:deep\s+research|research|рисерч|ресерч|луркай|исследуй|расследуй)\b",
                r"\b(?:сделай|проведи|хочу|нужен|нужно)\s+(?:мне\s+)?(?:глубокий\s+|подробный\s+)?(?:рисерч|ресерч|research|разбор\s+форума)\b",
                r"\b(?:сделай|проведи|хочу|нужно)\s+(?:мне\s+)?(?:глубокое\s+|подробное\s+)?(?:исследование|расследование)\b"]
    if re.search(r"https?://(?:[^/\s]*forums?\.|[^\s]*/forums?\b)|\bфорум\w*\b|\bforums?\.[\w.-]+", text, re.I):
        patterns.append(r"\b(?:изучи|изучите|исследуй|расследуй|луркай|пролуркай|study|investigate)\b")
    for pattern in patterns:
        for match in re.finditer(pattern, words):
            before = words[max(0, match.start()-50):match.start()]
            if not re.search(r"\b(?:не|не надо|не нужно|не хочу|do not|don't|что значит|что означает)\s*$", before):
                return True
    return False


def response_format(text):
    """Read explicit format instructions in the caller's words, not in quoted posts or code."""
    if text_task(text) != "explain":
        return "text"
    text = _request_words(text)
    verb = r"(?:об[ъь]?ясни(?:ть|те)?|об[ъь]?ясняй(?:те)?|поясни(?:ть|те)?|ответь(?:те)?|ответить|расскажи(?:те)?|напиши(?:те)?)"
    fillers = r"(?:пожалуйста|мне|это|только|просто|коротко|подробнее|все|этот\s+(?:тред|пост|код|пример)|эту\s+статью)"
    text_format = r"(?:текстом|в\s+(?:виде|формате)\s+текста|в\s+текстовом\s+виде)"
    patterns = [
        # The question can sit between the instruction and its format: "объясни что тут говорят текстом".
        ("text", rf"\b{verb}\b[^.!?;\n]{{0,600}}?\b(?P<requested_format>{text_format})\b(?=\s*(?:$|[,.!?;:\n]))"),
        ("text", rf"\b{verb}(?:[\s,]+{fillers})*[\s,]+(?:текстом|в\s+(?:виде|формате)\s+текста|в\s+текстовом\s+виде)\b"),
        ("text", rf"\bтекстом[\s,]+{verb}\b"),
        ("text", r"(?:^|(?<=[,;:!?.\n]))\s*(?:только\s+)?текстом(?:\s+пожалуйста)?\s*(?=[.!?,;:]|$)"),
        ("text", r"\b(?:можно|давай|нужно|хочу)\s+(?:только\s+)?текстом\b"),
        ("text", r"\b(?:дай|нужен|хочу|напиши)\s+(?:мне\s+)?текстовый\s+ответ\b"),
        ("text", r"\b(?:дай|хочу|нужно)\s+(?:об[ъь]?яснение|пояснение)\s+текстом\b"),
        ("text", r"\b(?:explain|answer|reply)\s+(?:only\s+)?(?:in|with|as)\s+(?:plain\s+)?text\b"),
        ("video", r"\b(?:сделай|сгенерируй|запиши|об[ъь]?ясни)\s+(?:мне\s+)?(?:видео|ролик)\b"),
        ("video", r"\bлучше\s+(?:видео|ролик)\b"),
    ]
    requests = []
    for mode, pattern in patterns:
        for match in re.finditer(pattern, text):
            before = text[max(0, match.start() - 40):match.start()]
            if re.search(r"\b(?:не|не надо|не нужно|не хочу|не нужен|do not|don't)\s*$", before):
                continue
            position = match.start()
            if match.groupdict().get("requested_format"):
                position = match.start("requested_format")
                prefix = text[max(0, position - 60):position]
                # Distinguish the format from a topic such as "работать с этим текстом" or "называют текстом".
                if re.search(r"\b(?:не|с|со|над|под|за|перед|между|называют|называется|называть|считают|считается)"
                             r"(?:\s+[\w-]+(?:ым|им))?\s*$", prefix):
                    continue
            requests.append((position, mode))
    return max(requests)[1] if requests else "video"
