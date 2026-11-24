"""No network: ensure a community bot cannot consume central Pushok updates."""
import unittest
from unittest.mock import patch

from transports import Telegram, TransportError


class TelegramOwnershipTests(unittest.TestCase):
    def test_reserved_bot_is_rejected_before_webhook_check(self):
        for name in ('pushok_app', 'PUSHOK_APP', 'mtproxy_pushok_bot', '@MtProxy_Pushok_Bot'):
            with self.subTest(name=name):
                bot = Telegram('synthetic-token')
                with patch.object(bot, 'api', return_value={'id':42,'username':name}) as api:
                    with self.assertRaises(TransportError):
                        bot.start()
                    api.assert_called_once_with('getMe', {})

    def test_unknown_identity_is_rejected(self):
        for me in (None, [], {}, {'username':None}, {'username':' '}):
            with self.subTest(me=me):
                bot = Telegram('synthetic-token')
                with patch.object(bot, 'api', return_value=me) as api:
                    with self.assertRaises(TransportError):
                        bot.start()
                    api.assert_called_once_with('getMe', {})

    def test_separate_bot_is_allowed(self):
        bot = Telegram('synthetic-token')
        with patch.object(bot, 'api', side_effect=[{'id':42,'username':'pushok_fixture_bot'}, {'url':''}]) as api:
            self.assertEqual(bot.start(), '42')
            self.assertEqual([call.args[0] for call in api.call_args_list], ['getMe','getWebhookInfo'])
