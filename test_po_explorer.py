import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import po_explorer as pe
import sap_po_download as po


def msg(key, ref="", mid="", start="2026-11-17T09:05:07Z", version="v1"):
    xml = f'<m xmlns="{po.DATA}" xmlns:j="{po.JAVA}"><messageKey>{key}</messageKey>'
    xml += f"<referenceID>{ref}</referenceID><messageID>{mid}</messageID><startTime>{start}</startTime>"
    xml += f"<logLocations><j:String>{version}</j:String></logLocations></m>"
    return ET.fromstring(xml)


class FakeClient:
    def __init__(self, messages, payloads):
        self.messages, self.payloads, self.calls = messages, payloads, []

    def search(self, args):
        return self.messages

    def logged_bytes(self, key, version):
        self.calls.append(key)
        p = self.payloads[key]
        if isinstance(p, Exception):
            raise p
        return p


def run(client, out, text="", **kw):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return pe.run_search(client, "id", "My/ICO", t, t, Path(out), text, delay=0, **kw)


class Tests(unittest.TestCase):
    def test_filename_scheme(self):
        stem, valid = po.message_stem(msg("K", ref="R1/x", mid="M"), "ICO")
        self.assertEqual((stem, valid), ("MsgR1_x_ICO_20261117_090507", True))
        self.assertEqual(po.message_stem(msg("K", mid="M", start="bad"), "ICO"),
                         ("MsgM_ICO_unknown-start", False))
        self.assertEqual(po.message_stem(msg("K"), "ICO")[0], "MsgK_ICO_20261117_090507")

    def test_search_saves_only_matches_and_suffixes(self):
        client = FakeClient([msg("a", ref="R"), msg("b", ref="R"), msg("c", ref="Z")],
                            {"a": b"<x>Hello</x>", "b": b"<x>HELLO</x>", "c": b"<x>nope</x>"})
        with tempfile.TemporaryDirectory() as out:
            res = run(client, out, "hello")
            names = sorted(p.name for p in Path(out).iterdir())
        self.assertEqual((res.candidates, res.matched), (3, 2))
        self.assertEqual(names, ["MsgR_My_ICO_20261117_090507.xml", "MsgR_My_ICO_20261117_090507_2.xml"])

    def test_blank_text_saves_all_and_errors_do_not_stop(self):
        client = FakeClient([msg("a"), msg("b"), msg("c")],
                            {"a": b"<x/>", "b": po.DownloadError("boom"), "c": b"<y/>"})
        with tempfile.TemporaryDirectory() as out:
            res = run(client, out)
        self.assertEqual((res.matched, len(res.errors)), (2, 1))

    def test_cancel(self):
        client = FakeClient([msg("a"), msg("b")], {"a": b"<x/>", "b": b"<x/>"})
        with tempfile.TemporaryDirectory() as out:
            res = run(client, out, cancelled=lambda: bool(client.calls))
        self.assertTrue(res.cancelled)
        self.assertEqual(client.calls, ["a"])


if __name__ == "__main__":
    unittest.main()
