"""Regression checks for untrusted Fire TV output and artwork."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "firetv_status.py"
SPEC = importlib.util.spec_from_file_location("firetv_status", SOURCE)
firetv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firetv)


class RemoteInputTests(unittest.TestCase):
    def test_child_output_ceiling_and_timeout_reap_process(self):
        cases = [
            ("import os,time; os.write(1,b'x'*100000); time.sleep(5)", 2,
             firetv.OutputLimitExceeded),
            ("import time; time.sleep(5)", 0.2, subprocess.TimeoutExpired),
        ]
        for script, deadline, error in cases:
            with self.subTest(error=error):
                started = time.monotonic()
                with self.assertRaises(error):
                    firetv.bounded_run(["/usr/bin/python3", "-c", script], deadline, 4096)
                self.assertLess(time.monotonic() - started, 2)
                self.assertIsNone(firetv.active_process)

    def test_timeout_kills_child_group_and_environment_is_closed(self):
        with patch.dict(os.environ, {"FIRETV_TEST_SECRET": "do-not-inherit"}):
            result = firetv.bounded_run(
                ["/usr/bin/python3", "-c", "import os; print(os.getenv('FIRETV_TEST_SECRET', ''))"],
                2, 4096)
        self.assertEqual(result.stdout.strip(), "")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "orphan-ran"
            child = f"import time,pathlib; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('bad')"
            parent = ("import subprocess,time; "
                      f"subprocess.Popen(['/usr/bin/python3','-c',{child!r}]); "
                      "time.sleep(5)")
            with self.assertRaises(subprocess.TimeoutExpired):
                firetv.bounded_run(["/usr/bin/python3", "-c", parent], 0.2, 4096)
            time.sleep(0.7)
            self.assertFalse(marker.exists())

    def test_session_parser_rejects_long_lines_and_excess_sessions(self):
        with self.assertRaises(firetv.OutputLimitExceeded):
            firetv.sessions("    " + "x" * 5000)
        headers = "".join("    token com.example.app/Session (userId=0)\n" for _ in range(130))
        with self.assertRaises(firetv.OutputLimitExceeded):
            firetv.sessions(headers)

    def test_bounded_snapshot_still_reads_playback(self):
        media = ("    session com.amazon.firetv.youtube/Player (userId=0)\n"
                 "      active=true\n"
                 "      state=PlaybackState {state=3, actions=38}\n"
                 "      metadata: description=Example title, Example artist, null\n")
        window = "mCurrentFocus=Window{ab3 u0 com.amazon.firetv.youtube/.MainActivity}\n"

        def fake_adb(serial, *args, **kwargs):
            if args == ("get-state",):
                return "device\n", None
            if args[-1] == "media_session":
                return media, None
            if args[-1] == "windows":
                return window, None
            self.fail(f"Unexpected ADB command: {args}")

        with patch.object(firetv, "adb", side_effect=fake_adb):
            result = firetv.snapshot("test:5555")
        self.assertEqual(result["status"], "playing")
        self.assertEqual(result["title"], "Example title")
        self.assertEqual(result["artist"], "Example artist")

    def test_final_json_has_a_byte_ceiling(self):
        sink = io.BytesIO()
        with patch.object(firetv.sys, "stdout", type("Output", (), {"buffer": sink})()):
            firetv.emit({"title": "x" * firetv.MAX_JSON_BYTES})
        result = json.loads(sink.getvalue())
        self.assertEqual(result["status"], "offline")
        self.assertLessEqual(len(sink.getvalue()), firetv.MAX_JSON_BYTES)

    def test_artwork_rejects_local_and_mixed_dns_targets(self):
        self.assertIsNone(firetv.artwork_target("file:///etc/passwd"))
        self.assertIsNone(firetv.artwork_target("http://example.com/a.png"))
        self.assertIsNone(firetv.artwork_target("https://example.com:8443/a.png"))
        with patch.object(firetv.socket, "getaddrinfo", return_value=[
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]):
            self.assertIsNone(firetv.artwork_target("https://example.com/a.png"))

    @unittest.skipUnless(Path(firetv.MAGICK).is_file(), "optional ImageMagick unavailable")
    def test_artwork_is_decoded_to_bounded_local_png(self):
        png = subprocess.check_output([firetv.MAGICK, "-size", "2x2", "xc:red", "png:-"])

        class Response:
            status = 200

            def __init__(self):
                self.data = png

            def getheader(self, name, default=""):
                return {"Content-Type": "image/png", "Content-Length": str(len(png))}.get(name, default)

            def read(self, length):
                data, self.data = self.data[:length], self.data[length:]
                return data

        class Connection:
            sock = None

            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(firetv, "ARTWORK_DIR", Path(directory)), \
                patch.object(firetv, "artwork_target", return_value=("example.com", "8.8.8.8", "/a")), \
                patch.object(firetv.http.client, "HTTPSConnection", Connection):
            name = firetv.fetch_artwork("https://example.com/a")
            self.assertRegex(name, r"^[0-9a-f]{24}\.png$")
            output = Path(directory) / name
            self.assertTrue(output.is_file())
            self.assertLessEqual(output.stat().st_size, 512 * 1024)
            self.assertTrue(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
