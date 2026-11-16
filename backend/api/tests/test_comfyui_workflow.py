import json
from pathlib import Path

from app.media.comfyui import _substitute, _visual_prompt


def test_comfyui_workflow_uses_api_format_and_known_local_checkpoint():
    workflow_path = Path(__file__).parents[3] / "config/comfyui/workflow_api.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))

    assert workflow["3"]["class_type"] == "CheckpointLoaderSimple"
    assert workflow["3"]["inputs"]["ckpt_name"] == "v1-5-pruned-emaonly-fp16.safetensors"
    assert workflow["8"]["inputs"]["width"] == 512
    assert workflow["8"]["inputs"]["height"] == 768
    assert workflow["10"]["inputs"]["steps"] == 24
    assert workflow["9"]["class_type"] == "SaveImage"
    assert workflow["9"]["inputs"]["images"] == ["11", 0]


def test_comfyui_substitution_keeps_seed_as_integer_and_replaces_prompts():
    workflow = {
        "10": {
            "inputs": {
                "seed": "{{SEED}}",
                "positive": "{{PROMPT}}",
                "negative": "{{NEGATIVE_PROMPT}}",
            }
        }
    }

    result = _substitute(
        workflow,
        prompt="Portrait cinematic frame",
        negative_prompt="text, watermark",
        seed=987654321,
    )

    assert result["10"]["inputs"] == {
        "seed": 987654321,
        "positive": "Portrait cinematic frame",
        "negative": "text, watermark",
    }


def test_comfyui_prompt_prioritizes_scene_subject_over_optional_narration():
    prompt = _visual_prompt(
        visual_direction="A modular station above the Moon, framed in wide orbit",
        narration_segment="The station supports long-term lunar exploration.",
        visual_style="restrained documentary realism",
        lighting="cinematic",
        default_style="",
    )

    assert prompt.startswith("Literal scene: A modular station above the Moon")
    assert "exact subject prominent" in prompt
    assert "Story context: The station supports long-term lunar exploration." in prompt
    assert len(prompt) < 500
    assert "no captions" in prompt


def test_comfyui_prompt_varies_shot_type_and_enforces_color_and_vertical_readability():
    wide = _visual_prompt(
        visual_direction="A lunar gateway above the Moon",
        visual_style="documentary realism",
        lighting="natural",
        default_style="",
        scene_index=0,
    )
    detail = _visual_prompt(
        visual_direction="A lunar gateway above the Moon",
        visual_style="documentary realism",
        lighting="natural",
        default_style="",
        scene_index=2,
    )

    assert wide.startswith("Literal scene: A lunar gateway above the Moon")
    assert "photorealistic documentary photograph" in wide
    assert "vertical portrait composition" in wide
    assert "wide shot, subject dominates the frame" in wide
    assert "close detail of the meaningful object" in detail
    assert wide != detail
