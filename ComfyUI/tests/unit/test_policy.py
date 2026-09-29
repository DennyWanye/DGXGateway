import json
import pathlib
import sys
import unittest


CUSTOM_NODES = pathlib.Path(__file__).resolve().parents[2] / "custom_nodes"
sys.path.insert(0, str(CUSTOM_NODES))

from dgx_remote_h3.policy import (
    InputError,
    build_fl2va_gateway_fields,
    build_gateway_fields,
    build_ref2va_gateway_fields,
    validate_inputs,
    validate_fl2va_inputs,
    validate_ref2va_inputs,
)


class PolicyTests(unittest.TestCase):
    def test_builds_exact_gateway_fields(self):
        request = validate_inputs(
            prompt="海浪在日出时抵达沙滩",
            resolution="768x448",
            duration=2,
            seed=42,
        )

        fields = build_gateway_fields(request)

        self.assertEqual(
            list(fields),
            [
                "prompt",
                "width",
                "height",
                "fps",
                "num_inference_steps",
                "flow_shift",
                "seed",
                "extra_params",
            ],
        )
        self.assertEqual(fields["prompt"], "海浪在日出时抵达沙滩")
        self.assertEqual(fields["width"], "768")
        self.assertEqual(fields["height"], "448")
        self.assertEqual(fields["fps"], "24")
        self.assertEqual(fields["num_inference_steps"], "20")
        self.assertEqual(fields["flow_shift"], "12.0")
        self.assertEqual(fields["seed"], "42")
        self.assertEqual(
            json.loads(fields["extra_params"]),
            {
                "task": "t2va",
                "duration": 2.0,
                "audio_flow_shift": 3.0,
                "required_profile": "heretic-hmnsfw-v1",
            },
        )

    def test_accepts_only_supported_resolution_duration_and_seed(self):
        for resolution in ("768x448", "1344x768"):
            for duration in (2, 3, 4):
                validate_inputs("valid prompt", resolution, duration, 0)
                validate_inputs("valid prompt", resolution, duration, 2_147_483_647)

        invalid = [
            ("valid", "640x480", 2, 0),
            ("valid", "768x448", 1, 0),
            ("valid", "768x448", 5, 0),
            ("valid", "768x448", 2, -1),
            ("valid", "768x448", 2, 2_147_483_648),
            ("valid", "768x448", True, 0),
            ("valid", "768x448", 2, True),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(InputError):
                validate_inputs(*args)

    def test_rejects_blank_and_overlong_utf8_prompt(self):
        for prompt in ("", "   \n", "你" * 683):
            with self.subTest(prompt_len=len(prompt)), self.assertRaises(InputError):
                validate_inputs(prompt, "768x448", 2, 0)

        self.assertEqual(len(("你" * 682).encode("utf-8")), 2046)
        validate_inputs("你" * 682, "768x448", 2, 0)

    def test_ref2va_policy_is_fixed_to_one_reference_1344x768_and_four_seconds(self):
        request = validate_ref2va_inputs("same actor in a new shot", 42)
        self.assertEqual((request.width, request.height, request.duration), (1344, 768, 4))
        fields = build_ref2va_gateway_fields(request)
        self.assertEqual(json.loads(fields["extra_params"])["task"], "ref2va")
        self.assertEqual(json.loads(fields["extra_params"])["duration"], 4.0)
        for prompt, seed in (("", 0), ("valid", -1), ("valid", 2_147_483_648)):
            with self.subTest(prompt=prompt, seed=seed), self.assertRaises(InputError):
                validate_ref2va_inputs(prompt, seed)

    def test_fl2va_policy_requires_a_first_frame_and_declares_required_profile(self):
        request = validate_fl2va_inputs("adult actor walks toward camera", "768x448", 4, 7)
        fields = build_fl2va_gateway_fields(request, required_profile="10eros-fl2va-v1")
        extra = json.loads(fields["extra_params"])
        self.assertEqual(extra["task"], "fl2va")
        self.assertEqual(extra["required_profile"], "10eros-fl2va-v1")
        self.assertEqual(fields["num_inference_steps"], "8")

    def test_all_generation_variants_bind_an_explicit_profile(self):
        t2va = build_gateway_fields(
            validate_inputs("cinematic neutral kitchen", "768x448", 2, 1),
            required_profile="10eros-fl2va-v1",
        )
        ref2va = build_ref2va_gateway_fields(
            validate_ref2va_inputs("same adult actor", 2),
            required_profile="heretic-ref2va-hmnsfw-v1",
        )
        self.assertEqual(json.loads(t2va["extra_params"])["required_profile"], "10eros-fl2va-v1")
        self.assertEqual(
            json.loads(ref2va["extra_params"])["required_profile"],
            "heretic-ref2va-hmnsfw-v1",
        )


if __name__ == "__main__":
    unittest.main()

class HMNSFWDurationTests(unittest.TestCase):
    def test_duration_range_and_native_fps(self):
        from dgx_remote_h3.policy import validate_ref2va_duration_inputs, build_ref2va_gateway_fields
        import json
        for duration in (4, 8, 15):
            request=validate_ref2va_duration_inputs('A rainy street',42,duration)
            fields=build_ref2va_gateway_fields(request,required_profile='heretic-ref2va-hmnsfw-v1')
            self.assertEqual(json.loads(fields['extra_params'])['duration'],duration)
            self.assertEqual(fields['fps'],'24')
        for duration in (3,16,True,4.5):
            with self.assertRaises(ValueError): validate_ref2va_duration_inputs('x',42,duration)
        with self.assertRaises(ValueError): validate_ref2va_duration_inputs('x',42,4,30)
        with self.assertRaises(ValueError): build_ref2va_gateway_fields(validate_ref2va_duration_inputs('x',42,8),required_profile='heretic-ref2va-v1')
