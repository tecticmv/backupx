"""
Notification text must survive whatever a failure message contains.
"""
import io
import json
from email import message_from_string
from urllib.error import HTTPError

import pytest

TELEGRAM_CHANNEL = {'type': 'telegram', 'name': 'tg', 'config': {'bot_token': 'T', 'chat_id': '42'}}


class _Response:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture JSON payloads posted with urlopen; optionally fail the first N."""
    import app.main as main
    state = {'payloads': [], 'fail_first': 0}

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        state['payloads'].append(payload)
        if len(state['payloads']) <= state['fail_first']:
            raise HTTPError(req.full_url, 400, 'Bad Request', {},
                            io.BytesIO(b'{"ok":false,"description":"Bad Request: can\'t parse entities"}'))
        return _Response({'ok': True})

    monkeypatch.setattr(main, 'urlopen', fake_urlopen)
    return state


def test_telegram_escapes_dynamic_text(sent):
    import app.main as main
    main.send_telegram_notification(TELEGRAM_CHANNEL, 'job <a&b>', 'failed',
                                    'Fatal: unable to open <data/38b4ddc444> & more', 12)
    payload = sent['payloads'][0]
    assert payload['parse_mode'] == 'HTML'
    text = payload['text']
    assert '<b>job &lt;a&amp;b&gt;</b>' in text
    assert '&lt;data/38b4ddc444&gt; &amp; more' in text
    assert '<data/' not in text
    # Only our own formatting tag is left as markup
    stripped = text.replace('<b>', '').replace('</b>', '')
    assert '<' not in stripped and '>' not in stripped


def test_telegram_falls_back_to_plain_text_on_400(sent):
    import app.main as main
    sent['fail_first'] = 1
    main.send_telegram_notification(TELEGRAM_CHANNEL, 'job', 'failed', 'oops <data/38b4ddc444>', 1)
    assert len(sent['payloads']) == 2
    retry = sent['payloads'][1]
    assert 'parse_mode' not in retry
    assert 'oops <data/38b4ddc444>' in retry['text']  # plain text: shown as is


def test_telegram_raises_if_plain_text_also_fails(sent):
    import app.main as main
    sent['fail_first'] = 2
    with pytest.raises(HTTPError):
        main.send_telegram_notification(TELEGRAM_CHANNEL, 'job', 'failed', 'x', 1)
    assert len(sent['payloads']) == 2


@pytest.mark.parametrize('message', ['<' * 5000, '&' * 4093 + 'abc', 'x' * 10000, '\U0001F600' * 3000])
def test_telegram_truncates_without_splitting_entities(sent, message):
    import app.main as main
    main.send_telegram_notification(TELEGRAM_CHANNEL, 'job', 'failed', message, 1)
    text = sent['payloads'][0]['text']
    assert main._telegram_length(text) <= main.TELEGRAM_MAX_LENGTH
    body = text.split('\n', 2)[2]
    # Every "&" starts a complete entity
    import re
    assert not re.search(r'&(?!(amp|lt|gt);)', body)
    html_text, plain_text = main._telegram_texts('job', 'failed', message, 1)
    assert main._telegram_length(plain_text) <= main.TELEGRAM_MAX_LENGTH
    assert plain_text.endswith('…')


def test_telegram_huge_job_name_still_fits(sent):
    import app.main as main
    main.send_telegram_notification(TELEGRAM_CHANNEL, '<' * 5000, 'failed', 'msg', 1)
    text = sent['payloads'][0]['text']
    assert main._telegram_length(text) <= main.TELEGRAM_MAX_LENGTH
    assert text.count('<b>') == 1 and text.count('</b>') == 1


def test_slack_escapes_markup_and_code_fence(sent):
    import app.main as main
    channel = {'type': 'slack', 'name': 's', 'config': {'webhook_url': 'https://hooks.example/x'}}
    main.send_slack_notification(channel, 'job <!channel>', 'failed', 'bad ``` <!here> <http://evil|x>', 1)
    blocks = sent['payloads'][0]['attachments'][0]['blocks']
    head, body = blocks[0]['text']['text'], blocks[1]['text']['text']
    assert '<!channel>' not in head and '&lt;!channel&gt;' in head
    assert '<!here>' not in body and '<http' not in body
    assert body.startswith('```') and body.endswith('```') and body.count('```') == 2


def test_discord_disables_mentions_and_keeps_code_block(sent):
    import app.main as main
    channel = {'type': 'discord', 'name': 'd', 'config': {'webhook_url': 'https://discord.example/x'}}
    main.send_discord_notification(channel, 'j' * 400, 'failed', '@everyone ``` break', 1)
    payload = sent['payloads'][0]
    assert payload['allowed_mentions'] == {'parse': []}
    embed = payload['embeds'][0]
    assert len(embed['title']) <= 256
    assert embed['description'].count('```') == 2


def test_email_subject_cannot_inject_headers(monkeypatch):
    import app.main as main
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **kw):
            pass

        def login(self, *a):
            pass

        def sendmail(self, frm, to, msg):
            captured['msg'] = msg

    monkeypatch.setattr(main.smtplib, 'SMTP', FakeSMTP)
    channel = {'type': 'email', 'name': 'e', 'config': {'smtp_host': 'x', 'to_addresses': 'a@example.com'}}
    main.send_email_notification(channel, 'job\r\nBcc: victim@example.com', 'failed', 'm', 1)
    parsed = message_from_string(captured['msg'])
    assert parsed['Bcc'] is None
    assert '\n' not in parsed['Subject']
