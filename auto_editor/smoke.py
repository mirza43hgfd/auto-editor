"""Headless smoke test — no Qt imports, runs anywhere (CI-safe).

Verifies: package imports, data-model round trip, worker module import.
GUI construction is NOT tested here (needs a display); see README.
"""

from .timeline import Project, Slot, SfxEvent
from . import ai_helper, auto_pipeline, render_worker, media, settings_store
from . import voiceover_studio


def main():
    p = Project("smoke")
    p.add_slot(Slot(0, 5, visual_path="x.png", label="hello"))
    p.tracks["sfx"].sfx_events.append(SfxEvent(1.0, "y.mp3", 0.7))
    assert p.total_duration == 5
    c = p.to_contract_dict()
    p2 = Project.from_contract_dict(c)
    assert p2.total_duration == 5 and p2.slots[0].label == "hello"
    assert callable(render_worker.render_project)
    assert callable(auto_pipeline.run)
    assert callable(voiceover_studio.generate_voiceover)
    assert callable(ai_helper.call_gemini)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
