"""Configuration — contracts §6.

The keys are frozen, the example file must agree with them, and a typo must stop the service
instead of being ignored.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from test_sender_support import TempDir, make_config

from sender.config import CONFIG_KEYS, Config, ConfigError, parse_env_file
from v020.contracts import CONFIG_DEFAULTS, QUIET_POLICIES


def example_path() -> str:
    from test_sender_support import ROOT
    return os.path.join(ROOT, "castsender.conf.example")


class TestDefaults(unittest.TestCase):
    def test_defaults_match_the_contract(self):
        config = Config()
        self.assertEqual(config.chromecast, "192.168.2.80")
        self.assertEqual(config.app_id, "28028CD2")
        self.assertEqual(config.group, "Cuisine")
        self.assertEqual(config.http_bind, "192.168.2.21")
        self.assertEqual(config.http_port, 7640)
        self.assertEqual(config.delta_v2, 0.0)
        self.assertEqual(config.rate_max, 0.002)
        self.assertEqual(config.max_height, 1080)
        self.assertEqual((config.quiet_start, config.quiet_end), ("", ""))
        self.assertEqual(config.quiet_policy, "refuse")
        self.assertEqual(set(config.as_mapping()), set(CONFIG_KEYS))
        self.assertEqual(config.as_mapping(), CONFIG_DEFAULTS)


class TestExampleFile(unittest.TestCase):
    def test_the_example_parses_and_matches_the_defaults(self):
        self.assertTrue(os.path.isfile(example_path()), "castsender.conf.example is missing")
        config = Config.from_file(example_path())
        self.assertEqual(config.as_mapping(), CONFIG_DEFAULTS)
        self.assertEqual(set(parse_env_file(example_path())), set(CONFIG_KEYS))

    def test_the_example_only_names_the_frozen_keys(self):
        values = parse_env_file(example_path())
        self.assertEqual(set(values), set(CONFIG_KEYS))


class TestParsing(TempDir, unittest.TestCase):
    def write(self, text: str) -> str:
        path = os.path.join(self.root, "conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_comments_quotes_and_inline_comments(self):
        path = self.write('# a comment\nGROUP="Cuisine"  # the group\nHTTP_PORT=7641\n\n'
                          "QUIET_START=22:00\nQUIET_END=06:30\n")
        values = parse_env_file(path)
        self.assertEqual(values["GROUP"], "Cuisine")
        self.assertEqual(values["HTTP_PORT"], "7641")
        self.assertEqual(values["QUIET_START"], "22:00")
        config = Config.from_file(path)
        self.assertEqual(config.http_port, 7641)
        self.assertTrue(config.quiet.wraps_midnight)

    def test_a_bad_line_is_refused(self):
        with self.assertRaises(ConfigError):
            parse_env_file(self.write("JUSTAWORD\n"))
        with self.assertRaises(ConfigError):
            parse_env_file(self.write("lower=1\n"))

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(ConfigError) as caught:
            Config.from_file(self.write("GROUP=Cuisine\nDELTA=0.265\n"))
        self.assertIn("DELTA", str(caught.exception))

    def test_validation(self):
        bad = [
            {"APP_ID": "12345"},
            {"APP_ID": "zzzzzzzz"},
            {"HTTP_PORT": "70000"},
            {"HTTP_PORT": "port"},
            {"DELTA_V2": "9"},
            {"RATE_MAX": "0"},
            {"RATE_MAX": "2"},
            {"MAX_HEIGHT": "12"},
            {"QUIET_POLICY": "loud"},
            {"QUIET_START": "08:00"},
            {"QUIET_START": "8h", "QUIET_END": "22:00"},
            {"CHROMECAST": ""},
        ]
        for values in bad:
            with self.assertRaises(ConfigError, msg=str(values)):
                Config.from_mapping(values)

    def test_environment_and_overrides(self):
        config = Config.from_env({"GROUP": "Cuisine", "HTTP_PORT": "7645", "IGNORED": "x"})
        self.assertEqual(config.http_port, 7645)
        config = Config.from_env({"GROUP": "Cuisine"}, http_bind="127.0.0.1", http_port=0)
        self.assertEqual(config.http_bind, "127.0.0.1")
        self.assertEqual(config.http_port, 0)

    def test_policies_come_from_the_contract(self):
        for policy in QUIET_POLICIES:
            self.assertEqual(make_config(self.root, quiet_policy=policy).quiet_policy, policy)


if __name__ == "__main__":
    unittest.main()
