import tempfile
import unittest
from pathlib import Path

from jabber import config


def write(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ConfigTests(unittest.TestCase):
    def test_defaults_are_solo_loopback(self):
        c = config.load()
        self.assertEqual((c.mode, c.bind, c.auth.provider), ("solo", "127.0.0.1", "none"))

    def test_multi_requires_provider(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('mode = "multi"\n'))

    def test_multi_local_ok(self):
        c = config.load(write('mode = "multi"\nbind = "0.0.0.0"\n[auth]\nprovider = "local"\n'))
        self.assertEqual(c.auth.provider, "local")

    def test_open_bind_without_auth_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('bind = "0.0.0.0"\n'))

    def test_solo_rejects_oidc(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('[auth]\nprovider = "oidc"\noidc_issuer = "https://x"\noidc_client_id = "y"\n'))

    def test_oidc_needs_issuer(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('mode = "multi"\n[auth]\nprovider = "oidc"\n'))

    def test_proxy_ok(self):
        c = config.load(write('mode = "multi"\n[auth]\nprovider = "proxy"\nproxy_header = "Cf-Access-Authenticated-User-Email"\n'))
        self.assertEqual(c.auth.proxy_trusted_ips, ["127.0.0.1"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('colour = "red"\n'))
        with self.assertRaises(config.ConfigError):
            config.load(write('[auth]\nprovdier = "local"\n'))

    def test_model_choice_off_needs_default(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('model_choice = false\n'))
        c = config.load(write('model_choice = false\ndefault_model = "m"\n'))
        self.assertFalse(c.model_choice)

    def test_bad_mode(self):
        with self.assertRaises(config.ConfigError):
            config.load(write('mode = "team"\n'))


if __name__ == "__main__":
    unittest.main()
