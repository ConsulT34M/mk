"""Exercise recording storage using boto3's offline S3 stub and a temporary local folder."""
import io
import tempfile
import unittest
import wave
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from werkzeug.exceptions import RequestedRangeNotSatisfiable

from app import app, recording_store as store


class RecordingStoreTest(unittest.TestCase):
    def test_local_recording_after_enabling_s3(self):
        item = {"object_key": store.object_key("local"), "storage_bucket": None}
        with tempfile.TemporaryDirectory() as folder, app.test_request_context(), patch.dict(
            app.config, RECORDING_DIR=folder, S3_RECORDING_BUCKET="new-bucket"
        ), patch.object(store, "_s3_client", side_effect=AssertionError("Unexpected S3 call")):
            store.put_audio(item, io.BytesIO(b"local wav"))
            with store.open_audio(item) as source:
                self.assertEqual(source.read(), b"local wav")
            with store.send_audio(item) as response:
                self.assertEqual(b"".join(response.response), b"local wav")
            store.delete_audio(item)
            self.assertFalse((Path(folder) / item["object_key"]).exists())

    def test_s3_recording_after_switching_to_local(self):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00")
        data = wav.getvalue()
        wav.seek(0)
        item = {"object_key": store.object_key("remote"), "storage_bucket": "original-bucket", "size_bytes": len(data)}
        address = {"Bucket": item["storage_bucket"], "Key": item["object_key"]}
        client = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="test", aws_secret_access_key="test")
        with closing(client), Stubber(client) as stub, app.app_context(), patch.dict(app.config, S3_RECORDING_BUCKET=None), \
                patch.object(store, "_s3_client", return_value=client):
            stub.add_response("put_object", {}, {**address, "ContentType": "audio/wav", "Body": wav})
            store.put_audio(item, wav)
            raw = io.BytesIO(data)
            stub.add_response("get_object", {"Body": StreamingBody(raw, len(data))}, address)
            with store.open_audio(item) as source, wave.open(source) as audio:
                self.assertEqual(audio.readframes(1), b"\x01\x00")
            self.assertTrue(raw.closed)

            for headers, chunk, status in [({}, data, 200), ({"Range": "bytes=4-7"}, data[4:8], 206)]:
                raw = io.BytesIO(chunk)
                result = {"Body": StreamingBody(raw, len(chunk)), "ContentLength": len(chunk)}
                if headers:
                    result["ContentRange"] = f"bytes 4-7/{len(data)}"
                stub.add_response("get_object", result, {**address, **headers})
                with app.test_request_context(headers=headers), store.send_audio(item) as response:
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.mimetype, "audio/wav")
                    self.assertEqual(response.content_length, len(chunk))
                    self.assertEqual(response.headers.get("Content-Range"), result.get("ContentRange"))
                    self.assertEqual(response.get_data(), chunk)
                self.assertTrue(raw.closed)

            with app.test_request_context(headers={"Range": "bytes=1000-"}), self.assertRaises(RequestedRangeNotSatisfiable):
                store.send_audio(item)
            stub.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=address)
            with self.assertRaises(FileNotFoundError), store.open_audio(item):
                pass
            stub.add_client_error("delete_object", "AccessDenied", http_status_code=403, expected_params=address)
            with self.assertRaisesRegex(OSError, "AccessDenied"):
                store.delete_audio(item)
            stub.add_response("delete_object", {}, address)
            store.delete_audio(item)
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
