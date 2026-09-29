import pathlib
import sys
import tempfile
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.client import GatewayError
from dgx_remote_h3.contract import RuntimeContract
from dgx_remote_h3.media import MediaError
from dgx_remote_h3.policy import validate_inputs, validate_ref2va_inputs
from dgx_remote_h3.service import execute_ref2va_request, execute_request


class RecordingClient:
    calls = 0

    def __init__(self, contract):
        self.contract = contract

    def generate(self, fields, token, staging):
        type(self).calls += 1
        pathlib.Path(staging).write_bytes(b"transport-complete-mp4")
        return len(b"transport-complete-mp4")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        RecordingClient.calls = 0
        self.contract = RuntimeContract(
            schema_id="unit",
            profile="test",
            endpoint_id="unit",
            endpoint_host="unused",
            endpoint_port=1,
            endpoint_path="/v1/videos/sync",
            secret_path=pathlib.Path("/private/token"),
            connect_timeout_seconds=1,
            total_timeout_seconds=1,
            max_response_bytes=1024,
        )
        self.request = validate_inputs("sunrise waves", "768x448", 2, 3)

    def test_success_validates_then_publishes_unique_mp4(self):
        validated = []

        def validate(path, **expected):
            validated.append((pathlib.Path(path), expected))

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            result = execute_request(
                self.request,
                output_directory=output,
                contract=self.contract,
                token_reader=lambda path: "opaque-token",
                client_factory=RecordingClient,
                media_validator=validate,
                minimum_free_bytes=0,
            )

            self.assertEqual(RecordingClient.calls, 1)
            self.assertEqual(result.parent, output.resolve())
            self.assertTrue(result.name.endswith(".mp4"))
            self.assertEqual(result.read_bytes(), b"transport-complete-mp4")
            self.assertEqual(validated[0][1], {"width": 768, "height": 448, "requested_duration": 2})
            self.assertEqual(list(output.glob("*.part")), [])

    def test_failure_removes_staging_and_never_publishes(self):
        class FailingClient(RecordingClient):
            def generate(self, fields, token, staging):
                pathlib.Path(staging).write_bytes(b"partial-upstream-body")
                raise GatewayError("safe failure")

        def failing_validator(_path, **_expected):
            raise MediaError("decode failure")

        for client_factory, validator in (
            (FailingClient, lambda *_args, **_kwargs: None),
            (RecordingClient, failing_validator),
        ):
            with self.subTest(client=client_factory.__name__), tempfile.TemporaryDirectory() as directory:
                output = pathlib.Path(directory)
                with self.assertRaises((GatewayError, MediaError)):
                    execute_request(
                        self.request,
                        output_directory=output,
                        contract=self.contract,
                        token_reader=lambda path: "opaque-token",
                        client_factory=client_factory,
                        media_validator=validator,
                        minimum_free_bytes=0,
                    )
                self.assertEqual(list(output.iterdir()), [])

    def test_ref2va_stages_image_and_audio_and_always_removes_private_directories(self):
        observed = {}

        class ReferenceClient(RecordingClient):
            def generate(self, fields, token, staging, **kwargs):
                observed.update(fields=fields, token=token, kwargs=kwargs)
                self.assert_reference(kwargs["reference_path"])
                assert pathlib.Path(kwargs["reference_audio_path"]).read_bytes() == b"private-wav"
                pathlib.Path(staging).write_bytes(b"ref2va-mp4")

            @staticmethod
            def assert_reference(path):
                assert pathlib.Path(path).read_bytes() == b"private-png"

        def stage_image(_image, output):
            private = pathlib.Path(output) / ".private-reference-image"
            private.mkdir()
            path = private / "reference.png"
            path.write_bytes(b"private-png")
            return path

        def stage_audio(_audio, output):
            private = pathlib.Path(output) / ".private-reference-audio"
            private.mkdir()
            path = private / "reference.wav"
            path.write_bytes(b"private-wav")
            return path

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            result = execute_ref2va_request(
                validate_ref2va_inputs("same actor", 8),
                "image-tensor",
                "audio-tensor",
                output_directory=output,
                contract=self.contract,
                token_reader=lambda _path: "opaque-token",
                client_factory=ReferenceClient,
                media_validator=lambda *_args, **_kwargs: None,
                image_stager=stage_image,
                audio_stager=stage_audio,
                minimum_free_bytes=0,
            )
            self.assertEqual(observed["kwargs"]["reference_mime"], "image/png")
            self.assertEqual(observed["kwargs"]["reference_audio_mime"], "audio/wav")
            self.assertEqual(__import__("json").loads(observed["fields"]["extra_params"])["task"], "ref2va")
            self.assertTrue(result.is_file())
            self.assertFalse((output / ".private-reference-image").exists())
            self.assertFalse((output / ".private-reference-audio").exists())


if __name__ == "__main__":
    unittest.main()
