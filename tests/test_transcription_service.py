"""Prueba del orden de prioridad del servicio Whisper compartido."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from trace_engine.transcription import TranscriptionPriority, TranscriptionService


class Segment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio):
        self.calls.append(audio)
        return [Segment(str(audio))]


model = FakeModel()
service = TranscriptionService("unused", model=model)
meeting = service.submit("meeting", source="meeting",
                         priority=TranscriptionPriority.MEETING)
interactive = service.submit("dictado", source="interactive",
                             priority=TranscriptionPriority.INTERACTIVE)

assert meeting.get(timeout=2).text == "meeting"
assert interactive.get(timeout=2).text == "dictado"
assert model.calls == ["dictado", "meeting"], model.calls
assert service.pending_count == 0
service.close()
print("transcription priority OK:", model.calls)
