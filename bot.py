"""Public review snapshots -> Telegram. Standard library, Python 3.11+."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

SOURCES = {
    'Яндекс': 'https://yandex.ru/maps/org/midiynoye_mesto/111060569158/reviews/',
    '2ГИС': 'https://2gis.ru/v_novgorod/geo/70000001113962970/tab/reviews',
}


def fetch(url, data=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None,
                                 headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'ru-RU,ru;q=0.9'})
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}') from None
    except urllib.error.URLError:
        raise RuntimeError('Сетевой запрос не выполнен') from None


def tg(method, **data):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise RuntimeError('Не заполнен TELEGRAM_BOT_TOKEN')
    result = json.loads(fetch(f'https://api.telegram.org/bot{token}/{method}', data))
    if not result.get('ok'):
        raise RuntimeError('Telegram отклонил запрос')
    return result['result']


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.inside = False
        self.parts = []
        self.attrs = {}
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.inside, self.parts, self.attrs = True, [], dict(attrs)

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.inside:
            self.scripts.append((self.attrs, ''.join(self.parts)))
            self.inside = False


def js_string(value):
    """Decode a single-quoted JS string; never evaluate JavaScript."""
    out, i = [], 0
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v'}
    while i < len(value):
        ch = value[i]
        i += 1
        if ch != '\\':
            out.append(ch)
            continue
        if i >= len(value):
            raise ValueError('Malformed JS literal')
        ch = value[i]
        i += 1
        if ch in ('u', 'x'):
            size = 4 if ch == 'u' else 2
            out.append(chr(int(value[i:i+size], 16)))
            i += size
        else:
            out.append(escapes.get(ch, ch))
    return ''.join(out)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def parse_reviews(source, html):
    parser = Scripts()
    parser.feed(html)
    records = []
    if source == 'Яндекс':
        states = [json.loads(body) for attrs, body in parser.scripts
                  if attrs.get('type') == 'application/json' and 'state-view' in attrs.get('class', '').split()]
        for state in states:
            for item in walk(state):
                if item.get('reviewId') and str(item.get('businessId')) == '111060569158':
                    records.append({'id': str(item['reviewId']), 'author': item.get('author', {}).get('name', ''),
                                    'rating': item.get('rating'), 'date': item.get('updatedTime', ''),
                                    'text': item.get('text', ''), 'url': SOURCES[source]})
    elif source == '2ГИС':
        for _, body in parser.scripts:
            pattern = r"__REACT_QUERY_STATE__\s*=\s*JSON.parse\('((?:\\.|[^'\\])*)'\)"
            for match in re.finditer(pattern, body):
                state = json.loads(js_string(match.group(1)))
                for item in walk(state):
                    if item.get('id') and str(item.get('object', {}).get('id')) == '70000001113962970' and 'rating' in item and 'date_created' in item:
                        records.append({'id': str(item['id']), 'author': item.get('user', {}).get('name', ''),
                                        'rating': item.get('rating'), 'date': item.get('date_created', ''),
                                        'text': item.get('text', ''), 'url': SOURCES[source]})
    else:
        raise ValueError('Unsupported source')
    unique = {r['id']: r for r in records}
    if not unique:
        raise RuntimeError('Отзывы не извлечены: блокировка, капча или изменилась страница')
    return sorted(unique.values(), key=lambda r: r['date'])


def key(review):
    return hashlib.sha256(review['id'].encode()).hexdigest()


def chunks(source, review):
    rating = f"{review['rating']}/5" if review.get('rating') else 'без оценки'
    text = (f'Мидийное место · Великий Новгород\n{source} · {rating}\n'
            f"{review.get('author', '')}\n{review.get('date', '')}\n\n"
            f"{review.get('text') or '(Без текста)'}\n\n{review['url']}")
    return [text[i:i+1500] for i in range(0, len(text), 1500)]


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    tmp.replace(path)


def process(source, reviews, state, send, checkpoint):
    entry = state.setdefault('sources', {}).setdefault(source, {})
    if not entry.get('initialized'):
        entry.update(initialized=True, seen=sorted(key(r) for r in reviews), pending={})
        checkpoint()
        send(f'{source}: подключён. Запомнено {len(reviews)} отзывов с публичной страницы. Старые отзывы не рассылаю.')
        return
    seen = set(entry['seen'])
    pending = entry.setdefault('pending', {})
    for review in reviews:
        rid = key(review)
        if rid in seen:
            continue
        parts = chunks(source, review)
        previous = pending.get(rid, {})
        digest = hashlib.sha256(''.join(parts).encode()).hexdigest()
        cursor = previous.get('cursor', 0) if previous.get('digest') == digest else 0
        for index in range(cursor, len(parts)):
            send(parts[index])
            pending[rid] = {'cursor': index + 1, 'digest': digest}
            checkpoint()
            time.sleep(1.1)
        seen.add(rid)
        entry['seen'] = sorted(seen)
        pending.pop(rid, None)
        checkpoint()


def pair():
    code = os.environ.get('PAIR_CODE', '')
    if len(code) < 8:
        raise RuntimeError('Добавьте PAIR_CODE: случайную строку минимум 8 символов')
    for update in tg('getUpdates', timeout=0):
        msg = update.get('message', {})
        if msg.get('chat', {}).get('type') == 'private' and msg.get('text') == '/start ' + code:
            chat_id = str(msg['chat']['id'])
            print('::add-mask::' + chat_id)
            tg('sendMessage', chat_id=chat_id,
               text='Подключение подтверждено. В GitHub создайте Secret TELEGRAM_CHAT_ID со значением:\n' + chat_id)
            tg('getUpdates', offset=update['update_id'] + 1, timeout=0)
            print('ID отправлен вам в личный чат с ботом.')
            return
    raise RuntimeError('Не найдена команда /start с вашим PAIR_CODE. Отправьте её боту и повторите pair.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['pair', 'diagnose', 'monitor'], default='diagnose')
    args = parser.parse_args()
    if args.mode == 'pair':
        return pair()
    path = Path(os.environ.get('STATE_PATH', '.bot-state/state.json'))
    # Corrupt/missing state after initialization must not silently reset deduplication.
    state = json.loads(path.read_text()) if path.exists() else {'sources': {}}
    if args.mode == 'monitor':
        chat = os.environ.get('TELEGRAM_CHAT_ID')
        if not chat:
            raise RuntimeError('Добавьте TELEGRAM_CHAT_ID')
        send = lambda text: tg('sendMessage', chat_id=chat, text=text)
    errors = []
    for source, url in SOURCES.items():
        try:
            reviews = parse_reviews(source, fetch(url))
            print(f'{source}: прочитано {len(reviews)} отзывов; это снимок, не гарантия полного списка.')
            if args.mode == 'monitor':
                process(source, reviews, state, send, lambda: save(path, state))
                state['sources'][source].pop('error', None)
                save(path, state)
        except Exception as exc:
            # Never print exceptions containing request URLs (Telegram token is in URL).
            print(f'{source}: ошибка ({type(exc).__name__}). Сбор не подтверждён.')
            errors.append(source)
            if args.mode == 'monitor':
                entry = state.setdefault('sources', {}).setdefault(source, {})
                if not entry.get('error'):
                    try:
                        send(f'⚠️ {source}: не удалось прочитать или отправить отзывы. Проверьте запуск в GitHub Actions. Новые отзывы могут быть пропущены.')
                        entry['error'] = True
                        save(path, state)
                    except Exception:
                        print('Оповещение об ошибке не доставлено.')
    print('VK: не подключён — публичная страница недоступна из среды разработки.')
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Fixed error text is safe for public Actions logs.
        print(f'Запуск остановлен ({type(exc).__name__}); проверьте секреты, режим и доступность площадок.')
        raise SystemExit(1) from None
