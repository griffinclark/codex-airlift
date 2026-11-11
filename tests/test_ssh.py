import unittest
from codex_airlift.cli import merge_ssh_config


class SshConfigTest(unittest.TestCase):
    def test_alias_is_discoverable_and_human_configuration_is_preserved(self):
        human = "Host personal\n  HostName example.invalid\n"
        include = 'Include "/private/example/ssh_config"'
        block = "Host airlift\n  HostName 127.0.0.1\n"
        value = merge_ssh_config(include + "\n" + human, include, block, "airlift")
        self.assertIn(block, value)
        self.assertTrue(value.endswith(human))
        self.assertEqual(merge_ssh_config(value, include, block, "airlift"), value)

    def test_unowned_alias_and_incomplete_block_are_preserved(self):
        for original in ("Host airlift\n  HostName example.invalid\n", "# BEGIN codex-airlift airlift\n"):
            with self.assertRaises(ValueError):
                merge_ssh_config(original, "Include x", "Host airlift\n", "airlift")
