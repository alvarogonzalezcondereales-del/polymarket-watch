import contextlib
import html
import io
import json
import re
import unittest
from unittest.mock import patch

import polymarket_watch as watch


class ParseClobTokenIdsTests(unittest.TestCase):
    def test_parses_json_string_and_keeps_list_compatibility(self):
        self.assertEqual(
            watch.parse_clob_token_ids('["yes-token", "no-token"]'),
            ["yes-token", "no-token"],
        )
        self.assertEqual(
            watch.parse_clob_token_ids(["yes-token", "no-token"]),
            ["yes-token", "no-token"],
        )

    def test_rejects_malformed_or_invalid_token_lists(self):
        for value in (
            '["yes-token",]',
            '{"tokens": ["yes-token"]}',
            '["yes-token", 7]',
            '["yes-token", "   "]',
            None,
            42,
        ):
            with self.subTest(value=value):
                self.assertEqual(watch.parse_clob_token_ids(value), [])


class GetPricesBatchTests(unittest.TestCase):
    def test_url_encodes_token_id_as_one_query_parameter(self):
        with patch.object(
            watch, "api_get", return_value={"price": "0.5", "side": "BUY"}
        ) as get:
            prices = watch.get_prices_batch(["id&other=value"])

        self.assertEqual(prices, {"id&other=value": "0.5"})
        self.assertEqual(
            get.call_args.args[0],
            "https://clob.polymarket.com/last-trade-price?token_id=id%26other%3Dvalue",
        )

    def test_no_trade_placeholder_is_not_reported_as_a_last_trade(self):
        with patch.object(watch, "api_get", side_effect=[
            {"price": "0.5", "side": ""},
            {"price": "0.42", "side": "BUY"},
        ]):
            prices = watch.get_prices_batch(["untraded-token", "traded-token"])

        self.assertEqual(
            prices,
            {"untraded-token": None, "traded-token": "0.42"},
        )


class TelegramHTMLTruncationTests(unittest.TestCase):
    def test_long_escaped_text_is_not_cut_inside_an_entity(self):
        message = "<b>" + "a" * 3997 + "&amp;tail</b>"

        truncated = watch.truncate_telegram_html(message)

        self.assertEqual(truncated, "<b>" + "a" * 3997 + "...</b>")
        self.assertEqual(
            len(html.unescape(re.sub(r"</?b>", "", truncated))),
            4000,
        )

    def test_raw_entity_expansion_does_not_trigger_unnecessary_truncation(self):
        message = "<b>" + "&amp;" * 1000 + "</b>"

        self.assertGreater(len(message), 4000)
        self.assertEqual(watch.truncate_telegram_html(message), message)

    def test_truncation_counts_supplementary_unicode_as_two_utf16_units(self):
        message = "<b>" + "😀" * 6 + "</b>"

        truncated = watch.truncate_telegram_html(message, max_chars=8)

        self.assertEqual(truncated, "<b>😀😀...</b>")


class MainBehaviorTests(unittest.TestCase):
    def test_main_uses_decoded_tokens_and_escapes_dynamic_html_text(self):
        events = [{
            "title": "<b>Alpha & Omega</b>",
            "slug": "market?a=1&b=2",
            "volume": "<volume>&",
            "markets": [{
                "question": "Will <i>A & B</i> happen?",
                "clobTokenIds": json.dumps(["yes-token", "no-token"]),
            }],
        }]
        with patch.object(watch, "get_trending", return_value=events), \
             patch.object(watch, "get_prices_batch", return_value={
                 "yes-token": "0.42<&",
                 "no-token": "0.58",
             }) as get_prices, \
             patch.object(watch, "telegram_send", return_value=True) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            watch.main()

        get_prices.assert_called_once_with(["yes-token", "no-token"])
        message = send.call_args.args[0]
        for escaped_text in (
            "&lt;b&gt;Alpha &amp; Omega&lt;/b&gt;",
            "market?a=1&amp;b=2",
            "&lt;volume&gt;&amp;",
            "Will &lt;i&gt;A &amp; B&lt;/i&gt; happen?",
            "Sí: $0.42&lt;&amp;",
        ):
            with self.subTest(escaped_text=escaped_text):
                self.assertIn(escaped_text, message)
        self.assertIn("<b>Polymarket Trends</b>", message)

    def test_main_preserves_long_escaped_slug_without_raw_html_truncation(self):
        events = [{"title": "Event", "slug": "&" * 1000, "markets": []}]
        with patch.object(watch, "get_trending", return_value=events), \
             patch.object(watch, "telegram_send", return_value=True) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            watch.main()

        message = send.call_args.args[0]
        self.assertIn("&amp;" * 1000, message)

    def test_nullable_gamma_fields_do_not_abort_event_rendering(self):
        events = [{
            "title": None,
            "volume": None,
            "markets": [{"question": None, "clobTokenIds": None}],
        }]
        with patch.object(watch, "get_trending", return_value=events), \
             patch.object(watch, "get_prices_batch") as prices, \
             patch.object(watch, "telegram_send", return_value=True) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            watch.main()

        prices.assert_called_once_with([])
        message = send.call_args.args[0]
        self.assertIn("<b>Evento</b>", message)
        self.assertIn("Vol: —", message)

    def test_telegram_delivery_failure_exits_nonzero_for_empty_and_normal_results(self):
        for events in ([], [{"title": "Event", "markets": []}]):
            with self.subTest(events=events):
                with patch.object(watch, "get_trending", return_value=events), \
                     patch.object(watch, "telegram_send", return_value=False), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        watch.main()
                self.assertEqual(raised.exception.code, 1)

    def test_external_api_error_is_escaped_in_telegram_html(self):
        with patch.object(
            watch, "get_trending", return_value={"error": "<b>API & error</b>"}
        ), patch.object(watch, "telegram_send", return_value=True) as send, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                watch.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("&lt;b&gt;API &amp; error&lt;/b&gt;", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
